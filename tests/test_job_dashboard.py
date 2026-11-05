import contextlib
import csv
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
# Sample postings dated relative to today: the dashboard only keeps listings under 3 weeks old (2024-12-09).
RECENT_OLDER = (dt.date.today() - dt.timedelta(days=2)).isoformat()
RECENT = (dt.date.today() - dt.timedelta(days=1)).isoformat()
MODULE_PATH = ROOT / "scripts" / "job_dashboard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("job_dashboard", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JobDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()
        (self.root / "scripts").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_raw_csv(self, filename="linkedin-raw-2024-08-27.csv"):
        path = self.root / "weekly-sweeps" / filename
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["title", "company", "location", "date_posted", "min_amount", "max_amount", "is_remote", "job_url"],
            )
            writer.writeheader()
            writer.writerow({
                "title": "IT Support Technician",
                "company": "Elastic",
                "location": "Durham, NC",
                "date_posted": RECENT_OLDER,
                "is_remote": "False",
                "job_url": "https://www.linkedin.com/jobs/view/4415581100",
            })
            writer.writerow({
                "title": "Desktop Support Engineer",
                "company": "NewCo",
                "location": "Durham, NC",
                "date_posted": RECENT,
                "is_remote": "False",
                "job_url": "https://example.com/newco",
            })
            writer.writerow({
                "title": "IT Support Engineer (Tier 2) - NJ",
                "company": "FarAwayCo",
                "location": "Lakewood, NJ",
                "date_posted": RECENT,
                "is_remote": "False",
                "job_url": "https://example.com/faraway",
            })
            writer.writerow({
                "title": "IT Support Specialist II (Remote, US)",
                "company": "Openly",
                "location": "",
                "date_posted": RECENT,
                "is_remote": "True",
                "job_url": "https://example.com/openly-duplicate",
            })
        return path

    def write_dashboard_data(self):
        data = {
            "last_validated": RECENT,  # a curated role verified over 21 days ago leaves Apply (2024-12-17)
            "profile": {
                "name": "Alex Example",
                "search": "Example metro + Remote",
                "target": "IT Support / Sysadmin / Infrastructure",
                "salary_floor": "$85k minimum",
                "toolset": "Jamf · Intune · Okta · CrowdStrike · M365",
            },
            "apply": [
                {
                    "company": "Elastic",
                    "title": "IT Support Technician",
                    "company_blurb": "Search AI company.",
                    "location": "Durham, NC",
                    "work_model": "Onsite",
                    "salary": "Not posted",
                    "verified": "Live · verified Jun 30",
                    "apply_url": "https://jobs.example.com/jobs?gh_jid=1000006",
                    "source": "jobs.example.com",
                    "why": "Matches JOB_A toolset.",
                    "note": "Confirm the salary.",
                }
            ],
            "skip": [
                {
                    "company": "Openly",
                    "title": "IT Support Specialist II",
                    "location": "Remote US",
                    "tag": "Closed",
                    "reason": "No longer posted on company board.",
                }
            ],
        }
        path = self.root / "dashboard" / "job-dashboard-data.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_default_profile_uses_updated_salary_range(self):
        module = load_module()
        self.assertEqual(module.DEFAULT_PROFILE["salary_floor"], "$70k–$150k target range")

    def test_latest_raw_csv_uses_most_recent_dated_file(self):
        older = self.write_raw_csv("linkedin-raw-2024-08-26.csv")
        newer = self.write_raw_csv("linkedin-raw-2024-08-27.csv")
        leftover = self.write_raw_csv("linkedin-raw-2024-08-27_filtered.csv")  # retired prefilter output: not a pull
        hand_made = self.write_raw_csv("linkedin-raw-9999-12-31-contract-test.csv")
        module = load_module()
        self.assertEqual(module.latest_raw_csv(self.root), newer)
        self.assertEqual([p for _, p in module.pull_csvs(self.root)], [older, newer])
        for path in (older, leftover, hand_made):
            path.unlink()

    def test_render_dashboard_creates_one_living_html_and_markdown_without_pdf_language(self):
        self.write_raw_csv()
        self.write_dashboard_data()
        module = load_module()

        result = module.build_dashboard(self.root, check_links=False)

        html = (self.root / "job-dashboard.html").read_text(encoding="utf-8")
        md = (self.root / "job-dashboard.md").read_text(encoding="utf-8")
        self.assertEqual(result["html_path"], str(self.root / "job-dashboard.html"))
        self.assertIn("Apply to 1", html)
        self.assertIn("Elastic", html)
        self.assertIn("Openly", html)
        self.assertIn("Needs validation", html)
        self.assertIn("NewCo", html)
        self.assertNotIn("FarAwayCo", html)
        self.assertNotIn("openly-duplicate", html)
        self.assertNotIn("daily-jobs-2024-08-27.pdf", html)
        self.assertIn("Apply to these", md)
        self.assertIn("Skip these", md)
        self.assertIn("New roles needing validation", md)

    def test_applied_on_check_mark_moves_role_out_of_apply(self):
        # Applied roles once sat in Apply for a week because pipeline.md listed the companies without
        # full titles. applied_on is exact: no pipeline.md row needed, outcome rides along.
        module = load_module()
        data = {"apply": [
            {"company": "Acme Robotics", "title": "IT Systems Administrator", "applied_on": "2024-10-29"},
            {"company": "Example Medical Co", "title": "IT Engineer",
             "applied_on": "2024-10-29", "outcome": "Rejected 2024-11-05"},
            {"company": "Example Staffing (client role)", "title": "Executive / VIP IT Support Specialist"},
        ]}
        remaining, tracked = module.reconcile_with_pipeline(data)
        self.assertEqual([r["company"] for r in remaining], ["Example Staffing (client role)"])
        self.assertEqual([r["tracked"] for r in tracked],
                         ["Applied 2024-10-29", "Applied 2024-10-29 · Rejected 2024-11-05"])
        cards = "".join(module.tracked_card(r) for r in tracked)
        self.assertIn("✅ Applied 2024-10-29", cards)
        self.assertIn("❌ Applied 2024-10-29 · Rejected 2024-11-05", cards)

    def test_raw_queue_review_count_is_nonzero_and_flags_empty(self):
        # Regression for the 2024-10-16 "frozen dashboard" bug: the daily pipeline renders the
        # needs-validation queue from the raw feed, and pull-jobs.ps1's queue-health guard alerts
        # when review_count is 0. Lock both halves: relevant rows surface, an empty feed reports 0.
        self.write_raw_csv()
        self.write_dashboard_data()
        module = load_module()

        populated = module.build_dashboard(self.root, check_links=False)
        self.assertGreater(populated["review_count"], 0)

        # An empty raw feed yields zero candidates — exactly the signal the guard escalates on.
        for csv_file in (self.root / "weekly-sweeps").glob("linkedin-raw-*.csv"):
            csv_file.unlink()
        empty = module.build_dashboard(self.root, check_links=False)
        self.assertEqual(empty["review_count"], 0)

    def test_raw_queue_ranks_the_pulls_of_today_and_yesterday(self):
        # A missed night (the laptop asleep at 00:30) leaves no CSV for today. The queue then ranks the most
        # recent pulls on disk within the two-day window, not a union of the week. 2024-12-23: yesterday's pull
        # is read alongside today's, so a day whose render never ran (the 10/25 chain was killed after the
        # judge) is recovered by the next render instead of being lost at midnight.
        module = load_module()
        fields = ["title", "company", "location", "date_posted", "is_remote", "job_url"]

        def pull(day, rows):
            with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

        today = dt.date.today()
        yesterday, before = today - dt.timedelta(days=1), today - dt.timedelta(days=2)
        pull(before, [{"title": "IT Support Specialist", "company": "OlderCo", "location": "Durham, NC",
                       "date_posted": RECENT_OLDER, "is_remote": "False", "job_url": "https://x/older"}])
        pull(yesterday, [{"title": "IT Support Specialist II", "company": "YesterdayCo", "location": "Raleigh, NC",
                          "date_posted": RECENT, "is_remote": "False", "job_url": "https://x/yesterday"}])
        self.write_dashboard_data()

        result = module.build_dashboard(self.root, check_links=False, today=today)
        html = (self.root / "job-dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(result["review_count"], 1)
        self.assertIn("YesterdayCo", html)
        self.assertNotIn("OlderCo", html)

        pull(today, [{"title": "Desktop Support Technician", "company": "TodayCo", "location": "Durham, NC",
                      "date_posted": RECENT, "is_remote": "False", "job_url": "https://x/today"}])
        result = module.build_dashboard(self.root, check_links=False, today=today)
        html = (self.root / "job-dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(result["review_count"], 2)
        self.assertIn("TodayCo", html)
        self.assertIn("YesterdayCo", html)  # yesterday's unrendered rows are not lost
        self.assertNotIn("OlderCo", html)   # two days ago is outside the window
        self.assertEqual(module.render_raw_csvs(self.root, today),
                         [self.root / "weekly-sweeps" / f"linkedin-raw-{d}.csv" for d in (today, yesterday)])

    def test_render_window_falls_back_to_the_latest_pull_when_nothing_is_recent(self):
        # A laptop off for several days: the newest pull on disk is still ranked rather than an empty queue.
        module = load_module()
        today = dt.date.today()
        old = today - dt.timedelta(days=5)
        older = today - dt.timedelta(days=9)
        for day in (older, old):
            self.write_raw_csv(f"linkedin-raw-{day}.csv")
        self.assertEqual(module.render_raw_csvs(self.root, today), [self.root / "weekly-sweeps" / f"linkedin-raw-{old}.csv"])
        self.assertEqual(module.render_raw_csvs(self.root, today, days=10),
                         [self.root / "weekly-sweeps" / f"linkedin-raw-{d}.csv" for d in (old, older)])

    def test_validation_result_is_written_next_to_apply_roles(self):
        self.write_raw_csv()
        self.write_dashboard_data()
        module = load_module()

        result = module.build_dashboard(
            self.root,
            check_links=True,
            link_checker=lambda url: {"ok": True, "status": 200, "final_url": url},
        )

        html = pathlib.Path(result["html_path"]).read_text(encoding="utf-8")
        self.assertIn("HTTP 200", html)
        self.assertIn("official apply link checked", html)

    def test_raw_reason_names_actual_source(self):
        path = self.write_raw_csv()
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            fields = list(rows[0]) + ["site"]
        rows[1]["site"] = "usajobs"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        self.write_dashboard_data()
        module = load_module()
        module.build_dashboard(self.root, check_links=False)
        md = (self.root / "job-dashboard.md").read_text(encoding="utf-8")
        self.assertIn("Fresh raw match from usajobs", md)

    def test_dashboard_outputs_are_atomic_and_root_defaults_from_environment(self):
        self.write_dashboard_data()
        module = load_module()
        with mock.patch.dict(os.environ, {"JOBSEARCH_ROOT": str(self.root)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(["--no-link-check", "--no-queue-check"]), 0)
        self.assertTrue((self.root / "job-dashboard.html").exists())
        self.assertTrue((self.root / "job-dashboard.md").exists())
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_queue_has_one_source_the_raw_csv(self):
        # 2024-12-17: the job-agent database is retired, so there is no queue-source switch any more.
        self.write_dashboard_data()
        module = load_module()
        result = module.build_dashboard(self.root, check_links=False)
        self.assertNotIn("queue_source", result)
        self.assertFalse(hasattr(module, "unvalidated_from_db"))
        self.assertTrue((self.root / "job-dashboard.md").exists())


class TargetAreaTests(unittest.TestCase):
    """The example target area is Raleigh-Durham (plus remote)."""

    def test_target_area_is_home_region_or_remote(self):
        module = load_module()
        self.assertTrue(module.is_target_location({"location": "Raleigh, NC"}))
        self.assertTrue(module.is_target_location({"location": "Remote"}))
        self.assertTrue(module.is_target_location({"location": "United States", "is_remote": "true"}))
        self.assertFalse(module.is_target_location({"location": "Denver, CO"}))
        # The remote flag alone is not evidence when the location names another city.
        self.assertFalse(module.is_target_location({"location": "Charlotte, NC", "is_remote": "true"}))

    def test_only_listings_under_three_weeks_old(self):
        # The owner's rule since 2024-12-09. Unknown dates stay visible for review.
        module = load_module()
        today = dt.date(2024, 12, 9)
        self.assertTrue(module.posted_recently("2024-11-22", today))
        self.assertFalse(module.posted_recently("2024-11-18", today))
        self.assertTrue(module.posted_recently("", today))
        rows = [{"title": "IT Support Specialist", "company": "NewCo", "location": "Durham, NC", "date_posted": "2024-12-07"},
                {"title": "IT Support Specialist", "company": "OldCo", "location": "Durham, NC", "date_posted": "2024-10-29"}]
        with mock.patch.object(module.employer_hq, "known_outside_home_state", return_value=False):
            kept = module.rank_raw_candidates(rows, set(), today=today)
        self.assertEqual([r["company"] for r in kept], ["NewCo"])

    def test_employers_known_to_be_out_of_state_still_reach_the_queue(self):
        # Widened 2024-12-23: a North Carolina branch also qualifies (an out-of-state headquarters with a local
        # branch), and headquarters alone cannot disprove a branch, so the relevance gate no longer
        # excludes known out-of-state employers. The queue-check flags their remote roles for the owner's branch
        # confirmation instead of hiding them.
        module = load_module()
        row = {"title": "IT Support Specialist", "company": "DLA Piper", "location": "Durham, NC"}
        with mock.patch.object(module.employer_hq, "known_outside_home_state", return_value=True):
            self.assertTrue(module.is_probably_relevant(row))


class FeedExtrasTests(unittest.TestCase):
    """Two feed fields the DB does not keep (2024-12-09): the published pay interval (the staffing-agency
    hourly flag) and an Indeed row's employer posting (checked instead of Indeed's page)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_pull(self, name, rows, fields=("title", "company", "job_url", "pay_interval", "job_url_direct")):
        with (self.root / "weekly-sweeps" / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields))
            writer.writeheader()
            writer.writerows(rows)

    def test_reads_the_last_three_weeks_of_pulls(self):
        module = load_module()
        self.write_pull("linkedin-raw-2024-12-09.csv", [{"job_url": "https://x/1", "pay_interval": "hourly"},
                                                         {"job_url": "https://x/2", "pay_interval": "", "job_url_direct": "https://acme/2"},
                                                         {"job_url": "https://x/7", "pay_interval": ""}])
        self.write_pull("linkedin-raw-2024-12-09_filtered.csv", [{"job_url": "https://x/3", "pay_interval": "hourly"}])
        self.write_pull("linkedin-raw-2024-11-18.csv", [{"job_url": "https://x/4", "pay_interval": "hourly"}])
        self.write_pull("linkedin-raw-2024-11-28.csv", [{"job_url": "https://x/5"}], fields=("job_url",))  # before the column
        self.write_pull("linkedin-raw-9999-12-31-contract-test.csv", [{"job_url": "https://x/6", "pay_interval": "hourly"}])
        self.assertEqual(module.feed_extras(self.root, dt.date(2024, 12, 9)),
                         {"https://x/1": {"pay_interval": "hourly"}, "https://x/2": {"job_url_direct": "https://acme/2"}})

    def test_queue_cards_link_the_page_the_checker_read(self):
        module = load_module()
        role = {"title": "IT Support Specialist", "company": "Acme", "location": "Durham, NC",
                "job_url": "https://www.indeed.com/viewjob?jk=1", "facts": {"checked_url": "https://acme.wd1.myworkdayjobs.com/x"}}
        for card in (module.apply_now_card(role), module.needs_validation_card(role)):
            self.assertIn('href="https://acme.wd1.myworkdayjobs.com/x"', card)
            self.assertNotIn("indeed.com", card)
        unchecked = dict(role, facts={})  # could not be checked: the feed link stays
        self.assertIn('href="https://www.indeed.com/viewjob?jk=1"', module.needs_validation_card(unchecked))


class KeyToolsTests(unittest.TestCase):
    """2024-12-09: each card shows which of the owner's key tools the posting mentions, and the best matches come first."""

    def test_the_checker_gets_the_key_tools_from_validate_listing(self):
        # 2024-12-17: the list moved from the job-agent database to validate_listing.KEY_TOOLS.
        module = load_module()
        self.assertEqual(module.validate_listing.KEY_TOOLS,
                         ("Intune", "Jamf", "Okta", "Entra", "Active Directory", "ServiceNow", "PowerShell"))
        with mock.patch.object(module.validate_listing, "validate", return_value={"verdict": "review"}) as validate:
            module.validate_candidate({"title": "IT Support Specialist", "company": "Acme", "job_url": "https://x/1"})
        self.assertEqual(validate.call_args.kwargs["skills"], module.validate_listing.KEY_TOOLS)
        self.assertFalse(hasattr(module, "key_tools"))

    def test_cards_show_the_tools_the_posting_mentions(self):
        module = load_module()
        role = {"title": "IT Support Specialist", "company": "Acme", "location": "Durham, NC", "job_url": "https://x/1",
                "facts": {"skills": ["Intune", "Okta"], "skills_of": 7}}
        self.assertIn("2 of 7 key tools: Intune, Okta", module.apply_now_card(role))
        self.assertIn("2 of 7 key tools: Intune, Okta", module.needs_validation_card(role))
        self.assertIn("0 of 7 key tools<", module.apply_now_card(dict(role, facts={"skills": [], "skills_of": 7})))
        self.assertNotIn("key tools", module.apply_now_card(dict(role, facts={})))  # posting not read: no chip

    def test_best_matches_come_first(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "weekly-sweeps").mkdir()
            (root / "dashboard").mkdir()
            with (root / "weekly-sweeps" / f"linkedin-raw-{RECENT}.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("title", "company", "location", "job_url", "date_posted", "site"))
                writer.writeheader()
                for i, company in enumerate(("Alpha Co", "Beta Co", "Gamma Co")):
                    writer.writerow({"title": "IT Support Specialist", "company": company, "location": "Durham, NC",
                                     "job_url": f"https://x/{i}", "date_posted": RECENT, "site": "indeed"})
            tools = {"Alpha Co": ["Okta"], "Beta Co": ["Intune", "Jamf", "Okta"], "Gamma Co": []}
            checker = lambda cand: {"verdict": "apply_now", "reasons": [],
                                    "facts": {"skills": tools[cand["company"]], "skills_of": 7}}
            with mock.patch.object(module.employer_hq, "known_outside_home_state", return_value=False):
                module.build_dashboard(root, check_links=False, queue_checker=checker)
            page = (root / "job-dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(sorted(tools, key=page.index), ["Beta Co", "Alpha Co", "Gamma Co"])


class NotFoundTests(unittest.TestCase):
    """2024-12-09: "page not found" can be a site down for maintenance, so a role is dropped only when a
    second morning's check says so too. A page that says it is closed still drops at once."""

    TODAY = dt.date(2024, 12, 10)

    def setUp(self):
        self.m = load_module()
        self.cand = self.m.raw_candidate({"company": "Acme", "title": "IT Support Specialist", "location": "Durham, NC",
                                          "date_posted": "2024-12-09", "job_url": "https://acme.example/job/1",
                                          "site": "linkedin"})
        self.key = self.m.seen_key(self.cand)

    def gone(self, cand):
        return {"verdict": "dead", "reasons": ["listing closed"], "facts": {"not_found": True}}

    def test_a_first_not_found_hides_the_role_and_a_second_morning_drops_it(self):
        seen = {}
        sel = self.m.select_queue([self.cand], seen, self.TODAY, checker=self.gone)
        self.assertEqual((sel["queue"], sel["dead"]), ([], 0))
        self.assertEqual(seen[self.key]["not_found_on"], "2024-12-10")
        self.m.select_queue([self.cand], seen, self.TODAY, checker=self.gone)  # same-day re-render
        self.m.select_queue([self.cand], seen, self.TODAY + dt.timedelta(days=1), checker=None)  # no new check
        self.assertNotIn("dead_on", seen[self.key])
        sel = self.m.select_queue([self.cand], seen, self.TODAY + dt.timedelta(days=1), checker=self.gone)
        self.assertEqual((sel["dead"], seen[self.key]["dead_on"]), (1, "2024-12-11"))

    def test_a_role_that_comes_back_is_shown_again(self):
        seen = {}
        self.m.select_queue([self.cand], seen, self.TODAY, checker=self.gone)
        live = lambda cand: {"verdict": "apply_now", "reasons": [], "facts": {}}
        sel = self.m.select_queue([self.cand], seen, self.TODAY + dt.timedelta(days=1), checker=live)
        self.assertEqual([c["tier"] for c in sel["queue"]], ["apply_now"])
        self.assertNotIn("not_found_on", seen[self.key])

    def test_a_held_role_is_rechecked_after_the_feed_drops_it(self):
        seen = {self.key: {"verdict": "apply_now", "first_seen": "2024-12-08", "checked_on": "2024-12-09",
                           "company": "Acme", "title": "IT Support Specialist", "date_posted": "2024-12-09",
                           "job_url": "https://acme.example/job/1"}}
        self.m.select_queue([self.cand], seen, self.TODAY, checker=self.gone)
        carried = self.m.carried_candidates(seen, self.TODAY + dt.timedelta(days=1), set(), set())
        self.assertEqual([c["company"] for c in carried], ["Acme"])

    def test_a_page_that_says_closed_still_drops_at_once(self):
        seen = {}
        closed = lambda cand: {"verdict": "dead", "reasons": ["listing closed"], "facts": {"not_found": False}}
        sel = self.m.select_queue([self.cand], seen, self.TODAY, checker=closed)
        self.assertEqual((sel["dead"], seen[self.key]["dead_on"]), (1, "2024-12-10"))


class _PullRoot(unittest.TestCase):
    """A scratch Job Search folder with daily pulls written by date."""

    TODAY = dt.date(2024, 12, 9)
    FIELDS = ("title", "company", "location", "date_posted", "job_url", "site", "is_remote")

    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()
        self.no_hq = mock.patch.object(self.m.employer_hq, "known_outside_home_state", return_value=False)
        self.no_hq.start()

    def tearDown(self):
        self.no_hq.stop()
        self.tmp.cleanup()

    def job(self, company, link, title="IT Support Specialist", location="Durham, NC", posted="2024-12-08"):
        return {"title": title, "company": company, "location": location, "date_posted": posted,
                "job_url": f"https://jobs.example/{link}", "site": "linkedin", "is_remote": "false"}

    def write_pull(self, day, rows):
        with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.FIELDS))
            writer.writeheader()
            writer.writerows(rows)

    def build(self, verdict="apply_now"):
        checker = lambda cand: {"verdict": verdict, "reasons": [], "facts": {}}
        result = self.m.build_dashboard(self.root, check_links=False, queue_checker=checker, today=self.TODAY)
        return result, (self.root / "job-dashboard.html").read_text(encoding="utf-8"), (self.root / "job-dashboard.md").read_text(encoding="utf-8")


class RepostTests(_PullRoot):
    """The owner's choice 2024-12-09: flag reposts on every card; a job reposted 3+ times in 4 weeks goes to Review."""

    def test_new_links_days_apart_are_reposts_but_same_day_copies_and_other_towns_are_not(self):
        self.write_pull("2024-11-17", [self.job("Acme", "a1"), self.job("Acme", "a1-indeed"), self.job("Solo", "s1"),
                                       self.job("Solo", "s1-indeed")])
        self.write_pull("2024-11-25", [self.job("Acme", "a2"), self.job("Acme", "a-morrisville", location="Morrisville, NC")])
        self.write_pull("2024-12-03", [self.job("Acme", "a3")])
        self.write_pull("2024-09-28", [self.job("Acme", "a0")])  # older than four weeks: ignored
        history = self.m.repost_history(self.root, self.TODAY)
        self.assertEqual(self.m.repost_info(self.job("Acme", "a3"), history), {"reposts": 2, "since": "2024-11-17"})
        self.assertEqual(self.m.repost_info(self.job("Solo", "s1"), history), {})  # LinkedIn + Indeed the same day
        self.assertEqual(self.m.repost_info(self.job("Acme", "a-morrisville", location="Morrisville, NC"), history), {})

    def test_chronic_reposts_go_to_review_and_a_single_repost_keeps_apply_now(self):
        self.write_pull("2024-11-12", [self.job("Ghostco", "g1"), self.job("Onceco", "o1")])
        self.write_pull("2024-11-19", [self.job("Ghostco", "g2")])
        self.write_pull("2024-11-26", [self.job("Ghostco", "g3")])
        self.write_pull("2024-12-09", [self.job("Ghostco", "g4"), self.job("Onceco", "o2")])
        result, html_text, md_text = self.build()
        self.assertEqual(result["apply_now_count"], 1)
        self.assertIn("reposted 1 time since 2024-11-12", html_text)  # Onceco keeps Apply now, flagged
        self.assertIn("reposted 3 times since 2024-11-12: may be a ghost job, so not Apply now", html_text)
        self.assertIn("reposted 3 times since 2024-11-12", md_text.split("## 🔎 New roles needing validation")[1])


class DecisionTests(_PullRoot):
    """The owner's Applied / Later / Not for me clicks (2024-12-09) stick: the job leaves the queue for good."""

    def decide(self, **by_company):
        entries = {}
        for company, decision in by_company.items():
            role = self.job(company, company.lower())
            entries[self.m.seen_key(role)] = {"decision": decision, "on": "2024-12-09", "company": company,
                                              "title": role["title"], "job_url": role["job_url"], "location": role["location"]}
        (self.root / "dashboard" / "decisions.json").write_text(json.dumps(entries), encoding="utf-8")

    def test_clicks_change_what_the_dashboard_shows(self):
        self.write_pull("2024-12-09", [self.job(c, c.lower()) for c in ("Passco", "Laterco", "Appliedco", "Newco")])
        self.decide(Passco="skip", Laterco="later", Appliedco="applied")
        result, html_text, md_text = self.build()
        self.assertEqual(result["apply_now_count"], 1)
        apply_now = html_text.split("Apply now: new roles checked today")[1].split("<h2>")[0]
        self.assertIn("Newco", apply_now)
        self.assertNotIn("Passco", html_text)
        saved = html_text.split("Saved for later")[1].split("<h2>")[0]
        self.assertIn("Laterco", saved)
        self.assertIn("Applied 2024-12-09 (marked on the dashboard)", html_text)
        self.assertIn("## ⭐ Saved for later", md_text)

    def test_an_applied_click_stays_listed_after_the_saved_window(self):
        # 2024-12-19: Applied is a record and never expires; Saved for later still leaves after DECISION_SHOW_DAYS
        old = (self.TODAY - dt.timedelta(days=self.m.DECISION_SHOW_DAYS + 10)).isoformat()
        decisions = {"a": {"decision": "applied", "on": old, "company": "Oldco", "title": "T", "location": "Durham, NC"},
                     "b": {"decision": "later", "on": old, "company": "Laterco", "title": "T", "location": "Durham, NC"},
                     "c": {"decision": "later", "on": self.TODAY.isoformat(), "company": "Freshco", "title": "T", "location": "Durham, NC"}}
        saved, applied = self.m.apply_decisions(decisions, self.TODAY)
        self.assertEqual([e["company"] for e in saved], ["Freshco"])
        self.assertEqual([e["company"] for e in applied], ["Oldco"])

    def test_cards_carry_buttons_and_the_page_carries_the_token(self):
        self.write_pull("2024-12-09", [self.job("Newco", "n1")])
        _, html_text, _ = self.build()
        token = (self.root / "dashboard" / ".decision-token").read_text(encoding="utf-8")
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertIn(f'const TOKEN = "{token}";', html_text)
        key = self.m.seen_key(self.job("Newco", "n1"))
        self.assertIn(f'data-key="{key}"', html_text)
        for label in ("Applied", "Later", "Not for me"):
            self.assertIn(f">{label}</button>", html_text)
        self.assertEqual(self.m.decision_token(self.root), token)  # stable across renders

    def test_a_curated_role_marked_not_for_me_moves_to_skip(self):
        role = {"company": "Curatedco", "title": "IT Support Specialist", "location": "Durham, NC",
                "apply_url": "https://curated.example/1", "verified_on": "2024-12-07"}
        (self.root / "dashboard" / "job-dashboard-data.json").write_text(
            json.dumps({"apply": [role], "skip": []}), encoding="utf-8")
        (self.root / "dashboard" / "decisions.json").write_text(json.dumps(
            {self.m.seen_key(role): {"decision": "skip", "on": "2024-12-09", "company": "Curatedco"}}), encoding="utf-8")
        self.write_pull("2024-12-09", [])
        _, html_text, _ = self.build()
        skip = html_text.split("Skip these")[1].split("</details>")[0]
        self.assertIn("Curatedco", skip)
        self.assertIn("You marked it Not for me on 2024-12-09.", skip)


class SectionOrderTests(_PullRoot):
    """2024-12-23: the needs-validation queue sat under a long applied/skip history, so nothing new was
    visible without a long scroll. New roles now render directly after the curated Apply column, and the
    applied history and Skip list render collapsed (but complete) at the bottom of the page."""

    def write_data(self):
        (self.root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps({
            "last_validated": RECENT,
            "apply": [{"company": "Doneco", "title": "IT Support Specialist", "location": "Denver, CO",
                       "apply_url": "https://x/done", "applied_on": "2024-10-29"}],
            "skip": [{"company": "Skipco", "title": "IT Support Specialist", "location": "Denver, CO",
                      "tag": "Skip", "reason": "Out of area."}],
        }), encoding="utf-8")

    def test_new_roles_render_right_after_apply_in_html_and_markdown(self):
        self.write_data()
        self.write_pull("2024-12-09", [self.job("Queueco", "q1")])
        _, html_text, md_text = self.build()
        self.assertLess(html_text.index("Apply to these (curated)"), html_text.index("New roles needing validation"))
        self.assertLess(html_text.index("New roles needing validation"), html_text.index("Skip these"))
        self.assertLess(md_text.index("## ✅ Apply to these (curated)"), md_text.index("## 🔎 New roles needing validation"))
        self.assertLess(md_text.index("## 🔎 New roles needing validation"), md_text.index("## ⛔ Skip these"))

    def test_applied_and_skip_history_render_collapsed_but_complete(self):
        self.write_data()
        self.write_pull("2024-12-09", [self.job("Queueco", "q1")])
        _, html_text, _ = self.build()
        self.assertEqual(html_text.count("<details"), 2)
        self.assertIn("Already applied or in process (1)", html_text)
        self.assertIn("Skip these (1)", html_text)
        self.assertIn("Doneco", html_text)  # collapsed, never deleted
        self.assertIn("Skipco", html_text)

    def test_collapsed_history_expands_for_printing(self):
        self.write_data()
        self.write_pull("2024-12-09", [self.job("Queueco", "q1")])
        _, html_text, _ = self.build()
        self.assertIn("details.history:not([open])", html_text)  # print CSS forces the history open


if __name__ == "__main__":
    unittest.main()


class QueueCheckSplitTests(unittest.TestCase):
    """2024-12-17: the chain runs the Bright Data listing checks as their own `queue-check` step
    (--queue-check-only) and the render reads the remembered verdicts with no network at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()
        self.today = dt.date.today()
        with (self.root / "weekly-sweeps" / f"linkedin-raw-{self.today.isoformat()}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["title", "company", "location", "date_posted", "is_remote", "job_url", "site"])
            writer.writeheader()
            writer.writerow({"title": "IT Support Specialist", "company": "TriangleCo", "location": "Chapel Hill, NC",
                             "date_posted": RECENT, "is_remote": "false", "job_url": "https://example.com/triangleco", "site": "indeed"})
        (self.root / "dashboard" / "job-dashboard-data.json").write_text(
            json.dumps({"last_validated": RECENT, "apply": [], "skip": []}), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("job_dashboard", MODULE_PATH)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_queue_check_only_stores_verdicts_and_writes_no_views(self):
        calls = []

        def checker(cand):
            calls.append(cand["company"])
            return "live"

        first = self.m.build_dashboard(self.root, check_links=False, queue_checker=checker, today=self.today, write_views=False)
        self.assertEqual(calls, ["TriangleCo"])
        self.assertEqual((first["queue_checked"], first["review_count"], first["html_path"]), (1, 1, ""))
        self.assertFalse((self.root / "job-dashboard.html").exists())
        seen = json.loads((self.root / "dashboard" / "queue-seen.json").read_text(encoding="utf-8"))
        entry = next(iter(seen.values()))
        self.assertEqual((entry["verdict"], entry["checked_on"]), ("live", self.today.isoformat()))

        def never(cand):
            raise AssertionError("render must not fetch anything")

        second = self.m.build_dashboard(self.root, check_links=False, queue_checker=None, link_checker=never, today=self.today)
        self.assertTrue((self.root / "job-dashboard.html").exists())
        self.assertEqual(second["review_count"], 1)
        self.assertIn("TriangleCo", (self.root / "job-dashboard.md").read_text(encoding="utf-8"))

    def test_cli_flag_implies_no_link_check_and_no_views(self):
        with mock.patch.object(self.m, "build_dashboard", return_value={"ok": True}) as build, \
             mock.patch.object(self.m.rate_control, "finish_shared_runs", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.main(["--root", str(self.root), "--queue-check-only"]), 0)
        kwargs = build.call_args.kwargs
        self.assertEqual((kwargs["check_links"], kwargs["write_views"]), (False, False))
        self.assertIsNotNone(kwargs["queue_checker"])
