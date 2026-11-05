"""The state carrier for the GitHub Actions runner (scripts/automation/state_sync.py, 2024-12-23)."""
import datetime as dt
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("state_sync", ROOT / "scripts" / "automation" / "state_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "checkout"
        self.state = base / "state"
        for folder in ("dashboard", "automation-logs", "weekly-sweeps"):
            (self.root / folder).mkdir(parents=True)
        self.today = dt.date(2024, 12, 23)
        self.module = load_module()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_save_keeps_the_state_files_three_weeks_of_pulls_and_two_months_of_logs(self):
        for rel in self.module.STATE_FILES:
            self.write(rel)
        self.write("dashboard/decisions.json")                 # tracked on main, not state
        self.write("weekly-sweeps/linkedin-raw-2024-12-23.csv")
        self.write("weekly-sweeps/linkedin-raw-2024-12-03.csv")  # 20 days: kept
        self.write("weekly-sweeps/linkedin-raw-2024-12-02.csv")  # 21 days: dropped
        self.write("weekly-sweeps/linkedin-raw-2024-12-17_filtered.csv")  # not a pull
        self.write("weekly-sweeps/.lock")
        self.write("automation-logs/linkedin-pull-2024-12.log")
        self.write("automation-logs/linkedin-pull-2024-11.log")
        self.write("automation-logs/linkedin-pull-2024-10.log")  # two months back: dropped
        self.write("automation-logs/linkedin-feed-2024-12.jsonl")
        self.write("automation-logs/steps/2024-12-23-judge.out.txt")

        self.state.mkdir()
        (self.state / ".git").mkdir()
        (self.state / "weekly-sweeps").mkdir()
        (self.state / "weekly-sweeps" / "linkedin-raw-2024-11-27.csv").write_text("old", encoding="utf-8")

        saved = self.module.save(self.root, self.state, self.today)

        self.assertEqual(saved, self.module.select(self.root, self.today))  # a stable, documented order
        self.assertEqual(saved[:len(self.module.STATE_FILES)], list(self.module.STATE_FILES))
        for rel in self.module.STATE_FILES:
            self.assertIn(rel, saved)
        self.assertIn("weekly-sweeps/linkedin-raw-2024-12-23.csv", saved)
        self.assertIn("weekly-sweeps/linkedin-raw-2024-12-03.csv", saved)
        self.assertNotIn("weekly-sweeps/linkedin-raw-2024-12-02.csv", saved)
        self.assertNotIn("weekly-sweeps/linkedin-raw-2024-12-17_filtered.csv", saved)
        self.assertIn("automation-logs/linkedin-pull-2024-12.log", saved)
        self.assertIn("automation-logs/linkedin-pull-2024-11.log", saved)
        self.assertNotIn("automation-logs/linkedin-pull-2024-10.log", saved)
        self.assertIn("automation-logs/linkedin-feed-2024-12.jsonl", saved)
        self.assertFalse(any("decisions" in s or "steps/" in s or ".lock" in s for s in saved))
        # the folder now holds exactly the selection, a README and the untouched .git
        self.assertFalse((self.state / "weekly-sweeps" / "linkedin-raw-2024-11-27.csv").exists())
        self.assertTrue((self.state / ".git").is_dir())
        self.assertTrue((self.state / "dashboard" / "verdicts.db").is_file())
        self.assertIn("2024-12-23", (self.state / "README.md").read_text(encoding="utf-8"))

    def test_restore_copies_what_is_present_and_nothing_else(self):
        (self.state / "dashboard").mkdir(parents=True)
        (self.state / "dashboard" / "verdicts.db").write_bytes(b"sqlite")
        (self.state / "weekly-sweeps").mkdir()
        (self.state / "weekly-sweeps" / "linkedin-raw-2024-12-22.csv").write_text("rows", encoding="utf-8")
        (self.state / ".git").mkdir()
        (self.state / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (self.state / "README.md").write_text("about", encoding="utf-8")

        restored = self.module.restore(self.state, self.root)

        self.assertEqual(restored, ["dashboard/verdicts.db", "weekly-sweeps/linkedin-raw-2024-12-22.csv"])
        self.assertEqual((self.root / "dashboard" / "verdicts.db").read_bytes(), b"sqlite")
        self.assertFalse((self.root / "README.md").exists())
        self.assertFalse((self.root / ".git").exists())

    def test_first_run_has_no_state_folder_content(self):
        self.state.mkdir()
        self.assertEqual(self.module.restore(self.state, self.root), [])

    def test_month_window_crosses_the_year(self):
        self.assertEqual(self.module._months_kept(dt.date(2025, 3, 2)), {(2025, 3), (2025, 2)})


if __name__ == "__main__":
    unittest.main()
