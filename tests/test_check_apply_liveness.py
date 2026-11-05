"""Regression tests for the curated-Apply liveness checker.

Locks the empirically-validated dead/live signals (2024-10-16) so a future change
can't silently reopen the loophole that let closed roles show as validated-open.
The classifier is pure, so these run without network.
"""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import check_apply_liveness as mod  # noqa: E402


def fetched(status=200, final_url="", body="", error=None):
    return {"status": status, "final_url": final_url, "body": body, "error": error}


class ClassifyTests(unittest.TestCase):
    def test_http_404_is_dead(self):
        role = {"apply_url": "https://job-boards.greenhouse.io/x/jobs/1", "title": "IT Admin"}
        self.assertEqual(mod.classify(role, fetched(status=404)), "dead")

    def test_linkedin_redirected_off_jobs_view_is_dead(self):
        # A removed LinkedIn posting 302s to a search page.
        role = {"apply_url": "https://www.linkedin.com/jobs/view/1000000002", "title": "Systems Administrator"}
        f = fetched(final_url="https://www.linkedin.com/jobs/senior-unix-system-administrator-jobs")
        self.assertEqual(mod.classify(role, f), "dead")

    def test_linkedin_still_on_jobs_view_is_live(self):
        role = {"apply_url": "https://www.linkedin.com/jobs/view/1000000003", "title": "Cybersecurity IAM Analyst"}
        f = fetched(final_url="https://www.linkedin.com/jobs/view/1000000003")
        self.assertEqual(mod.classify(role, f), "live")

    def test_ashby_shell_without_role_tokens_is_dead(self):
        # An Ashby SPA shell: generic <title>Jobs</title>, role absent.
        role = {
            "apply_url": "https://jobs.ashbyhq.com/exampleco/15ad90c1",
            "title": "IT Production Support Technician",
        }
        f = fetched(body="<title>Jobs</title><div>Powered by Ashby</div>")
        self.assertEqual(mod.classify(role, f), "dead")

    def test_ashby_with_role_tokens_is_live(self):
        role = {"apply_url": "https://jobs.ashbyhq.com/acme/uuid", "title": "Platform Reliability Engineer"}
        f = fetched(body='<meta property="og:title" content="Platform Reliability Engineer at Acme">')
        self.assertEqual(mod.classify(role, f), "live")

    def test_greenhouse_role_title_in_title_tag_is_live(self):
        role = {"apply_url": "https://job-boards.greenhouse.io/acme/jobs/1000004", "title": "IT Systems Administrator"}
        f = fetched(body="<title>Job Application for IT Systems Administrator - Okta at Acme</title>")
        self.assertEqual(mod.classify(role, f), "live")

    def test_greenhouse_error_redirect_to_board_is_dead(self):
        # A removed Greenhouse job id 302s to the board with ?error=true (seen in practice);
        # the board page is a 200 without the role's title, which used to read as indeterminate.
        role = {"apply_url": "https://job-boards.greenhouse.io/exampleco/jobs/1000005",
                "title": "Senior IT Support Engineer"}
        f = fetched(final_url="https://job-boards.greenhouse.io/exampleco?error=true", body="<title>Jobs at Exampleco</title>")
        self.assertEqual(mod.classify(role, f), "dead")

    def test_explicit_dead_marker_phrase_is_dead(self):
        role = {"apply_url": "https://apply.workable.com/x/j/CODE", "title": "IT Support Technician III"}
        f = fetched(body="<title>Careers</title><p>This job is no longer accepting applications.</p>")
        self.assertEqual(mod.classify(role, f), "dead")

    def test_network_error_is_indeterminate(self):
        role = {"apply_url": "https://example.com/x", "title": "IT Support"}
        self.assertEqual(mod.classify(role, fetched(status=None, error="TimeoutError: x")), "indeterminate")


class FlaggingTests(unittest.TestCase):
    def test_flag_is_idempotent(self):
        role = {"company": "X", "title": "T", "verified": "Live - checked Jul 30"}
        self.assertTrue(mod.flag_role(role, "2024-10-16"))
        self.assertTrue(role["needs_recheck"])
        self.assertIn(mod.RECHECK_PREFIX, role["verified"])
        # Second pass must not stack another warning.
        self.assertFalse(mod.flag_role(role, "2024-10-17"))
        self.assertEqual(role["verified"].count(mod.RECHECK_PREFIX), 1)

    def test_run_flags_dead_and_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "dashboard").mkdir()
            data = {"apply": [
                {"company": "DeadCo", "title": "IT Production Support Technician",
                 "apply_url": "https://jobs.ashbyhq.com/deadco/uuid", "verified": "Live"},
                {"company": "LiveCo", "title": "IT Systems Administrator",
                 "apply_url": "https://job-boards.greenhouse.io/liveco/jobs/1", "verified": "Live"},
            ], "skip": []}
            (root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps(data), encoding="utf-8")

            def fake_fetch(url):
                if "ashbyhq" in url:
                    return fetched(body="<title>Jobs</title>")  # dead shell
                return fetched(body="<title>IT Systems Administrator at LiveCo</title>")

            s = mod.run(root, "2024-10-16", fetcher=fake_fetch)
            self.assertEqual(s["dead"], 1)
            self.assertEqual(s["live"], 1)
            self.assertEqual(s["flagged"], ["DeadCo"])
            written = json.loads((root / "dashboard" / "job-dashboard-data.json").read_text(encoding="utf-8"))
            self.assertTrue(written["apply"][0]["needs_recheck"])
            self.assertNotIn("needs_recheck", written["apply"][1])
            # Both files go through a temp name and one replace; nothing else may be left behind.
            self.assertEqual(sorted(p.name for p in (root / "dashboard").iterdir()),
                             ["apply-liveness.json", "job-dashboard-data.json"])
            liveness = json.loads((root / "dashboard" / "apply-liveness.json").read_text(encoding="utf-8"))
            self.assertEqual(liveness["https://jobs.ashbyhq.com/deadco/uuid"]["verdict"], "dead")

    def test_run_skips_roles_pipeline_tracks_as_applied(self):
        # Example Staffing: applied, posting expired later. The dashboard already moves such
        # roles out of the Apply column (applied_on on the role; until 2024-12-19 a pipeline.md regex
        # did it too), so the checker must not fail the run on them (it exited 1 every run Sep 1 to 8).
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "dashboard").mkdir()
            data = {"apply": [
                {"company": "Example Staffing (client role)", "title": "Systems Administrator", "applied_on": "2024-09-26",
                 "apply_url": "https://www.linkedin.com/jobs/view/1000000001", "verified": "Live"},
                {"company": "DeadCo", "title": "IT Production Support Technician",
                 "apply_url": "https://jobs.ashbyhq.com/deadco/uuid", "verified": "Live"},
            ], "skip": []}
            (root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "pipeline.md").write_text(
                "| # | Company | Role | Location | Status |\n"
                "| 1 | Example Staffing | Systems Administrator (client role) | Denver, CO | **APPLIED 2024-09-26** |\n",
                encoding="utf-8",
            )

            def fake_fetch(url):
                if "linkedin" in url:  # expired posting: LinkedIn 302s to a search page
                    return fetched(final_url="https://www.linkedin.com/jobs/senior-unix-system-administrator-jobs?trk=expired_jd_redirect")
                return fetched(body="<title>Jobs</title>")  # dead Ashby shell

            s = mod.run(root, "2024-11-05", fetcher=fake_fetch)
            self.assertEqual(s["flagged"], ["DeadCo"])  # an untracked dead role still fails the run
            self.assertEqual(s["dead"], 1)
            written = json.loads((root / "dashboard" / "job-dashboard-data.json").read_text(encoding="utf-8"))
            self.assertNotIn("needs_recheck", written["apply"][0])  # the applied role is left alone
            self.assertTrue(written["apply"][1]["needs_recheck"])
            liveness = json.loads((root / "dashboard" / "apply-liveness.json").read_text(encoding="utf-8"))
            self.assertNotIn("https://www.linkedin.com/jobs/view/1000000001", liveness)

    def test_run_skips_roles_with_the_applied_check_mark(self):
        # The applied_on field is the check mark: no pipeline.md row needed, and no pipeline.md at all here.
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "dashboard").mkdir()
            data = {"apply": [
                {"company": "Acme Robotics", "title": "IT Systems Administrator", "applied_on": "2024-10-29",
                 "apply_url": "https://boards.greenhouse.io/acmerobotics/jobs/1", "verified": "Live"},
            ], "skip": []}
            (root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps(data), encoding="utf-8")
            calls = []

            def fake_fetch(url):
                calls.append(url)
                return fetched(status=404)

            s = mod.run(root, "2024-11-05", fetcher=fake_fetch)
            self.assertEqual(calls, [])  # never fetched: an applied role is not an apply action
            self.assertEqual(s["dead"], 0)
            self.assertEqual(s["records"], 0)


class MainExitTests(unittest.TestCase):
    def test_closed_roles_make_the_step_degraded_not_failed(self):
        # 2024-12-17: three stale curated roles made every night read "1 step failed" since 10/17.
        # Closed roles already show under "Recheck before applying"; the step reports them as a
        # degraded provider on its PROVIDER_STATUS line and exits 0. It fails only when it cannot run.
        import contextlib, io
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "dashboard").mkdir()
            data = {"apply": [
                {"company": "DeadCo", "title": "IT Production Support Technician",
                 "apply_url": "https://jobs.ashbyhq.com/deadco/uuid", "verified": "Live"},
                {"company": "LiveCo", "title": "IT Systems Administrator",
                 "apply_url": "https://job-boards.greenhouse.io/liveco/jobs/1", "verified": "Live"},
            ], "skip": []}
            (root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps(data), encoding="utf-8")

            def fake_fetch(url):
                return fetched(body="<title>Jobs</title>") if "ashbyhq" in url else fetched(body="<title>IT Systems Administrator at LiveCo</title>")

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(mod, "fetch", fake_fetch), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mod.main(["--root", str(root), "--date", "2024-12-17"])
            self.assertEqual(code, 0)
            status_line = next(l for l in out.getvalue().splitlines() if l.startswith("PROVIDER_STATUS "))
            status = json.loads(status_line[len("PROVIDER_STATUS "):])
            self.assertEqual(status["state"], "degraded")
            self.assertEqual(status["providers"]["curated_apply"]["reason"], "1 closed: DeadCo")
            self.assertIn("flagged closed: DeadCo", err.getvalue())

            clean = json.loads(mod.status_line({"dead": 0, "flagged": []})[len("PROVIDER_STATUS "):])
            self.assertEqual((clean["state"], clean["providers"]["curated_apply"]["reason"]), ("ok", ""))

    def test_the_step_fails_when_it_cannot_run(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):  # no data file: a non-zero exit, nothing silent
                mod.main(["--root", d, "--date", "2024-12-17"])


if __name__ == "__main__":
    unittest.main()
