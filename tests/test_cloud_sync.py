import json
import pathlib
import subprocess
import tempfile
import unittest

from scripts import cloud_sync


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class CloudSyncTests(unittest.TestCase):
    """The laptop keeps the cloud brief's private repo current (2024-12-09)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "Job Search"
        for folder in ("automation-logs", "dashboard", "interview-prep/packs", "weekly-reviews", "resumes"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "pipeline.md").write_text("| Company | Status |\n| Acme | applied |\n", encoding="utf-8")
        (self.root / "job-dashboard.md").write_text("# Job Dashboard\n", encoding="utf-8")
        (self.root / "automation-logs" / "pipeline-status.json").write_text(json.dumps({"run_date": "2024-12-09"}), encoding="utf-8")
        (self.root / "interview-prep" / "packs" / "2024-11-28-acme-panel.html").write_text("<p>pack</p>", encoding="utf-8")
        (self.root / "resumes" / "resume.docx").write_text("private", encoding="utf-8")
        self.remote = base / "remote.git"
        run_git(base, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        self.repo = base / "private-brief-repo"
        self.repo.mkdir()
        self.init_clone(self.repo)
        run_git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "README.md").write_text("job brief\n", encoding="utf-8")
        run_git(self.repo, "add", "README.md")
        run_git(self.repo, "commit", "--quiet", "-m", "start")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def init_clone(path):
        run_git(path, "init", "--quiet", "-b", "main")
        run_git(path, "config", "user.name", "test")
        run_git(path, "config", "user.email", "test@example.com")

    def remote_files(self):
        return sorted(run_git(self.repo, "ls-tree", "-r", "--name-only", "origin/main").split())

    def test_pushes_only_the_brief_inputs_and_pack_names(self):
        result = cloud_sync.sync(self.root, self.repo)
        self.assertTrue(result["committed"] and result["pushed"])
        run_git(self.repo, "fetch", "--quiet", "origin")
        self.assertEqual(self.remote_files(), ["README.md", "inputs/job-dashboard.md", "inputs/market-snapshot.md",
                                               "inputs/pipeline-status.json", "inputs/pipeline.md", "inputs/prep-packs.txt"])
        self.assertEqual((self.repo / "inputs" / "prep-packs.txt").read_text(encoding="utf-8"), "2024-11-28-acme-panel.html\n")
        # The Monday brief's weekly numbers (2024-12-10): counts only, and an empty week still renders.
        snapshot = (self.repo / "inputs" / "market-snapshot.md").read_text(encoding="utf-8")
        self.assertTrue(snapshot.startswith("Raleigh-Durham market snapshot, "), snapshot)
        self.assertIn("IT jobs in your area: 0", snapshot)
        again = cloud_sync.sync(self.root, self.repo)
        self.assertFalse(again["committed"] or again["pushed"])  # nothing changed, nothing sent

    def test_cloud_briefs_come_back_without_overwriting_a_laptop_brief(self):
        cloud_sync.sync(self.root, self.repo)
        cloud = pathlib.Path(self.tmp.name) / "cloud"
        run_git(pathlib.Path(self.tmp.name), "clone", "--quiet", str(self.remote), str(cloud))
        run_git(cloud, "config", "user.name", "cloud")
        run_git(cloud, "config", "user.email", "cloud@example.com")
        (cloud / "briefs").mkdir()
        (cloud / "briefs" / "daily-brief-2024-12-10.md").write_text("cloud 13\n", encoding="utf-8")
        (cloud / "briefs" / "daily-brief-2024-12-11.md").write_text("cloud 14\n", encoding="utf-8")
        run_git(cloud, "add", "briefs")
        run_git(cloud, "commit", "--quiet", "-m", "Daily brief")
        run_git(cloud, "push", "--quiet", "origin", "main")
        reviews = self.root / "weekly-reviews"
        (reviews / "daily-brief-2024-12-10.md").write_text("laptop 13\n", encoding="utf-8")
        (self.root / "pipeline.md").write_text("| Company | Status |\n| Acme | interview |\n", encoding="utf-8")
        result = cloud_sync.sync(self.root, self.repo)
        self.assertEqual(sorted(result["briefs_copied"]), ["daily-brief-2024-12-10-cloud.md", "daily-brief-2024-12-11.md"])
        self.assertEqual((reviews / "daily-brief-2024-12-10.md").read_text(encoding="utf-8"), "laptop 13\n")
        self.assertTrue(result["pushed"])
        run_git(cloud, "pull", "--quiet", "origin", "main")
        self.assertIn("interview", (cloud / "inputs" / "pipeline.md").read_text(encoding="utf-8"))
        self.assertEqual(cloud_sync.sync(self.root, self.repo)["briefs_copied"], [])


if __name__ == "__main__":
    unittest.main()
