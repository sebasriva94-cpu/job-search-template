"""Bright Data rate control: concurrency, pacing, budgets, cooldowns, no direct fetch of LinkedIn/Indeed."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.fakes import ManualClock, temp_gate
from scripts import rate_control as rc
from scripts import validate_listing as vl

LINKEDIN = "https://www.linkedin.com/jobs/view/123456"
INDEED = "https://www.indeed.com/viewjob?jk=abc"
EMPLOYER = "https://careers.example.com/jobs/42"
GOOD = "<html><body>IT Support Specialist Durham, NC</body></html>"


def page(status=200, html=GOOD, error=None, retry_after=None):
    return {"status": status, "final_url": "x", "html": html if status == 200 else "", "error": error,
            "retry_after": retry_after}


class BrightDataGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock()
        self.gate = temp_gate(self.tmp.name, rc.BRIGHTDATA, self.clock)
        patcher = mock.patch.object(vl.rate_control, "brightdata_gate", return_value=self.gate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, pages, url=EMPLOYER):
        queue = list(pages)
        with mock.patch.object(vl, "unlocker_get", side_effect=lambda *a, **k: queue.pop(0)) as fake:
            result = vl.controlled_unlocker_get(url)
        self.fake = fake
        return result

    def test_a_timeout_is_retried_once_unless_the_caller_declines_the_retry(self):
        # 2024-12-18: read timeouts on the Unlocker made the judge's text fetch cost 60 s plus the retry per row.
        timed_out = page(status=None, error="TimeoutError: The read operation timed out")
        result = self.get([timed_out, page()])
        self.assertEqual((self.fake.call_count, result.get("bd_unavailable")), (2, None))
        self.assertTrue(any(lo == vl.BD_RETRY_WAIT[0] for lo, _ in self.clock.uniform_calls))
        queue = [timed_out, page()]
        with mock.patch.object(vl, "unlocker_get", side_effect=lambda *a, **k: queue.pop(0)) as fake:
            result = vl.controlled_unlocker_get(EMPLOYER, timeout=25, retry_transient=False)
        self.assertEqual((fake.call_count, result["bd_unavailable"]), (1, True))
        self.assertEqual(fake.call_args.kwargs["timeout"], 25)

    def test_request_starts_are_paced_at_1_5_seconds(self):
        for _ in range(3):
            self.get([page()])
        self.assertEqual(self.clock.sleeps, [1.5, 1.5])

    def test_concurrency_never_exceeds_two(self):
        live, peak, lock = [0], [0], threading.Lock()

        def slow(*args, **kwargs):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.03)
            with lock:
                live[0] -= 1
            return page()

        self.gate.limiter.min_interval = 0
        with mock.patch.object(vl, "unlocker_get", side_effect=slow):
            threads = [threading.Thread(target=vl.controlled_unlocker_get, args=(EMPLOYER,)) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(peak[0], 2)

    def test_run_budget_is_60_then_requests_are_skipped(self):
        for _ in range(60):
            self.assertIsNone(self.get([page()]).get("bd_unavailable"))
        result = self.get([])
        self.assertTrue(result["bd_unavailable"])
        self.assertIn("run_budget", result["error"])
        self.fake.assert_not_called()

    def test_daily_budget_is_150_across_processes(self):
        self.gate.store.update("brightdata", lambda e: e["requests"].update(date=self.clock.now().date().isoformat(), today=150))
        self.assertIn("daily_budget", self.get([])["error"])

    def test_429_honours_retry_after(self):
        result = self.get([page(429, error="unlocker HTTP 429", retry_after="120")])
        self.assertTrue(result["bd_unavailable"])
        self.assertEqual(self.gate.summary("brightdata")["state"], "cooldown")
        self.clock.advance(seconds=119)
        self.assertTrue(self.get([])["bd_unavailable"])
        self.clock.advance(seconds=2)
        self.assertFalse(self.get([page()]).get("bd_unavailable"))

    def test_429_without_retry_after_is_a_15_minute_cooldown(self):
        self.get([page(429, error="unlocker HTTP 429")])
        self.clock.advance(minutes=14)
        self.assertTrue(self.get([]).get("bd_unavailable"))
        self.clock.advance(minutes=2)
        self.assertFalse(self.get([page()]).get("bd_unavailable"))

    def test_402_is_a_24_hour_cooldown_without_retry(self):
        self.get([page(402, error="unlocker HTTP 402")])
        self.assertEqual(self.fake.call_count, 1)
        summary = self.gate.summary("brightdata")
        self.assertEqual(summary["reason"], "quota_exceeded")
        self.clock.advance(hours=23)
        self.assertTrue(self.get([]).get("bd_unavailable"))
        self.clock.advance(hours=2)
        self.assertFalse(self.get([page()]).get("bd_unavailable"))

    def test_auth_failure_is_a_24_hour_configuration_failure(self):
        for code in (401, 403):
            self.setUp()
            self.get([page(code, error=f"unlocker HTTP {code}")])
            self.assertEqual(self.fake.call_count, 1)
            summary = self.gate.summary("brightdata")
            self.assertEqual((summary["state"], summary["reason"]), ("failed", "auth_failed"))

    def test_a_5xx_is_retried_once_after_20_to_40_seconds(self):
        result = self.get([page(503, error="unlocker HTTP 503"), page()])
        self.assertFalse(result.get("bd_unavailable"))
        self.assertEqual(self.fake.call_count, 2)
        self.assertIn((20, 40), self.clock.uniform_calls)

    def test_a_second_5xx_is_not_retried_again(self):
        result = self.get([page(503, error="x"), page(503, error="x")])
        self.assertTrue(result["bd_unavailable"])
        self.assertEqual(self.fake.call_count, 2)

    def test_a_404_is_an_answer_not_a_fault(self):
        self.assertEqual(self.get([page(404)])["status"], 404)
        self.assertEqual(self.gate.summary("brightdata")["state"], "ok")


class FetchListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = temp_gate(self.tmp.name, rc.BRIGHTDATA)
        for patcher in (mock.patch.object(vl.rate_control, "brightdata_gate", return_value=self.gate),
                        mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "t"}),
                        mock.patch.object(vl, "ats_fetch", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gate.cooldown("brightdata", seconds=900, reason="http_429")

    def test_linkedin_falls_back_to_scrapling_and_indeed_is_never_fetched_directly(self):
        # 2024-12-23: while Bright Data is unavailable a LinkedIn guest page goes through scrapling
        # (source "scrapling-direct"), never through the plain GET; Indeed is never fetched directly.
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(vl, "_http_get", side_effect=AssertionError("direct fetch")), \
                mock.patch.object(vl, "scrapling_get", return_value=page()) as scrapling, \
                mock.patch.object(vl.rate_control, "get_gate",
                                  return_value=temp_gate(state_dir, rc.LINKEDIN_DIRECT)):
            linkedin = vl.fetch_listing(LINKEDIN)
            indeed = vl.fetch_listing(INDEED)
        self.assertEqual(scrapling.call_count, 1)
        self.assertEqual(linkedin["source"], "scrapling-direct")
        self.assertFalse(linkedin["error"])
        self.assertTrue(indeed["error"])
        self.assertFalse(indeed["closed"])

    def test_an_employer_page_gets_exactly_one_direct_fallback(self):
        with mock.patch.object(vl, "_http_get", return_value=page()) as direct:
            listing = vl.fetch_listing(EMPLOYER)
        direct.assert_called_once()
        self.assertIn("IT Support", listing["text"])

    def test_provider_failure_gives_review_never_apply_now(self):
        row = {"title": "IT Support Specialist", "company": "Acme", "location": "Durham, NC",
               "job_url": LINKEDIN, "source": "linkedin"}
        with mock.patch.object(vl, "_http_get", side_effect=AssertionError("direct fetch")):
            verdict = vl.validate(row)["verdict"]
        self.assertNotEqual(verdict, "apply_now")


if __name__ == "__main__":
    unittest.main()
