"""The Bright Data LinkedIn feed (2024-12-18): the 01:00 lane that replaced the guest-endpoint trickle."""
import contextlib
import csv
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import ManualClock, temp_gate
from scripts import linkedin_feed as feed
from scripts import rate_control as rc
from scripts.sweep_common import CSV_COLUMNS

TODAY = dt.date(2024, 12, 18)


def record(**over):
    base = {
        "url": "https://www.linkedin.com/jobs/view/it-specialist-at-hedsouth-4481706394?_l=en",
        "job_posting_id": "4481706394", "job_title": "IT Specialist",
        "company_name": "Example AV Integrators", "job_location": "Holly Springs, NC",
        "job_summary": "Department: IT. Compensation: $80,000 - $90,000 / year. Sole IT owner for a growing company. " * 4,
        "job_description_formatted": "<section><div>Department: IT.</div></section>",
        "job_posted_date": "2024-12-17T17:56:14.690Z", "job_employment_type": "Full-time",
        "base_salary": {"min_amount": 80000, "max_amount": 90000, "currency": "$", "payment_period": "yr"},
        "discovery_input": {"location": "Raleigh, NC", "keyword": "IT Specialist", "remote": ""},
    }
    base.update(over)
    return base


class FakeApi:
    """Bright Data's trigger / progress / snapshot endpoints, scripted."""

    def __init__(self, records, trigger=(200, {"snapshot_id": "sd_test"}), progress=("running", "ready")):
        self.records, self.trigger, self.progress = records, trigger, list(progress)
        self.calls = []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if "/trigger" in url:
            return self.trigger
        if "/progress/" in url:
            return 200, {"status": self.progress.pop(0) if len(self.progress) > 1 else self.progress[0]}
        if "/snapshot/" in url:
            return 200, self.records
        raise AssertionError(url)


class MappingTests(unittest.TestCase):
    def test_a_record_becomes_a_pull_row_with_text_pay_and_a_clean_link(self):
        row = feed.record_to_row(record())
        self.assertEqual(sorted(row), sorted(CSV_COLUMNS))
        self.assertEqual((row["title"], row["company"], row["location"], row["date_posted"]),
                         ("IT Specialist", "Example AV Integrators", "Holly Springs, NC", "2024-12-17"))
        self.assertEqual((row["min_amount"], row["max_amount"], row["pay_interval"]), ("80000", "90000", "yearly"))
        self.assertEqual((row["is_remote"], row["site"]), ("false", "linkedin"))
        # 2024-12-19: the id form, like every other lane (repost history keys links by exact URL)
        self.assertEqual(row["job_url"], "https://www.linkedin.com/jobs/view/4481706394")
        slug_only = feed.record_to_row(record(job_posting_id=""))
        self.assertEqual(slug_only["job_url"], "https://www.linkedin.com/jobs/view/4481706394")
        self.assertIn("Sole IT owner", row["description"])

    def test_hourly_pay_is_yearly_with_the_published_interval_and_remote_rows_say_so(self):
        row = feed.record_to_row(record(job_location="Chicago, IL", base_salary={"min_amount": 25, "max_amount": 30, "payment_period": "hr"},
                                        discovery_input={"location": "United States", "keyword": "IT Specialist", "remote": "Remote"}))
        self.assertEqual((row["min_amount"], row["max_amount"], row["pay_interval"]), ("52000", "62400", "hourly"))
        self.assertEqual((row["is_remote"], row["location"]), ("true", "Remote (Chicago, IL)"))
        bare = feed.record_to_row(record(base_salary=None, job_summary="", job_description_formatted="<p>Only the <b>markup</b> text</p>"))
        self.assertEqual((bare["min_amount"], bare["pay_interval"], bare["description"]), ("", "", "Only the markup text"))

    def test_the_inputs_mirror_the_pull_groups(self):
        groups = feed.inputs("Past week", ["1", "2"])
        self.assertEqual(len(groups), 21)  # 5 Raleigh + 5 Minneapolis + 11 remote
        self.assertEqual(sum(1 for g in groups if g["remote"] == "Remote"), 11)
        self.assertTrue(all(g["jobs_to_not_include"] == ["1", "2"] and g["country"] == "US" for g in groups))
        self.assertEqual(groups[0], {"location": "Raleigh, NC", "keyword": "IT Specialist", "country": "US",
                                     "time_range": "Past week", "remote": "", "jobs_to_not_include": ["1", "2"]})


class FeedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()
        self.clock = ManualClock(start=dt.datetime(2024, 12, 18, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4))))
        self.gate = temp_gate(str(self.root / "dashboard"), rc.BRIGHTDATA_FEED, self.clock)
        self.saved = []
        patches = [mock.patch.object(feed, "merge_rows", side_effect=lambda rows, root: self.saved.extend(rows) or {"added": len(rows), "path": "x", "total": len(rows)}),
                   mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "test-token", "BRIGHTDATA_FEED_PRICE_PER_1K": "2.0"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def csv(self, day, urls):
        with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for url in urls:
                writer.writerow({"title": "IT Specialist", "company": f"Co {url[-3:]}", "job_url": url, "site": "linkedin"})

    def run_feed(self, api, argv=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = feed.main(list(argv), http_call=api, gate=self.gate, root=self.root)
        line = next((l for l in out.getvalue().splitlines() if l.startswith(rc.STATUS_PREFIX)), None)
        return code, (json.loads(line[len(rc.STATUS_PREFIX):]) if line else None), out.getvalue()

    def test_a_night_triggers_once_excludes_known_ids_and_merges_unique_rows_with_text(self):
        self.csv("2024-12-17", ["https://www.linkedin.com/jobs/view/4470000001", "https://www.indeed.com/viewjob?jk=abc"])
        self.csv("2024-10-29", ["https://www.linkedin.com/jobs/view/4460000009"])  # older than the exclusion window
        api = FakeApi([record(), record(url="https://www.linkedin.com/jobs/view/4481706394?trk=x"),  # the same posting twice
                       record(job_posting_id="4481700001", url="https://www.linkedin.com/jobs/view/4481700001",
                              company_name="Abacus", job_title="Engineer, Enterprise Service Desk", base_salary=None),
                       {"error": "page not found", "input": {"url": "https://www.linkedin.com/jobs/view/1"}}])
        code, status, out = self.run_feed(api)
        self.assertEqual(code, 0)
        method, url, body = api.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn(f"dataset_id={feed.DATASET_ID}&type=discover_new&discover_by=keyword&limit_per_input=14", url)
        self.assertEqual((len(body["input"]), body["limit_per_input"]), (21, 14))  # 294, inside the 300 cap
        self.assertEqual(body["input"][0]["jobs_to_not_include"], ["4470000001"])
        self.assertEqual(body["input"][0]["time_range"], "Past week")  # first night
        self.assertEqual([m for m, u, _ in api.calls if "/progress/" in u], ["GET", "GET"])
        self.assertEqual(self.clock.sleeps, [feed.POLL_SECONDS])
        self.assertEqual([r["company"] for r in self.saved], ["Example AV Integrators", "Abacus"])
        self.assertTrue(all(r["description"] for r in self.saved))
        self.assertIn("4 records (1 with errors), 2 unique postings, 2 new in the CSV, 2 with text", out)
        self.assertIn("294 at most, about $0.59", out)
        self.assertEqual(status, {"state": "ok", "providers": {"brightdata_feed": {"state": "ok", "reason": "", "consecutive_degraded_runs": 0}}})
        self.assertEqual(self.gate.store.get(feed.KEY)["requests"]["today"], 294)
        log = (self.root / "automation-logs" / "linkedin-feed-2024-12.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(log[0])
        self.assertEqual((entry["records"], entry["unique"], entry["added"], entry["with_text"], entry["excluded"]), (4, 2, 2, 2, 1))
        self.assertEqual(entry["est_dollars"], 0.008)

    def test_a_second_run_the_same_day_is_refused_by_the_record_cap_and_exits_clean(self):
        api = FakeApi([record()])
        self.assertEqual(self.run_feed(api)[0], 0)
        api2 = FakeApi([record()])
        code, status, out = self.run_feed(api2)
        self.assertEqual((code, api2.calls), (0, []))
        self.assertEqual(status["providers"]["brightdata_feed"]["state"], "skipped")
        self.assertIn("daily_budget", status["providers"]["brightdata_feed"]["reason"])
        self.assertIn("already spent", out)

    def test_after_a_run_the_window_narrows_to_the_past_day(self):
        self.assertEqual(self.run_feed(FakeApi([record()]))[0], 0)
        self.clock.advance(days=1)
        api = FakeApi([record()])
        self.run_feed(api)
        self.assertEqual(api.calls[0][2]["input"][0]["time_range"], "Past 24 hours")
        self.clock.advance(days=5)  # a gap: back to the week
        api = FakeApi([record()])
        self.run_feed(api)
        self.assertEqual(api.calls[0][2]["input"][0]["time_range"], "Past week")

    def test_the_exclusion_window_is_exactly_the_last_eight_files(self):
        today = dt.date(2024, 12, 18)
        for back, ident in ((0, "1000001"), (7, "1000007"), (8, "1000008")):
            self.csv((today - dt.timedelta(days=back)).isoformat(), [f"https://www.linkedin.com/jobs/view/{ident}"])
        self.assertEqual(sorted(feed.known_job_ids(self.root, today)), ["1000001", "1000007"])

    def test_a_refused_trigger_fails_the_step_so_the_guest_lane_runs(self):
        api = FakeApi([], trigger=(401, "bad token"))
        code, status, out = self.run_feed(api)
        self.assertEqual(code, 1)
        self.assertEqual((status["state"], status["providers"]["brightdata_feed"]["reason"]), ("failed", "auth_failed"))
        self.assertEqual(self.saved, [])
        self.assertNotIn("bad token", status["providers"]["brightdata_feed"]["reason"])
        # 2024-12-19: the refused trigger bought nothing, so the budget is back and a later run the same day fires
        self.assertEqual(self.gate.store.get(feed.KEY)["requests"]["today"], 0)
        again = FakeApi([record()])
        code, status, _ = self.run_feed(again)
        self.assertEqual((code, status["state"]), (0, "ok"))
        self.assertTrue(any("/trigger" in url for _, url, _ in again.calls))

    def test_a_collection_that_never_gets_ready_fails_inside_the_step_cap(self):
        api = FakeApi([record()], progress=("running",))
        code, status, out = self.run_feed(api)
        self.assertEqual((code, status["providers"]["brightdata_feed"]["reason"]), (1, "collection_timeout"))
        self.assertLessEqual(sum(self.clock.sleeps), feed.WAIT_SECONDS + feed.POLL_SECONDS)
        self.assertEqual(self.saved, [])

    def test_no_token_means_exit_1_without_a_call(self):
        with mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": ""}):
            api = FakeApi([record()])
            code, status, out = self.run_feed(api)
        self.assertEqual((code, api.calls, status["providers"]["brightdata_feed"]["reason"]), (1, [], "api_token_missing"))

    def test_dry_run_prints_the_plan_and_triggers_nothing(self):
        api = FakeApi([record()])
        code, status, out = self.run_feed(api, ["--dry-run"])
        self.assertEqual((code, api.calls, status), (0, [], None))
        self.assertIn("21 inputs, 14 records each", out)


if __name__ == "__main__":
    unittest.main()
