"""Pipeline banner and dashboard behaviour when providers are throttled, paused or failing."""
import csv
import datetime as dt
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.fakes import ManualClock, temp_gate
from scripts import rate_control as rc

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = dt.datetime(2024, 12, 11, 7, 20, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
TODAY = NOW.date()


def load_module():
    spec = importlib.util.spec_from_file_location("job_dashboard", ROOT / "scripts" / "job_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(steps, **over):
    base = {"run_date": "2024-12-11", "started": "2024-12-11T07:15:00-04:00", "updated": "2024-12-11T07:19:00-04:00",
            "finished": "2024-12-11T07:19:00-04:00", "steps": steps, "alert": {"status": "none"}}
    base.update(over)
    return base


def step(name, state="ok", exit=0, **providers):
    return {"name": name, "exit": exit, "state": state, "providers": providers}


LINKEDIN_429 = {"state": "cooldown", "reason": "http_429", "cooldown_until": "2024-12-12T00:30:00-04:00",
                "consecutive_degraded_runs": 1}


class BannerTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def banner(self, steps, **over):
        return self.m.pipeline_banner(status(steps, **over), NOW)

    def test_healthy_providers_are_green(self):
        banner = self.banner([step("pull", linkedin={"state": "ok", "reason": "", "consecutive_degraded_runs": 0}),
                              step("render")])
        self.assertEqual(banner["state"], "ok")
        self.assertIn("steps clean", banner["headline"])

    def test_old_status_without_states_is_still_green(self):
        self.assertEqual(self.banner([{"name": "pull", "exit": 0}, {"name": "render", "exit": 0}])["state"], "ok")

    def test_an_intentional_cooldown_is_amber_and_readable(self):
        banner = self.banner([step("pull", "degraded", linkedin=LINKEDIN_429), step("target"), step("render")])
        self.assertEqual(banner["state"], "warn")
        self.assertIn("LinkedIn paused", banner["headline"])
        self.assertIn("rate limited (HTTP 429)", banner["headline"])
        self.assertIn("not a clean run", banner["headline"])
        self.assertNotIn("steps clean", banner["headline"])
        line = next(d for d in banner["details"] if d.startswith("LinkedIn"))
        self.assertIn("paused until Thu Dec 12 00:30", line)
        self.assertIn("other sources ran", line)

    def test_skipped_and_degraded_providers_are_amber(self):
        banner = self.banner([step("render", "degraded", brightdata={"state": "skipped", "reason": "daily_budget",
                                                                     "consecutive_degraded_runs": 1})])
        self.assertEqual(banner["state"], "warn")
        self.assertIn("daily request budget used", banner["headline"])

    def test_a_provider_degraded_two_runs_in_a_row_is_red(self):
        twice = dict(LINKEDIN_429, consecutive_degraded_runs=2)
        banner = self.banner([step("pull", "degraded", linkedin=twice), step("target")])
        self.assertEqual(banner["state"], "bad")
        self.assertIn("2 runs in a row", banner["headline"])

    def test_a_configuration_failure_is_red(self):
        failed = {"state": "failed", "reason": "auth_failed", "consecutive_degraded_runs": 1,
                  "cooldown_until": "2024-12-12T07:00:00-04:00"}
        banner = self.banner([step("render", "failed", brightdata=failed)])
        self.assertEqual(banner["state"], "bad")
        self.assertIn("credentials rejected", banner["headline"])

    def test_a_local_failure_is_red(self):
        banner = self.banner([step("pull", "failed", exit=127, error="Python preflight failed")])
        self.assertEqual(banner["state"], "bad")

    def test_all_discovery_sources_unavailable_is_red(self):
        down = {"state": "cooldown", "reason": "http_429", "consecutive_degraded_runs": 1}
        banner = self.banner([step("pull", "degraded", linkedin=down), step("target", "degraded", workday=down),
                              step("render")])
        self.assertEqual(banner["state"], "bad")
        self.assertIn("all discovery sources unavailable", banner["headline"])

    def test_one_throttled_host_family_among_healthy_ones_is_not_all_down(self):
        banner = self.banner([step("pull", "degraded", linkedin=LINKEDIN_429),
                              step("target", "degraded", workday=LINKEDIN_429,
                                   greenhouse={"state": "ok", "reason": "", "consecutive_degraded_runs": 0})])
        self.assertEqual(banner["state"], "warn")

    def test_sensitive_text_never_reaches_the_banner(self):
        secret = "pass" + "word=hunter2 https://api.example.com/x?key=abc"
        banner = self.banner([step("pull", "degraded", linkedin={"state": "degraded", "reason": secret,
                                                                  "consecutive_degraded_runs": 1})])
        text = json.dumps(banner)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("api.example.com", text)


class RenderWithBrightDataDownTests(unittest.TestCase):
    def test_dashboard_renders_and_listings_stay_in_review(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for sub in ("dashboard", "weekly-sweeps", "automation-logs"):
                (root / sub).mkdir()
            (root / "dashboard" / "job-dashboard-data.json").write_text(
                json.dumps({"last_validated": "2024-12-10", "apply": [], "skip": []}), encoding="utf-8")
            (root / "automation-logs" / "pipeline-status.json").write_text(json.dumps(status([
                step("render", "degraded", brightdata={"state": "cooldown", "reason": "http_429",
                                                       "consecutive_degraded_runs": 1})])), encoding="utf-8")
            row = {"title": "IT Support Specialist", "company": "NewCo", "location": "Durham, NC", "date_posted": "2024-12-11",
                   "is_remote": "false", "job_url": "https://www.linkedin.com/jobs/view/77", "site": "linkedin"}
            with (root / "weekly-sweeps" / "linkedin-raw-2024-12-11.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            clock = ManualClock()
            gate = temp_gate(d, rc.BRIGHTDATA, clock)
            gate.cooldown("brightdata", seconds=900, reason="http_429")
            # 2024-12-23: the LinkedIn scrapling fallback is paused too (a refusal cooldown), so nothing
            # can be checked and the row must stay in review; no network, no real provider state.
            paused = lambda url, timeout=30: m.validate_listing._bd_unavailable(  # noqa: E731
                url, "LinkedIn direct fetch paused (http_429)")
            with mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "t"}), \
                    mock.patch.object(rc, "brightdata_gate", return_value=gate), \
                    mock.patch.object(m.validate_listing, "_http_get", side_effect=AssertionError("direct fetch")), \
                    mock.patch.object(m.validate_listing, "controlled_scrapling_get", side_effect=paused), \
                    mock.patch.object(m.employer_hq, "hq_state", return_value=None):
                result = m.build_dashboard(root, check_links=False,
                                           queue_checker=m.validate_candidate, today=TODAY, now=NOW)
            html = (root / "job-dashboard.html").read_text(encoding="utf-8")
            self.assertEqual(result["pipeline_state"], "warn")
            self.assertEqual(result.get("apply_now_count", 0), 0)
            self.assertIn("NewCo", html)
            self.assertIn("not a clean run", html)


if __name__ == "__main__":
    unittest.main()
