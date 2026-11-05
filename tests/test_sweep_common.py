import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts import sweep_common


class SweepCommonTests(unittest.TestCase):
    def test_nan_nat_and_none_become_empty_not_literals(self):
        row = {
            "title": "IT Support", "company": "Acme", "location": float("nan"),
            "date_posted": "NaT", "min_amount": None, "max_amount": float("nan"),
            "is_remote": float("nan"), "job_url": "https://x", "site": "linkedin",
        }
        cleaned = sweep_common._clean_row(row)
        for column in ("location", "date_posted", "min_amount", "max_amount", "is_remote"):
            self.assertEqual(cleaned[column], "", f"{column} should be empty, got {cleaned[column]!r}")
        self.assertNotIn("nan", " ".join(cleaned.values()).lower())

    def test_normalize_treats_nan_as_empty(self):
        self.assertEqual(sweep_common.normalize_company(float("nan")), "")
        self.assertEqual(sweep_common.normalize_title(float("nan")), "")
        self.assertEqual(sweep_common.normalize_company("NaN"), "")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, company="Acme", title="IT Support", site="test"):
        return {
            "title": title,
            "company": company,
            "location": "Denver, CO",
            "date_posted": "2024-09-12",
            "min_amount": "85000",
            "max_amount": "100000",
            "is_remote": "false",
            "job_url": "https://example.test/job",
            "site": site,
        }

    def read_rows(self):
        with sweep_common.daily_csv(self.root).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_project_root_honors_environment_override(self):
        with mock.patch.dict(os.environ, {"JOBSEARCH_ROOT": str(self.root)}):
            self.assertEqual(sweep_common.project_root(), self.root)

    def test_merge_creates_exact_schema_and_is_idempotent(self):
        result1 = sweep_common.merge_rows([self.row()], self.root)
        result2 = sweep_common.merge_rows([self.row(site="other")], self.root)
        self.assertEqual((result1["added"], result2["added"]), (1, 0))
        with sweep_common.daily_csv(self.root).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, sweep_common.CSV_COLUMNS)
            self.assertEqual(len(list(reader)), 1)

    def test_merge_backfills_text_direct_link_and_pay_from_a_later_source(self):
        # 2024-12-19: the 01:00 feed brings text and pay for roles Indeed listed first without them;
        # the first row keeps its link and site, and pay already present is never overwritten
        first = {**self.row(site="indeed"), "description": "", "job_url_direct": "", "min_amount": "", "max_amount": "",
                 "pay_interval": "", "job_url": "https://indeed/1"}
        sweep_common.merge_rows([first], self.root)
        later = {**self.row(site="linkedin"), "description": "Full posting text", "job_url_direct": "https://acme.example/jobs/1",
                 "min_amount": "70000", "max_amount": "80000", "pay_interval": "yearly", "job_url": "https://www.linkedin.com/jobs/view/1"}
        result = sweep_common.merge_rows([later], self.root)
        rows = self.read_rows()
        self.assertEqual((result["added"], result["total"], len(rows)), (0, 1, 1))
        self.assertEqual((rows[0]["site"], rows[0]["job_url"]), ("indeed", "https://indeed/1"))
        self.assertEqual((rows[0]["description"], rows[0]["job_url_direct"]), ("Full posting text", "https://acme.example/jobs/1"))
        self.assertEqual((rows[0]["min_amount"], rows[0]["max_amount"], rows[0]["pay_interval"]), ("70000", "80000", "yearly"))
        sweep_common.merge_rows([{**self.row(site="jsearch"), "min_amount": "20", "max_amount": "25", "pay_interval": "hourly"}], self.root)
        self.assertEqual(self.read_rows()[0]["min_amount"], "70000")
        # 2024-12-20: an interval with blank amounts (44 Indeed rows in 12 days) is not pay and is filled
        bare = {**self.row(company="Bare", site="indeed"), "min_amount": "", "max_amount": "", "pay_interval": "yearly"}
        sweep_common.merge_rows([bare], self.root)
        sweep_common.merge_rows([{**self.row(company="Bare", site="linkedin"), "min_amount": "60000", "max_amount": "65000", "pay_interval": "yearly"}], self.root)
        filled = next(r for r in self.read_rows() if r["company"] == "Bare")
        self.assertEqual((filled["min_amount"], filled["max_amount"], filled["pay_interval"]), ("60000", "65000", "yearly"))

    def test_one_place_rule_and_one_pay_table_for_every_lane(self):
        # 2024-12-19: four area rules of different strictness and five pay tables became these
        self.assertTrue(sweep_common.in_area({"location": "Cary, NC", "is_remote": "false"}))
        self.assertTrue(sweep_common.in_area({"location": "United States", "is_remote": "true"}))
        self.assertTrue(sweep_common.in_area({"location": "Remote (Birmingham, AL)", "is_remote": "false"}))
        self.assertFalse(sweep_common.in_area({"location": "Charlotte, NC", "is_remote": "true"}))  # a flag with a city is not evidence
        self.assertFalse(sweep_common.in_area({"location": "Reno, Nevada"}))
        self.assertEqual([sweep_common.remote_location(x) for x in ("Birmingham, AL", "Remote - US", "", float("nan"), "Anywhere", "Worldwide")],
                         ["Remote (Birmingham, AL)", "Remote - US", "Remote", "Remote", "Remote", "Remote"])
        self.assertTrue(sweep_common.in_area({"location": "Worldwide", "is_remote": "true"}))  # Remotive (2024-12-20)
        self.assertEqual([sweep_common.pay_interval(x) for x in ("hr", "Per Hour", "YEAR", "yearly", "Bi-weekly", "bogus", "")],
                         ["hourly", "hourly", "yearly", "yearly", "biweekly", "", ""])
        self.assertEqual([sweep_common.yearly_factor(x) for x in ("hourly", "Per Hour", "", "mo", "bogus")], [2080, 2080, 1, 12, None])

    def test_merge_preserves_other_sources(self):
        sweep_common.merge_rows([self.row(company="A", site="linkedin")], self.root)
        sweep_common.merge_rows([self.row(company="B", site="usajobs")], self.root)
        self.assertEqual({r["company"] for r in self.read_rows()}, {"A", "B"})

    def test_merge_adds_pay_interval_to_a_daily_file_written_before_it(self):
        path = sweep_common.daily_csv(self.root)
        path.parent.mkdir(parents=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=sweep_common.LEGACY_CSV_COLUMNS)
            writer.writeheader()
            writer.writerow(self.row(company="Old"))
        sweep_common.merge_rows([dict(self.row(company="New"), pay_interval="hourly")], self.root)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, sweep_common.CSV_COLUMNS)
        self.assertEqual([(r["company"], r["pay_interval"]) for r in rows], [("Old", ""), ("New", "hourly")])

    def test_description_is_one_line_of_plain_text_and_capped(self):
        # 2024-12-12: the posting text rides in the CSV; the job-agent importer parses line by line, so
        # no newlines, and board HTML (Greenhouse &lt;p&gt;) is unescaped and stripped.
        html = "&lt;p&gt;Supports &amp; maintains&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Active Directory&lt;/li&gt;&lt;li&gt;Intune&lt;/li&gt;&lt;/ul&gt;"
        self.assertEqual(sweep_common.clean_description(html), "Supports & maintains Active Directory Intune")
        self.assertEqual(sweep_common.clean_description("line one\n\nline two\r\n  three"), "line one line two three")
        self.assertEqual(sweep_common.clean_description(float("nan")), "")
        self.assertEqual(sweep_common.clean_description(None), "")
        long = " ".join(["word"] * 2000)
        capped = sweep_common.clean_description(long, limit=100)
        self.assertTrue(capped.endswith(" ...") and len(capped) <= 104, capped)

    def test_merge_writes_description_and_upgrades_the_11_column_file(self):
        path = sweep_common.daily_csv(self.root)
        path.parent.mkdir(parents=True)
        with path.open("w", newline="", encoding="utf-8") as handle:   # a file from 2024-12-09 to 10-15
            writer = csv.DictWriter(handle, fieldnames=sweep_common.LEGACY_CSV_COLUMNS)
            writer.writeheader()
            writer.writerow(dict(self.row(company="Old"), pay_interval="", job_url_direct=""))
        sweep_common.merge_rows([dict(self.row(company="New"), description="<p>Tier 2 support</p>\nOkta")], self.root)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, sweep_common.CSV_COLUMNS)
        self.assertEqual([(r["company"], r["description"]) for r in rows], [("Old", ""), ("New", "Tier 2 support Okta")])

    def test_staffing_agencies_by_name_and_agency_words(self):
        # The owner's rule (2024-12-09): agencies are shown, flagged when the pay is hourly.
        for name in ("Robert Half", "Kforce Inc", "TEKsystems", "Insight Global", "Yoh, A Day & Zimmermann Company",
                     "Example Staffing, Inc.", "Flybridge Staffing", "Crawford Thomas Recruiting", "Connect Tech+Talent"):
            self.assertTrue(sweep_common.is_staffing_agency(name), name)
        for name in ("UNC Chapel Hill", "Texas Workforce Commission", "Bureau of Naval Personnel", "Hays County",
                     "Insight Enterprises", "Apex Fintech Solutions", "Example Homes", "", float("nan")):
            self.assertFalse(sweep_common.is_staffing_agency(name), name)

    def test_merge_rejects_mismatched_header(self):
        path = sweep_common.daily_csv(self.root)
        path.parent.mkdir(parents=True)
        path.write_text("title,company\nBad,Header\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "header"):
            sweep_common.merge_rows([self.row()], self.root)

    def test_normalization_matches_dashboard_semantics(self):
        self.assertEqual(sweep_common.normalize_company("  ACME   Corp "), "acme")
        self.assertEqual(
            sweep_common.normalize_title("IT Support Specialist II (Remote, US)"),
            "it support specialist ii",
        )

    def test_company_suffixes_collapse_to_one_employer(self):
        self.assertEqual(sweep_common.normalize_company("Kapsch TrafficCom AG"),
                         sweep_common.normalize_company("Kapsch TrafficCom USA"))
        self.assertEqual(sweep_common.normalize_company("ActiveProspect, Inc."), "activeprospect")
        self.assertEqual(sweep_common.normalize_company("Amazon.com"), "amazon")
        self.assertEqual(sweep_common.normalize_company("Foo Co., Ltd."), "foo")
        self.assertEqual(sweep_common.normalize_company("Toys R Us"), "toys r us")

    def test_relevant_uses_one_vocabulary_with_word_boundaries_and_excludes(self):
        for title in ("IT Support Tech- Tier II", "Service Desk Analyst", "IT Systems Administrator",
                      "Information Technology Support Technician",
                      "Senior IT Systems Engineer (Azure Infrastructure)", "Manager, IT Support"):
            self.assertTrue(sweep_common.relevant(title), title)
        for title in ("Credit Support Analyst", "Power Systems Modeling Engineer", "Trading Systems Engineer",
                      "Senior Maintenance Technician", "Infrastructure Tax Lead",
                      "Staff+ Software Engineer, Auth & Identity", "Logistics Intern III",
                      "Director, Technical Support Engineering", ""):
            self.assertFalse(sweep_common.relevant(title), title)
        # Federal extras only apply when a sweep passes them (USAJOBS is 2210-restricted).
        self.assertTrue(sweep_common.relevant("IT SPECIALIST (CUSTSPT)", extra=sweep_common.FEDERAL_2210_EXTRA))
        self.assertFalse(sweep_common.relevant("Customer Support Specialist"))

    def test_vocabulary_widened_2026_09_08_catches_reworded_support_titles(self):
        # Once dropped for wording alone, though each was target work.
        for title in ("Tech Support Analyst - Denver (Onsite) - 6486133", "Customer Support Engineer",
                      "Microsoft 365 Administrator", "Collaboration Platform Administrator",
                      "SCCM Administrator", "Entra ID Engineer", "Application Support Engineer",
                      "Google Workspace Systems Engineer", "Systems Administration Specialist"):
            self.assertTrue(sweep_common.relevant(title), title)
        # The exclusions still win over the new include words; "Credit Support Analyst" stays out through the
        # finance exclusions since "support analyst" itself became an include word on 2024-12-19.
        for title in ("Sales Support Engineer", "Principal Support Engineer", "Support Engineer Intern",
                      "Director of Tech Support", "Staff Software Engineer, Active Directory",
                      "Credit Support Analyst"):
            self.assertFalse(sweep_common.relevant(title), title)

    def test_vocabulary_widened_2026_10_22_after_the_first_gold_round(self):
        # Two hand-labelled Apply roles never reached the judge: the title gate dropped them.
        for title in ("Support Analyst", "Senior Frontline Technician", "Technical Support Analyst",
                      "Computer Technician", "Information Systems Technician", "Network Support Specialist",
                      # common IT titles and O*NET's "IT Consultant": consultant and client support are
                      # exclusions only when nothing in the title says IT
                      "IT Support Consultant", "IT Client Support", "Technical Support Consultant", "IT Consultant",
                      # the title gate is for recall; the judge reads the description
                      "Software Support Analyst", "Customer Support Analyst II"):
            self.assertTrue(sweep_common.relevant(title), title)
        for title in ("Management Consultant", "Client Support Representative", "Client Support Associate",
                      "Credit Support Analyst", "Loan Support Analyst", "Trade Support Analyst",
                      "Frontline Sales Associate", "Customer Support Specialist"):
            self.assertFalse(sweep_common.relevant(title), title)

    def test_vocabulary_widened_2026_09_09_catches_hand_picked_linkedin_titles(self):
        # A 20-minute manual LinkedIn search found these; the engine had never seen them.
        for title in ("Technology Operations Support Analyst", "Jr. Sailpoint Engineer",
                      "Federal IT Enterprise Services Administrator", "SailPoint IdentityNow Administrator"):
            self.assertTrue(sweep_common.relevant(title), title)
        for title in ("Senior Staff SailPoint Architect", "Technology Operations Director", "Credit Support Analyst"):
            self.assertFalse(sweep_common.relevant(title), title)

    def test_lock_serializes_two_writers_and_sidecar_persists(self):
        ready = self.root / "ready"
        holder = subprocess.Popen([
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import time; from scripts import sweep_common as s; "
                f"p=Path({str(self.root)!r}); lp=s.lock_path(p); lp.parent.mkdir(parents=True); "
                "h=lp.open('a+b'); h.write(b'0') if lp.stat().st_size == 0 else None; h.flush(); s._acquire_lock(h, 2, .01); "
                f"Path({str(ready)!r}).write_text('ready'); time.sleep(.5); s._release_lock(h); h.close()"
            ),
        ], cwd=Path(__file__).resolve().parents[1])
        try:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            started = time.monotonic()
            sweep_common.merge_rows([self.row(company="B")], self.root, poll=.01)
            elapsed = time.monotonic() - started
            self.assertEqual(holder.wait(2), 0)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()
        self.assertGreaterEqual(elapsed, .35)
        self.assertEqual({r["company"] for r in self.read_rows()}, {"B"})
        self.assertTrue(sweep_common.lock_path(self.root).exists())
        self.assertFalse(list(sweep_common.daily_csv(self.root).parent.glob("*.tmp")))

    def test_home_region_is_the_three_county_area(self):
        # Example target: Durham, Wake and Orange counties.
        for loc in ("Durham, NC", "Raleigh, NC 33301", "Durham-Raleigh Area", "Chapel Hill, North Carolina",
                    "Carrboro, NC", "Cary, NC", "Hybrid remote in Rougemont, NC", "Morrisville, NC",
                    "Holly Springs, North Carolina", "Garner, NC", "Wake County"):
            self.assertTrue(sweep_common.is_home_region(loc), loc)
        # Elsewhere in North Carolina, same-named towns in other states, and Durhamville are not Raleigh-Durham.
        for loc in ("Charlotte, NC", "Greensboro, NC", "North Carolina, United States", "Holly Springs, CA", "Minnetonka, MN",
                    "Garner", "Durhamville, OH", "Denver, CO", "", None):
            self.assertFalse(sweep_common.is_home_region(loc), loc)

    def test_second_metro_is_in_the_area_on_site_too(self):
        # Minneapolis and the towns inside I-494/I-694, any office pattern.
        for loc in ("Minneapolis, MN", "Greater Minneapolis", "Edina, MN", "Bloomington, MN 02139", "Richfield, Minnesota",
                    "Hybrid in Eden Prairie", "Roseville, MN", "Minnetonka, MN"):
            self.assertTrue(sweep_common.is_second_metro(loc), loc)
            self.assertTrue(sweep_common.in_area({"location": loc, "is_remote": "false"}), loc)
        # Same-named places elsewhere, and Minnesota outside I-494/I-694, are not Greater Minneapolis.
        for loc in ("Bloomington, UK", "Roseville, VT", "Woodbury, KY", "Plymouth, IA", "Rochester, MN", "Duluth, MN",
                    "Bloomington", "Durham, NC", "", None):
            self.assertFalse(sweep_common.is_second_metro(loc), loc)
        self.assertFalse(sweep_common.in_area({"location": "Rochester, MN", "is_remote": "false"}))
        self.assertFalse(sweep_common.is_home_region("Minneapolis, MN"))

    def test_normalize_title_strips_a_home_state_location_suffix(self):
        self.assertEqual(sweep_common.normalize_title("IT Support Technician (Durham, NC)"), "it support technician")
        self.assertEqual(sweep_common.normalize_title("Help Desk Analyst (Onsite - North Carolina)"), "help desk analyst")


if __name__ == "__main__":
    unittest.main()
