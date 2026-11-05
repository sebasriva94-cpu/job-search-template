"""Public ATS rate control: host-keyed cooldowns, pacing, retries, circuit breaker, degraded sweep."""
import contextlib
import datetime as dt
import io
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.fakes import ManualClock, http_error, temp_gate
from scripts import rate_control as rc
from scripts import target_sweep as ts

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
WORKDAY_A = "https://exampleuniversity.wd1.myworkdayjobs.com/wday/cxs/exampleuniversity/Site/jobs"
WORKDAY_B = "https://example homes.wd1.myworkdayjobs.com/wday/cxs/example homes/Site/jobs"


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock()
        self.gates = {p.name: temp_gate(self.tmp.name, p, self.clock) for p in (rc.ATS_FAST, rc.ATS_SLOW)}
        patcher = mock.patch.object(ts.rate_control, "get_gate", side_effect=lambda policy: self.gates[policy.name])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fetch(self, url, *effects):
        queue = list(effects)

        def fake(request, timeout=None):
            self.calls.append(request.full_url)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return Reply(json.dumps(item).encode())

        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=fake):
            return ts.get_json(url)

    def state(self, family, host):
        gate = self.gates["ats-slow" if family in ts.SLOW_FAMILIES.values() else "ats-fast"]
        return gate.summary(f"{family}:{host}")

    def test_policies_match_the_spec(self):
        self.assertEqual((rc.ATS_FAST.max_concurrency, rc.ATS_FAST.per_host, rc.ATS_FAST.host_interval), (4, 1, 0.25))
        self.assertEqual((rc.ATS_SLOW.max_concurrency, rc.ATS_SLOW.per_host, rc.ATS_SLOW.host_interval), (2, 1, 1.0))
        for url, family in ((GREENHOUSE, "greenhouse"), (WORKDAY_A, "workday"),
                            ("https://recruiting.ultipro.com/x", "ultipro"), ("https://careers-x.icims.com/jobs", "icims"),
                            ("https://nations.breezy.hr/json", "breezy"), ("https://api.ashbyhq.com/x", "ashby"),
                            ("https://api.smartrecruiters.com/v1", "smartrecruiters"), ("https://apply.workable.com/a", "workable")):
            self.assertEqual(ts.provider_of(url)[0], family)

    def test_same_host_starts_are_paced_per_family(self):
        self.fetch(GREENHOUSE, {"jobs": []})
        self.fetch(GREENHOUSE, {"jobs": []})
        self.assertEqual(self.clock.sleeps, [0.25])
        self.clock.sleeps.clear()
        self.fetch(WORKDAY_A, {})
        self.fetch(WORKDAY_A, {})
        self.assertEqual(self.clock.sleeps, [1.0])

    def test_icims_html_is_paced_at_1_5_seconds(self):
        def fake(request, timeout=None):
            return Reply(b"<html></html>")
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=fake):
            ts.get_text("https://careers-x.icims.com/jobs/search?pr=0")
            ts.get_text("https://careers-x.icims.com/jobs/search?pr=1")
        self.assertEqual(self.clock.sleeps, [1.5])

    def test_same_host_requests_never_overlap(self):
        live, peak, lock = [0], [0], threading.Lock()

        def slow(request, timeout=None):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.03)
            with lock:
                live[0] -= 1
            return Reply(b"{}")

        self.gates["ats-fast"].limiter.host_interval = 0
        with mock.patch.object(ts.urllib.request, "urlopen", side_effect=slow):
            threads = [threading.Thread(target=ts.get_json, args=(GREENHOUSE,)) for _ in range(5)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(peak[0], 1)

    def test_429_with_retry_after_pauses_only_that_host(self):
        with self.assertRaises(Exception):
            self.fetch(WORKDAY_A, http_error(429, "90"))
        self.assertEqual(self.state("workday", "exampleuniversity.wd1.myworkdayjobs.com")["state"], "cooldown")
        with self.assertRaises(rc.ProviderUnavailable):
            self.fetch(WORKDAY_A)
        self.assertEqual(self.fetch(WORKDAY_B, {"ok": 1}), {"ok": 1})   # another tenant
        self.assertEqual(self.fetch(GREENHOUSE, {"jobs": []}), {"jobs": []})  # another provider
        self.clock.advance(seconds=91)
        self.assertEqual(self.fetch(WORKDAY_A, {"ok": 2}), {"ok": 2})

    def test_429_without_a_header_is_15_minutes(self):
        with self.assertRaises(Exception):
            self.fetch(GREENHOUSE, http_error(429))
        until = rc.parse_time(self.state("greenhouse", "boards-api.greenhouse.io")["cooldown_until"])
        self.assertEqual(until - self.clock.now(), dt.timedelta(minutes=15))
        self.assertEqual(len(self.calls), 1)

    def test_404_and_410_are_not_retried_and_not_a_provider_fault(self):
        for code in (404, 410):
            with self.assertRaises(Exception):
                self.fetch(GREENHOUSE, http_error(code))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.state("greenhouse", "boards-api.greenhouse.io")["state"], "ok")

    def test_403_blocks_the_host_for_6_hours_without_hammering(self):
        with self.assertRaises(Exception):
            self.fetch(WORKDAY_A, http_error(403))
        until = rc.parse_time(self.state("workday", "exampleuniversity.wd1.myworkdayjobs.com")["cooldown_until"])
        self.assertEqual(until - self.clock.now(), dt.timedelta(hours=6))
        with self.assertRaises(rc.ProviderUnavailable):
            self.fetch(WORKDAY_A)
        self.assertEqual(len(self.calls), 1)

    def test_one_5xx_is_retried_once_after_5_to_15_seconds(self):
        self.assertEqual(self.fetch(GREENHOUSE, http_error(503), {"jobs": [1]}), {"jobs": [1]})
        self.assertEqual(len(self.calls), 2)
        self.assertIn((5, 15), self.clock.uniform_calls)
        with self.assertRaises(Exception):
            self.fetch(GREENHOUSE, http_error(503), http_error(503))
        self.assertEqual(len(self.calls), 4)  # a second failure is not retried again

    def test_a_timeout_is_retried_once(self):
        self.assertEqual(self.fetch(GREENHOUSE, TimeoutError("slow"), {"jobs": []}), {"jobs": []})
        self.assertEqual(len(self.calls), 2)

    def test_three_transient_failures_in_10_minutes_open_a_30_minute_circuit(self):
        with self.assertRaises(Exception):
            self.fetch(GREENHOUSE, http_error(503), http_error(503))  # two failures
        with self.assertRaises(Exception):
            self.fetch(GREENHOUSE, http_error(503))                   # the third opens the circuit, no retry
        self.assertEqual(len(self.calls), 3)
        summary = self.state("greenhouse", "boards-api.greenhouse.io")
        self.assertEqual(summary["reason"], "circuit_open")
        self.assertAlmostEqual((rc.parse_time(summary["cooldown_until"]) - self.clock.now()).total_seconds(), 1800, delta=1)
        with self.assertRaises(rc.ProviderUnavailable):
            self.fetch(GREENHOUSE)

    def test_failures_spread_over_more_than_10_minutes_do_not_open_it(self):
        for _ in range(3):
            with self.assertRaises(Exception):
                self.fetch(GREENHOUSE, http_error(503), http_error(503))
            self.clock.advance(minutes=11)
        self.assertNotEqual(self.state("greenhouse", "boards-api.greenhouse.io")["reason"], "circuit_open")

    def test_workday_keeps_partial_results_when_throttled_midway(self):
        calls = []

        def post(url, body):
            calls.append(body["searchText"])
            if len(calls) == 3:
                raise http_error(429)
            return {"jobPostings": [{"externalPath": f"/job/{len(calls)}", "title": "IT Support", "locationsText": "Durham, NC"}]}

        with mock.patch.object(ts, "post_json", side_effect=post):
            jobs = ts.fetch_workday("h", "t", "s")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len(calls), 3)

    def test_a_first_failure_in_workday_still_raises(self):
        with mock.patch.object(ts, "post_json", side_effect=http_error(500)), self.assertRaises(Exception):
            ts.fetch_workday("h", "t", "s")


class SweepStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock()
        self.gates = {p.name: temp_gate(self.tmp.name, p, self.clock) for p in (rc.ATS_FAST, rc.ATS_SLOW)}
        for patcher in (mock.patch.object(ts.rate_control, "get_gate", side_effect=lambda p: self.gates[p.name]),
                        mock.patch.object(rc, "_GATES", {("x", "y"): self.gates["ats-fast"], ("z", "y"): self.gates["ats-slow"]}),
                        mock.patch.object(ts, "merge_rows", return_value={"added": 0, "total": 0, "path": "p"}),
                        mock.patch.object(ts, "project_root", return_value=self.tmp.name)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, boards):
        out = io.StringIO()
        with mock.patch.object(ts, "boards", return_value=boards), contextlib.redirect_stdout(out):
            code = ts.main()
        text = out.getvalue()
        line = next(l for l in text.splitlines() if l.startswith(rc.STATUS_PREFIX))
        return code, json.loads(line[len(rc.STATUS_PREFIX):]), text

    def board(self, url, effect):
        def fetch():
            reply = (lambda *a, **k: Reply(json.dumps(effect).encode())) if not isinstance(effect, Exception) else effect
            with mock.patch.object(ts.urllib.request, "urlopen", side_effect=reply):
                return ts.get_json(url)
        return ("Co " + url[8:14], fetch, lambda jobs: [])

    def test_one_throttled_provider_leaves_the_sweep_degraded_but_usable(self):
        code, status, text = self.run_main([self.board(WORKDAY_A, http_error(429, "600")),
                                            self.board(GREENHOUSE, {"jobs": []})])
        self.assertEqual(code, 0)
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["providers"]["workday"]["state"], "cooldown")
        self.assertEqual(status["providers"]["greenhouse"]["state"], "ok")
        self.assertIn("degraded", text)

    def test_a_paused_host_is_reported_as_skipped_not_failed(self):
        self.gates["ats-slow"].cooldown("workday:exampleuniversity.wd1.myworkdayjobs.com", seconds=600, reason="http_429")
        code, status, text = self.run_main([self.board(WORKDAY_A, {}), self.board(GREENHOUSE, {"jobs": []})])
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED", text)

    def test_no_provider_succeeding_fails_the_sweep(self):
        code, status, _ = self.run_main([self.board(WORKDAY_A, http_error(429)), self.board(GREENHOUSE, http_error(403))])
        self.assertEqual(code, 1)
        self.assertNotEqual(status["state"], "ok")


if __name__ == "__main__":
    unittest.main()
