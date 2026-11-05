"""LinkedIn/JobSpy rate control: rotation, pacing, throttle detection, cooldown, coverage warning."""
import contextlib
import dataclasses
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import ManualClock
from scripts import linkedin_pull as lp
from scripts import rate_control as rc

JOBSPY_LOG = logging.getLogger(lp.LINKEDIN_LOGGER)


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def reindex(self, columns, fill_value=""):
        return self

    def to_dict(self, kind):
        return [dict(row) for row in self.rows]


class FakeJobSpy:
    """Stands in for jobspy.scrape_jobs. `linkedin(n, kwargs)` returns (rows, logged_error_or_None)."""

    def __init__(self, clock, linkedin=None, indeed_raises=False):
        self.clock, self.linkedin, self.indeed_raises = clock, linkedin, indeed_raises
        self.calls = []

    def __call__(self, **kwargs):
        site = kwargs["site_name"][0]
        self.calls.append({"site": site, "at": self.clock.monotonic(), **kwargs})
        if site == "indeed":
            if self.indeed_raises:
                raise RuntimeError("indeed down")
            return FakeFrame([{"title": "IT Specialist", "company": f"Indeed Co {len(self.calls)}", "job_url": f"u{len(self.calls)}"}])
        n = len([c for c in self.calls if c["site"] == "linkedin"])
        rows, error = self.linkedin(n, kwargs) if self.linkedin else ([], None)
        if error:
            JOBSPY_LOG.error(error)
        return FakeFrame(rows)

    def linkedin_calls(self):
        return [c for c in self.calls if c["site"] == "linkedin"]


def one_row(n, kwargs):
    return [{"title": "IT Support Specialist", "company": f"Acme {n}", "job_url": f"https://li/{n}"}], None


class LinkedInTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = ManualClock()
        self.path = Path(self._tmp.name) / "dashboard" / "provider-state.json"
        self.saved = []
        patches = [
            mock.patch.object(lp, "merge_rows", side_effect=lambda rows, root: self.saved.extend(rows) or {"added": len(rows), "path": "x", "total": 0}),
            mock.patch.object(lp, "project_root", return_value=Path(self._tmp.name)),
            mock.patch.object(lp, "api_json", side_effect=lambda url: [{}] if "remoteok" in url else {"jobs": []}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def gate(self):  # a new Gate per run, like a new process: only the state file carries over
        return rc.Gate(rc.LINKEDIN, rc.StateStore(self.path, self.clock.runtime()), self.clock.runtime())

    def run_pull(self, jobspy):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lp.main(["100"], scrape=jobspy, gate=self.gate())
        line = next(l for l in out.getvalue().splitlines() if l.startswith(rc.STATUS_PREFIX))
        return code, json.loads(line[len(rc.STATUS_PREFIX):]), out.getvalue()

    def assertWithin(self, stamp, hours):
        """`stamp` is `hours` after the throttle (the fake clock moved a little before the run ended)."""
        start = ManualClock().now()
        spare = (rc.parse_time(stamp) - start).total_seconds() - hours * 3600
        self.assertTrue(0 <= spare < 300, spare)

    def entry(self):
        return rc.StateStore(self.path, self.clock.runtime()).get("linkedin")


class PolicyTests(LinkedInTestCase):
    def test_every_group_runs_each_night_at_up_to_25_results(self):
        # Settled 2024-12-10: the measured coverage needs all 16 groups (5 local + 11 remote); each
        # remote term finds roles no other term does, so none is rotated out. 2024-12-13: 25 results
        # (three pages) per group, because 16 groups of ten pages tripped LinkedIn's burst throttle.
        # Minneapolis's 5 local groups make 21; the budget stays 16 a night and the rotation
        # cursor reaches every group within two nights.
        seen = []
        for _ in range(3):
            jobspy = FakeJobSpy(self.clock, one_row)
            code, status, _ = self.run_pull(jobspy)
            self.assertEqual((code, status["state"]), (0, "ok"))
            day = [(c["location"], c["search_term"]) for c in jobspy.linkedin_calls()]
            self.assertEqual(len(day), 16)
            self.assertTrue(all(c["results_wanted"] == 25 for c in jobspy.linkedin_calls()))
            seen.extend(day)
            self.clock.advance(days=1)
        self.assertEqual(len(set(seen)), 21)
        self.assertEqual(len(seen), 48)

    def test_a_budget_covering_every_group_wraps_the_cursor_instead_of_looping(self):
        # Review finding 2024-12-10: with a daily budget of at least len(groups) every group lands in
        # `done`, and _advance_cursor walked forward forever; the night pull hung until the step cap.
        policy = dataclasses.replace(rc.LINKEDIN, daily_budget=21)
        gate = rc.Gate(policy, rc.StateStore(self.path, self.clock.runtime()), self.clock.runtime())
        jobspy = FakeJobSpy(self.clock, one_row)
        with contextlib.redirect_stdout(io.StringIO()):
            code = lp.main(["100"], scrape=jobspy, gate=gate)
        self.assertEqual(code, 0)
        self.assertEqual(len(jobspy.linkedin_calls()), 21)  # every group ran once
        self.assertEqual(self.entry().get("rotation_cursor"), 0)  # and the cursor wrapped to this run's start

    def test_a_second_run_the_same_day_makes_no_linkedin_calls(self):
        self.run_pull(FakeJobSpy(self.clock, one_row))
        jobspy = FakeJobSpy(self.clock, one_row)
        code, status, out = self.run_pull(jobspy)
        self.assertEqual(jobspy.linkedin_calls(), [])
        self.assertEqual((code, status["providers"]["linkedin"]["state"]), (0, "skipped"))
        self.assertIn("daily_budget", out)

    def test_groups_are_serial_and_paced_60_to_120_seconds(self):
        for pick, low, high in (("low", 60, 61), ("high", 120, 121)):
            self.clock = ManualClock(pick=pick)
            self.path = Path(self._tmp.name) / pick / "state.json"
            jobspy = FakeJobSpy(self.clock, one_row)
            self.run_pull(jobspy)
            times = [c["at"] for c in jobspy.linkedin_calls()]
            gaps = [b - a for a, b in zip(times, times[1:])]
            self.assertEqual(len(gaps), 15)
            self.assertTrue(all(low <= g < high + 5 for g in gaps), gaps)
            self.assertTrue(all(g >= 60 for g in gaps))

    def test_next_window_is_the_first_0030_at_least_12_hours_away(self):
        tz = self.clock.now().tzinfo
        at = lambda *a: lp.dt.datetime(*a, tzinfo=tz)
        self.assertEqual(lp.next_window(at(2026, 10, 13, 0, 45), 12), at(2026, 10, 14, 0, 30))
        self.assertEqual(lp.next_window(at(2026, 10, 13, 14, 0), 12), at(2026, 10, 15, 0, 30))
        self.assertEqual(lp.next_window(at(2026, 10, 13, 11, 0), 12), at(2026, 10, 14, 0, 30))


class ThrottleTests(LinkedInTestCase):
    def throttled_after(self, ok_groups, message):
        def behave(n, kwargs):
            return one_row(n, kwargs) if n <= ok_groups else ([], message)
        return behave

    def test_a_single_429_pauses_10_to_15_minutes_and_the_run_carries_on(self):
        # 2024-12-13: three nights in a row one 429 rolled LinkedIn to the next 00:30 window although
        # the throttle clears in about a minute. The first 429 of a run now pauses, retries the group
        # once and the run goes on; the night is degraded, not lost, and no cooldown is persisted.
        def behave(n, kwargs):
            return ([], "429 Response - Blocked by LinkedIn for too many requests") if n == 2 else one_row(n, kwargs)
        jobspy = FakeJobSpy(self.clock, behave)
        code, status, out = self.run_pull(jobspy)
        self.assertEqual(code, 0)
        self.assertEqual(len(jobspy.linkedin_calls()), 17)  # sixteen groups, the refused one tried twice
        self.assertIn((600, 900), self.clock.uniform_calls)
        self.assertIn(600, self.clock.sleeps)
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((status["state"], linkedin["state"], linkedin["reason"]), ("degraded", "degraded", "http_429_paused"))
        self.assertNotIn("cooldown_until", linkedin)
        self.assertEqual(self.entry()["cooldown_until"], "")
        self.assertEqual(self.entry()["consecutive_degraded_runs"], 1)
        self.assertIn("degraded, not clean", out)

    def test_a_second_429_in_the_run_persists_the_next_window_cooldown(self):
        jobspy = FakeJobSpy(self.clock, self.throttled_after(1, "429 Response - Blocked by LinkedIn for too many requests"))
        code, status, out = self.run_pull(jobspy)
        self.assertEqual(len(jobspy.linkedin_calls()), 3)  # one good group, the refused one, its retry after the pause
        self.assertTrue([c for c in jobspy.calls if c["site"] == "indeed"])  # Indeed carried on
        self.assertEqual(code, 0)
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((status["state"], linkedin["state"], linkedin["reason"]), ("degraded", "cooldown", "http_429"))
        self.assertEqual(linkedin["cooldown_until"], "2024-12-11T00:30:00-04:00")
        self.assertIn("degraded, not clean", out)
        self.assertEqual(self.entry()["cooldown_until"], "2024-12-11T00:30:00-04:00")

    def test_a_throttle_is_not_a_clean_empty_result(self):
        jobspy = FakeJobSpy(self.clock, self.throttled_after(0, "429 Response - Blocked by LinkedIn for too many requests"))
        _, _, out = self.run_pull(jobspy)
        self.assertIn("FAILED - ProviderThrottled", out)
        self.assertNotIn("Sections successful: 0/", out)
        self.assertEqual(self.entry()["status"], "cooldown")
        self.assertEqual(self.entry()["consecutive_degraded_runs"], 1)

    def test_partial_rows_before_the_refusal_are_kept(self):
        def behave(n, kwargs):
            return ([{"title": "IT Specialist", "company": "Partial Co", "job_url": "https://li/p"}], "429 Response - Blocked by LinkedIn")
        self.run_pull(FakeJobSpy(self.clock, behave))
        self.assertIn("Partial Co", [row["company"] for row in self.saved])

    def test_captcha_or_authwall_cools_down_for_24_hours(self):
        for text in ("LinkedIn response status code 999 - request denied", "redirected to authwall", "CAPTCHA challenge shown"):
            self.path = Path(self._tmp.name) / (str(abs(hash(text))) + ".json")
            _, status, _ = self.run_pull(FakeJobSpy(self.clock, self.throttled_after(0, text)))
            linkedin = status["providers"]["linkedin"]
            self.assertEqual((linkedin["state"], linkedin["reason"]), ("cooldown", "challenge_or_authwall"), text)
            self.assertWithin(linkedin["cooldown_until"], hours=24)

    def test_the_cooldown_is_honoured_by_the_next_process_without_a_call(self):
        self.run_pull(FakeJobSpy(self.clock, self.throttled_after(0, "429 Response - Blocked by LinkedIn")))
        self.clock.advance(hours=8)
        jobspy = FakeJobSpy(self.clock, one_row)
        code, status, out = self.run_pull(jobspy)
        self.assertEqual(jobspy.linkedin_calls(), [])
        self.assertTrue([c for c in jobspy.calls if c["site"] == "indeed"])
        self.assertEqual((code, status["providers"]["linkedin"]["state"]), (0, "cooldown"))
        # honouring the pause is not another degraded run: the count stays as the tripping run left it
        self.assertEqual(status["providers"]["linkedin"]["consecutive_degraded_runs"], 1)
        self.assertEqual(out.count("SKIPPED"), 1)

    def test_a_long_jobspy_message_with_the_429_at_the_end_is_still_a_rate_limit(self):
        # Review finding 2024-12-10: the message was shortened to 200 characters before it was
        # classified; the real one is about 300 characters with "429" near the end, so it read as transient.
        text = ("LinkedIn: HTTPSConnectionPool(host='www.linkedin.com', port=443): Max retries exceeded with url: "
                "/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=IT+Specialist&location=Raleigh%2C+NC"
                "&distance=50&pageNum=0&start=0&f_TPR=r259200 (Caused by ResponseError('too many 429 error responses'))")
        self.assertGreater(text.find("429 error"), 250)
        _, status, _ = self.run_pull(FakeJobSpy(self.clock, self.throttled_after(0, text)))
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((linkedin["state"], linkedin["reason"]), ("cooldown", "http_429"))

    def test_linkedin_resumes_once_the_cooldown_has_expired(self):
        self.run_pull(FakeJobSpy(self.clock, self.throttled_after(0, "429 Response - Blocked by LinkedIn")))
        self.clock.advance(hours=24)
        jobspy = FakeJobSpy(self.clock, one_row)
        _, status, _ = self.run_pull(jobspy)
        self.assertEqual(len(jobspy.linkedin_calls()), 16)
        self.assertEqual(status["providers"]["linkedin"]["state"], "ok")
        self.assertEqual(self.entry()["consecutive_degraded_runs"], 0)

    def test_everything_unavailable_fails_the_step(self):
        with mock.patch.object(lp, "api_json", side_effect=OSError("offline")):
            code, status, _ = self.run_pull(FakeJobSpy(self.clock, self.throttled_after(0, "429"), indeed_raises=True))
        self.assertEqual(code, 1)
        self.assertEqual(status["providers"]["linkedin"]["state"], "cooldown")


class TransientAndEmptyTests(LinkedInTestCase):
    def test_a_genuine_zero_result_query_is_ok(self):
        jobspy = FakeJobSpy(self.clock, lambda n, kw: ([], None))
        code, status, _ = self.run_pull(jobspy)
        self.assertEqual((code, status["state"], len(jobspy.linkedin_calls())), (0, "ok", 16))
        self.assertEqual(self.entry()["cooldown_until"], "")

    def test_one_timeout_is_retried_once_after_the_group_gap(self):
        calls = {"n": 0}

        def behave(n, kwargs):
            calls["n"] += 1
            return ([], "LinkedIn: timed out") if calls["n"] == 1 else one_row(n, kwargs)

        jobspy = FakeJobSpy(self.clock, behave)
        _, status, _ = self.run_pull(jobspy)
        self.assertEqual(status["providers"]["linkedin"]["state"], "ok")
        self.assertEqual(len(jobspy.linkedin_calls()), 17)  # sixteen groups, one of them tried twice
        self.assertIn((60, 120), self.clock.uniform_calls)

    def test_repeated_timeouts_cool_down_for_6_hours(self):
        jobspy = FakeJobSpy(self.clock, lambda n, kw: ([], "LinkedIn: Read timed out"))
        _, status, _ = self.run_pull(jobspy)
        self.assertEqual(len(jobspy.linkedin_calls()), 2)  # first try plus its one retry, then stop
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((linkedin["state"], linkedin["reason"]), ("cooldown", "transient_errors"))
        self.assertWithin(linkedin["cooldown_until"], hours=6)

    def test_abnormally_broad_empty_results_raise_a_coverage_warning_not_a_failure(self):
        for day in range(3):  # three normal days: most groups find something
            self.run_pull(FakeJobSpy(self.clock, lambda n, kw: one_row(n, kw) if n != 1 else ([], None)))
            self.clock.advance(days=1)
        jobspy = FakeJobSpy(self.clock, lambda n, kw: ([], None))
        code, status, out = self.run_pull(jobspy)
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((code, linkedin["state"], linkedin["reason"]), (0, "degraded", "coverage_warning"))
        self.assertNotIn("cooldown_until", linkedin)
        self.assertEqual(len(jobspy.linkedin_calls()), 16)
        self.assertIn("degraded, not clean", out)

    def test_coverage_needs_history_and_a_broad_gap(self):
        self.assertFalse(lp.coverage_warning([], 6, 6))
        history = [{"groups": 6, "empty": 1}] * 3
        self.assertTrue(lp.coverage_warning(history, 6, 5))
        self.assertFalse(lp.coverage_warning(history, 6, 3))
        self.assertFalse(lp.coverage_warning([{"groups": 6, "empty": 6}] * 3, 6, 6))  # empty is normal here

    def test_classifier(self):
        kinds = lambda text: lp.classify_jobspy_messages([text]).kind
        self.assertEqual(kinds("429 Response - Blocked by LinkedIn for too many requests"), "rate_limited")
        self.assertEqual(kinds("LinkedIn response status code 403 - x"), "auth")
        self.assertEqual(kinds("LinkedIn: Read timed out"), "transient")
        self.assertIsNone(lp.classify_jobspy_messages([]))


if __name__ == "__main__":
    unittest.main()
