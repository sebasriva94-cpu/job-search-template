"""2024-12-17: the two LinkedIn lanes. --no-linkedin is the fast lane the 00:30 chain runs (Indeed and the
APIs); --trickle is the LinkedIn lane the 01:00 task runs: long gaps, escalating 429 pauses that never end
the night, and the posting text fetched from the public job page for rows that pass the gates."""
import contextlib
import csv
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.test_linkedin_rate_control import FakeJobSpy, LinkedInTestCase, lp, one_row, rc

LD_PAGE = ('<script type="application/ld+json">{"@type":"JobPosting","description":"Provide Tier 2 support for 600 '
           'employees across Windows and macOS. Manage devices in Intune and Jamf Pro. Administer Okta. Requirements: '
           'three years of IT support experience. Salary 70000 to 85000 a year. '
           + "More text about the role. " * 12 + '"}</script>')


def home_state_row(n, kwargs):
    return [{"title": "IT Support Specialist", "company": f"Acme {n}", "location": "Durham, NC",
             "job_url": f"https://www.linkedin.com/jobs/view/44796384{n:02d}"}], None


class LaneTests(LinkedInTestCase):
    def run_lane(self, argv, jobspy, fetch_detail=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lp.main(argv, scrape=jobspy, gate=self.gate(), fetch_detail=fetch_detail)
        line = next(l for l in out.getvalue().splitlines() if l.startswith(rc.STATUS_PREFIX))
        return code, json.loads(line[len(rc.STATUS_PREFIX):]), out.getvalue()

    def probe_lines(self):
        files = list((Path(self._tmp.name) / "automation-logs").glob("linkedin-probe-*.jsonl"))
        return [json.loads(l) for f in files for l in f.read_text(encoding="utf-8").splitlines()]

    def test_no_linkedin_runs_indeed_and_the_apis_only(self):
        jobspy = FakeJobSpy(self.clock, one_row)
        code, status, out = self.run_lane(["100", "--no-linkedin"], jobspy)
        self.assertEqual(code, 0)
        self.assertEqual(jobspy.linkedin_calls(), [])
        self.assertEqual(len([c for c in jobspy.calls if c["site"] == "indeed"]), 21)  # 5 local + 5 Minneapolis + 11 remote
        self.assertEqual(status["providers"], {})
        self.assertIn("not in this lane", out)
        self.assertEqual(self.probe_lines(), [])

    def test_trickle_runs_linkedin_only_with_long_gaps(self):
        jobspy = FakeJobSpy(self.clock, one_row)
        code, status, out = self.run_lane(["100", "--trickle"], jobspy)
        self.assertEqual((code, status["providers"]["linkedin"]["state"]), (0, "ok"))
        self.assertEqual(len(jobspy.linkedin_calls()), 16)
        self.assertEqual([c for c in jobspy.calls if c["site"] == "indeed"], [])
        self.assertIn(lp.TRICKLE_GROUP_GAP, self.clock.uniform_calls)
        self.assertNotIn(lp.LINKEDIN_GROUP_GAP, self.clock.uniform_calls)
        self.assertIn("LinkedIn trickle: 16 groups, 0 pause(s)", out)
        self.assertTrue(all(l["lane"] == "trickle" for l in self.probe_lines()))

    def test_trickle_429s_pause_15_30_60_minutes_and_never_end_the_night(self):
        def behave(n, kwargs):
            # LinkedIn calls 2 to 6 are refused: group 1 twice (deferred), group 2 twice (deferred), group 3 once (retry ok)
            return ([], "429 Response - Blocked by LinkedIn for too many requests") if n in (2, 3, 4, 5, 6) else one_row(n, kwargs)
        jobspy = FakeJobSpy(self.clock, behave)
        code, status, out = self.run_lane(["100", "--trickle"], jobspy)
        self.assertEqual(code, 0)
        for pause in lp.TRICKLE_429_PAUSES:
            self.assertIn(pause, self.clock.sleeps)
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((status["state"], linkedin["state"]), ("degraded", "degraded"))
        self.assertNotIn("cooldown_until", linkedin)        # a 429 never ends the night any more
        self.assertEqual(self.entry()["cooldown_until"], "")
        self.assertIn("group deferred", out)
        self.assertEqual(len(jobspy.linkedin_calls()), 19)  # 16 groups, two deferred after a retry, one retried fine
        self.assertEqual(self.entry().get("rotation_cursor"), 1)  # the first deferred group is where tomorrow starts
        self.assertIn("3 pause(s) on 429", out)

    def test_trickle_stops_starting_groups_when_its_time_budget_is_spent_and_defers_the_rest(self):
        # 2024-12-18, first night: the lane hit the step's 300-minute cap at 06:00 inside a 60-minute pause and
        # the night read "failed" with 13 groups landed. The lane now keeps its own budget. One hour here with
        # 12-minute gaps fits five groups; the sixth would end past the hour, so it and the rest are deferred.
        jobspy = FakeJobSpy(self.clock, one_row)
        with mock.patch.object(lp, "TRICKLE_BUDGET", 60 * 60):
            code, status, out = self.run_lane(["100", "--trickle"], jobspy)
        self.assertEqual(code, 0)
        self.assertEqual(len(jobspy.linkedin_calls()), 5)
        self.assertLessEqual(self.clock.monotonic() - 1000.0, 60 * 60)  # nothing ran past the budget
        self.assertIn("LinkedIn trickle: 5 groups, 0 pause(s) on 429, 0 posting text(s) fetched, 11 group(s) deferred on the time budget", out)
        self.assertEqual(out.count("time budget spent"), 1)  # said once, not for every deferred group
        linkedin = status["providers"]["linkedin"]
        self.assertEqual((status["state"], linkedin["state"], linkedin["reason"]), ("degraded", "degraded", "time_budget"))
        self.assertEqual(self.entry().get("rotation_cursor"), 5)  # tomorrow starts where the budget stopped
        self.assertEqual(self.entry()["requests"]["today"], 5)    # deferred groups cost no daily-budget unit

    def test_trickle_defers_a_group_whose_429_pause_would_cross_the_budget_instead_of_sleeping(self):
        def behave(n, kwargs):
            return ([], "429 Response - Blocked by LinkedIn for too many requests") if n == 4 else one_row(n, kwargs)
        jobspy = FakeJobSpy(self.clock, behave)
        with mock.patch.object(lp, "TRICKLE_BUDGET", 50 * 60):  # group 3 is refused at minute 36; its 15-minute pause would end past 50
            code, status, out = self.run_lane(["100", "--trickle"], jobspy)
        self.assertEqual(code, 0)
        self.assertEqual(len(jobspy.linkedin_calls()), 4)      # groups 0 to 3 once each, no retry of the refused group
        self.assertNotIn(900, self.clock.sleeps)
        self.assertIn("13 group(s) deferred on the time budget", out)
        self.assertEqual(status["providers"]["linkedin"]["reason"], "http_429_paused")  # the first degradation names the 429
        self.assertEqual(self.entry().get("rotation_cursor"), 3)

    def test_trickle_fetches_posting_text_for_gate_passers_and_stops_on_a_429(self):
        pages = []

        def fetch_detail(url):
            pages.append(url)
            if len(pages) in (3, 4):
                return {"status": 429, "html": "", "final_url": url, "error": "HTTP 429"}
            return {"status": 200, "html": LD_PAGE, "final_url": url, "error": None}

        jobspy = FakeJobSpy(self.clock, home_state_row)
        code, status, out = self.run_lane(["100", "--trickle"], jobspy, fetch_detail=fetch_detail)
        self.assertEqual(code, 0)
        self.assertEqual(len(pages), 4)   # two texts, a 429 (paused until the next group), a second 429 (stopped)
        self.assertTrue(all(u.startswith("https://www.linkedin.com/jobs/view/44796384") for u in pages))
        with_text = [r for r in self.saved if r.get("description")]
        self.assertEqual(len(with_text), 2)
        self.assertIn("Manage devices in Intune and Jamf Pro", with_text[0]["description"])
        self.assertIn("2 posting text(s) fetched, detail fetches stopped: rate_limited", out)
        self.assertIn(lp.linkedin_detail.DETAIL_GAP, self.clock.uniform_calls)
        details = [l for l in self.probe_lines() if l["kind"] == "detail"]
        self.assertEqual([d["outcome"] for d in details], ["ok", "ok", "rate_limited", "rate_limited"])
        self.assertEqual(status["providers"]["linkedin"]["reason"], "detail_rate_limited")

    def test_one_detail_429_pauses_the_fetches_until_the_next_group_not_for_the_night(self):
        # 2024-12-18: the 47th job-page fetch got a 429 at 01:40 and no remote group fetched a page all night.
        pages = []

        def fetch_detail(url):
            pages.append(url)
            if len(pages) == 2:
                return {"status": 429, "html": "", "final_url": url, "error": "HTTP 429"}
            return {"status": 200, "html": LD_PAGE, "final_url": url, "error": None}

        def two_rows(n, kwargs):  # the second row of group 1 is never tried: the group is paused after its 429
            return [{"title": "IT Support Specialist", "company": f"Acme {n}", "location": "Durham, NC",
                     "job_url": f"https://www.linkedin.com/jobs/view/4480{n:03d}"},
                    {"title": "Systems Administrator", "company": f"Beta {n}", "location": "Durham, NC",
                     "job_url": f"https://www.linkedin.com/jobs/view/4481{n:03d}"}], None

        jobspy = FakeJobSpy(self.clock, two_rows)
        code, status, out = self.run_lane(["100", "--trickle"], jobspy, fetch_detail=fetch_detail)
        self.assertEqual(code, 0)
        self.assertEqual(len(pages), 1 + 1 + 15 * 2)         # group 1: one fetch then the pause; groups 2 to 16 in full
        self.assertEqual(len([r for r in self.saved if r.get("description")]), 31)
        self.assertIn("31 posting text(s) fetched, detail fetches paused once on a 429", out)
        self.assertEqual(status["providers"]["linkedin"]["reason"], "detail_rate_limited")

    def test_detail_fetches_skip_role_keys_already_in_the_csv_or_fetched_this_run(self):
        # 2024-12-18: 46 fetches produced 16 texts; the five local terms return the same postings, and the merge
        # keeps the first row per role key, so every repeat fetch landed nowhere.
        path = lp.daily_csv(Path(self._tmp.name))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=lp.CSV_COLUMNS)
            writer.writeheader()
            writer.writerow({"title": "IT Support Specialist", "company": "Already Here", "location": "Durham, NC", "site": "linkedin",
                             "job_url": "https://www.linkedin.com/jobs/view/4470000001"})
        pages = []

        def fetch_detail(url):
            pages.append(url)
            return {"status": 200, "html": LD_PAGE, "final_url": url, "error": None}

        def repeats(n, kwargs):
            return [{"title": "IT Support Specialist", "company": "Already Here", "location": "Durham, NC",   # in the CSV
                     "job_url": f"https://www.linkedin.com/jobs/view/4470000{n:03d}"},
                    {"title": "Help Desk Technician", "company": "Every Term", "location": "Durham, NC",     # same key each group
                     "job_url": f"https://www.linkedin.com/jobs/view/4471000{n:03d}"},
                    {"title": "Help Desk Technician", "company": "Every Term", "location": "Raleigh, NC",  # same key, other URL
                     "job_url": f"https://www.linkedin.com/jobs/view/4472000{n:03d}"},
                    {"title": "IT Support Specialist", "company": f"Acme {n}", "location": "Durham, NC",
                     "job_url": f"https://www.linkedin.com/jobs/view/4473000{n:03d}"}], None

        jobspy = FakeJobSpy(self.clock, repeats)
        code, status, out = self.run_lane(["100", "--trickle"], jobspy, fetch_detail=fetch_detail)
        self.assertEqual(code, 0)
        self.assertEqual(len(pages), 1 + 16)                  # Every Term once, each Acme once, Already Here never
        self.assertEqual(len(set(pages)), len(pages))
        self.assertIn("17 posting text(s) fetched", out)
        self.assertEqual(status["providers"]["linkedin"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
