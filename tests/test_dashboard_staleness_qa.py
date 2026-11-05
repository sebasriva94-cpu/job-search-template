"""QA regression tests for the 2024-10-29 staleness review.

Each test reproduces one bug found in the review (written red first, before the fixes):
a flagged-closed role rendering as a green Apply, the needs-validation queue picked by CSV
order instead of posting date, and the relevance filter admitting non-IT "systems" titles.
They stay as regression locks for scripts/job_dashboard.py and scripts/sweep_common.py.
"""
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "job_dashboard.py"


def load_module():
    spec = importlib.util.spec_from_file_location("job_dashboard", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_row(company, title, date_posted, location="Durham, NC"):
    return {
        "title": title, "company": company, "location": location, "date_posted": date_posted,
        "is_remote": "false", "job_url": f"https://example.com/{company.lower().replace(' ', '-')}",
        "site": "linkedin",
    }


class Bug1FlaggedRoleStillRendersAsApply(unittest.TestCase):
    """check_apply_liveness sets needs_recheck=True, but the renderer ignores it."""

    def setUp(self):
        self.m = load_module()
        self.role = {
            "company": "Example Staffing (client role)", "title": "Systems Administrator",
            "location": "Durham, NC", "work_model": "Onsite", "salary": "$90,000",
            "apply_url": "https://www.linkedin.com/jobs/view/1000000001",
            "verified": "⚠ NEEDS RECHECK — auto-liveness check found this posting closed (2024-10-28).",
            "needs_recheck": True,
            "link_check": {"ok": True, "status": 200, "final_url": "", "error": ""},
        }

    def test_flagged_role_does_not_get_green_checkmark_badge(self):
        card = self.m.apply_card(self.role)
        self.assertNotIn('<span class="badge">✓', card,
                         "a needs_recheck role must not carry the green verified checkmark")

    def test_flagged_role_does_not_claim_link_checked_ok(self):
        card = self.m.apply_card(self.role)
        self.assertNotIn("official apply link checked", card,
                         "HTTP 200 on a LinkedIn redirect is not proof the posting is open")

    def test_markdown_marks_flagged_role_as_recheck(self):
        data = {"apply": [self.role], "skip": []}
        md = self.m.render_markdown(data, [])
        line = next(l for l in md.splitlines() if "Example Staffing" in l and l.startswith("###"))
        self.assertIn("RECHECK", line.upper(),
                      "the role heading in job-dashboard.md must say the role needs a recheck")


class Bug2QueuePickedByFileOrderNotFreshness(unittest.TestCase):
    """unvalidated_from_raw takes the first N CSV rows; it must take the N freshest."""

    def setUp(self):
        self.m = load_module()

    def test_newest_rows_win_over_older_rows_that_appear_earlier_in_file(self):
        rows = [raw_row(f"OldCo{i}", "IT Support Specialist", "2024-10-26") for i in range(15)]
        rows.append(raw_row("Capgemini", "Service Desk Analyst", "2024-10-28"))
        queue = self.m.unvalidated_from_raw(rows, set(), limit=15)
        companies = [r["company"] for r in queue]
        self.assertIn("Capgemini", companies, "an Aug 31 row was cut off by Aug 29 rows above it")

    def test_undated_rows_sort_after_dated_rows(self):
        rows = [raw_row("PEAK6", "IT Operations Specialist", "")]
        rows += [raw_row(f"DatedCo{i}", "IT Support Specialist", "2024-10-28") for i in range(15)]
        queue = self.m.unvalidated_from_raw(rows, set(), limit=15)
        self.assertNotIn("PEAK6", [r["company"] for r in queue],
                         "a row with no posting date must not displace a dated row")

    def test_queue_is_ordered_newest_first(self):
        rows = [raw_row("A", "IT Support Specialist", "2024-10-26"),
                raw_row("B", "IT Support Specialist", "2024-10-29"),
                raw_row("C", "IT Support Specialist", "2024-10-28")]
        dates = [r["date_posted"] for r in self.m.unvalidated_from_raw(rows, set())]
        self.assertEqual(dates, sorted(dates, reverse=True))


class Bug2bRelevanceFilterTooBroad(unittest.TestCase):
    """Bare 'systems' / 'technician' keywords admit engineering and trades roles."""

    def setUp(self):
        self.m = load_module()

    def test_non_it_systems_titles_are_excluded(self):
        for company, title in [
            ("ERCOT", "Power Systems Modeling Engineer"),
            ("Hudson River Trading", "Trading Systems Engineer"),
            ("Tesla", "Quality Systems Engineer, Cell Global Quality"),
            ("Amazon", "Robotics Systems Engineer II"),
            ("White Lodging", "Senior Maintenance Technician"),
            ("Collins Aerospace", "Mechanical Manufacturing Technician I"),
        ]:
            with self.subTest(title=title):
                self.assertFalse(self.m.is_probably_relevant(raw_row(company, title, "2024-10-28")))

    def test_real_it_titles_still_pass(self):
        for company, title in [
            ("Capgemini", "Service Desk Analyst"),
            ("R.E. Michel Company", "IT Support Tech- Tier II"),
            ("Acme Robotics", "IT Systems Administrator"),
            ("Acme Translate", "IT Support Engineer"),
            ("WestStar Credit Union", "Information Technology Support Technician"),
        ]:
            with self.subTest(title=title):
                self.assertTrue(self.m.is_probably_relevant(raw_row(company, title, "2024-10-28")))


if __name__ == "__main__":
    unittest.main()
