import csv
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import market_snapshot


class MarketSnapshotTests(unittest.TestCase):
    """The Monday review's Raleigh-Durham numbers (2024-12-09)."""

    TODAY = dt.date(2024, 12, 16)
    FIELDS = ("title", "company", "location", "job_url", "site", "is_remote", "pay_interval")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "weekly-sweeps").mkdir()
        (self.root / "dashboard").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def pull(self, day, rows):
        with (self.root / "weekly-sweeps" / f"linkedin-raw-{day}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.FIELDS))
            writer.writeheader()
            writer.writerows(rows)

    def row(self, company, link, location="Durham, NC", site="linkedin", title="IT Support Specialist", pay=""):
        return {"title": title, "company": company, "location": location, "job_url": f"https://j/{link}", "site": site,
                "is_remote": "false", "pay_interval": pay}

    def test_counts_the_week_in_the_area(self):
        self.pull("2024-11-28", [self.row("Ghostco", "g0")])  # earlier sighting, outside the week
        self.pull("2024-12-11", [
            self.row("Ghostco", "g1"),
            self.row("Kforce", "k1", site="indeed", pay="hourly"),
            self.row("Remoteco", "r1", location="Remote (Raleigh, NC)"),  # remote, though it names a city here
            self.row("Denverco", "a1", location="Denver, CO"),            # outside the owner's area
            self.row("Nurseco", "n1", title="Registered Nurse"),         # not an IT title
            self.row("Outofstate", "o1", location="Remote, US", site="workday:exampleuniversity"),  # remote at a known out-of-state HQ
        ])
        self.pull("2024-12-13", [self.row("Ghostco", "g1")])  # the same link again is not a new job
        (self.root / "dashboard" / "queue-seen.json").write_text(json.dumps({
            "a": {"checked_on": "2024-12-12", "verdict": "apply_now"},
            "b": {"checked_on": "2024-12-12", "verdict": "skip"},
            "c": {"checked_on": "2024-11-28", "verdict": "apply_now"},  # last week
        }), encoding="utf-8")
        (self.root / "dashboard" / "decisions.json").write_text(json.dumps({
            "x": {"decision": "applied", "on": "2024-12-14"}, "y": {"decision": "skip", "on": "2024-12-15"},
            "z": {"decision": "later", "on": "2024-11-27"},
        }), encoding="utf-8")
        outside = lambda company: company == "Outofstate"
        with mock.patch.object(market_snapshot.employer_hq, "known_outside_home_state", side_effect=outside):
            s = market_snapshot.snapshot(self.root, self.TODAY)
        self.assertEqual((s["pulls"], s["jobs"], s["links"]), (2, 4, 4))
        # 2024-12-23: the widened rule counts any North Carolina brick-and-mortar, so nothing is removed for
        # headquarters alone; the snapshot instead counts the remote roles that need the owner's branch check.
        self.assertEqual((s["home_region"], s["remote"], s["home_state_branch_check"]), (2, 2, 1))
        self.assertEqual(s["by_source"], {"LinkedIn": 2, "Indeed": 1, "company job boards": 1})
        self.assertEqual((s["agencies"], s["agencies_hourly"]), (1, 1))
        self.assertEqual(s["verdicts"], {"apply_now": 1, "skip": 1})
        self.assertEqual((s["reposted"], s["top_reposters"]), (1, [("Ghostco", 1)]))
        self.assertEqual(s["clicks"], {"applied": 1, "later": 0, "skip": 1})
        text = market_snapshot.render(s)
        self.assertIn("IT jobs in your area: 4", text)
        self.assertNotIn("—", text)  # no em-dashes in anything the owner reads


if __name__ == "__main__":
    unittest.main()
