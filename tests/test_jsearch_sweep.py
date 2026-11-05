import contextlib
import csv
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

from scripts import jsearch_sweep
from scripts import rate_control as rc


def http_error(code: int, msg: str = "error") -> urllib.error.HTTPError:
    # An explicit empty body: with fp=None HTTPError opens a temporary file and the suite prints ResourceWarnings.
    return urllib.error.HTTPError("https://api.openwebninja.com/jsearch/search-v2", code, msg, None, io.BytesIO(b""))


def provider(text: str) -> dict | None:
    """The jsearch entry of the last PROVIDER_STATUS line in a run's output, or None when there is none."""
    lines = [line for line in text.splitlines() if line.startswith(rc.STATUS_PREFIX)]
    if not lines:
        return None
    return json.loads(lines[-1][len(rc.STATUS_PREFIX):])["providers"]["jsearch"]


def job(**overrides):
    """One Google for Jobs listing as JSearch returned it on 2024-12-10 (trimmed)."""
    base = {
        "job_title": "System Administrator - Catholic Health Services",
        "employer_name": "Catholic Health Services",
        "job_location": "Knightdale, NC",
        "job_is_remote": False,
        "job_posted_at_datetime_utc": "2024-12-08T00:00:00.000Z",
        "job_min_salary": None, "job_max_salary": None, "job_salary_period": None,
        "job_apply_link": "https://www.indeed.com/viewjob?jk=89b85cd7d1111b6c&utm_campaign=google_jobs_apply"
                          "&utm_source=google_jobs_apply&utm_medium=organic",
        "apply_options": [{"publisher": "Indeed", "apply_link": "https://www.indeed.com/viewjob?jk=89b85cd7d1111b6c",
                           "is_direct": False}],
    }
    base.update(overrides)
    return base


class JSearchMappingTests(unittest.TestCase):
    def test_row_matches_the_pull_columns(self):
        row = jsearch_sweep.job_to_row(job())
        self.assertEqual(row["title"], "System Administrator")  # employer suffix dropped so dedupe matches
        self.assertEqual(row["job_url"], "https://www.indeed.com/viewjob?jk=89b85cd7d1111b6c")  # tracking gone, jk kept
        self.assertEqual((row["site"], row["is_remote"], row["date_posted"]), ("jsearch", "false", "2024-12-08"))
        self.assertEqual((row["min_amount"], row["pay_interval"], row["job_url_direct"]), ("", "", ""))

    def test_hourly_pay_is_yearly_with_the_published_interval(self):
        row = jsearch_sweep.job_to_row(job(job_min_salary=22.71, job_max_salary=29.52, job_salary_period="HOUR"))
        self.assertEqual((row["min_amount"], row["max_amount"], row["pay_interval"]), ("47237", "61402", "hourly"))

    def test_remote_wording_and_direct_employer_link(self):
        row = jsearch_sweep.job_to_row(job(
            job_location="Anywhere", job_is_remote=True,
            apply_options=[{"publisher": "Acme Careers", "is_direct": True,
                            "apply_link": "https://acme.example/jobs/1?utm_source=google_jobs_apply"}]))
        self.assertEqual((row["location"], row["is_remote"]), ("Remote", "true"))
        self.assertEqual(row["job_url_direct"], "https://acme.example/jobs/1")

    def test_linkedin_links_take_the_pulls_short_form(self):
        row = jsearch_sweep.job_to_row(job(job_apply_link="https://www.linkedin.com/jobs/view/"
                                                          "it-support-specialist-hybrid-durham-nc-at-e-bit-consulting-4478573929"
                                                          "?utm_campaign=google_jobs_apply"))
        self.assertEqual(row["job_url"], "https://www.linkedin.com/jobs/view/4478573929")


class JSearchRunTests(unittest.TestCase):
    def run_main(self, root, search, argv=()):
        calls = []

        def fake_search(key, query):
            calls.append(query)
            return search(query)

        out = io.StringIO()
        with mock.patch.object(jsearch_sweep, "get_key", return_value="k"), \
                mock.patch.object(jsearch_sweep, "project_root", return_value=root), \
                mock.patch.object(jsearch_sweep, "search", side_effect=fake_search), \
                contextlib.redirect_stdout(out):
            code = jsearch_sweep.main(list(argv))
        return code, calls, out.getvalue()

    @staticmethod
    def csv_rows(root):
        daily = root / "weekly-sweeps" / f"linkedin-raw-{dt.date.today()}.csv"
        if not daily.exists():
            return []
        with daily.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def marker(root):
        return root / "automation-logs" / "jsearch-last-run.txt"

    def test_keeps_support_titles_merges_once_and_runs_once_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listings = [job(), job(job_title="2026 Summer Sales Intern", employer_name="Lotus Sales")]
            code, calls, text = self.run_main(root, lambda query: listings)
            self.assertEqual((code, len(calls)), (0, 3))
            self.assertEqual([(r["company"], r["title"], r["site"]) for r in self.csv_rows(root)],
                             [("Catholic Health Services", "System Administrator", "jsearch")])
            self.assertEqual(provider(text), {"state": rc.OK, "reason": ""})  # a healthy run says so
            code, calls, _ = self.run_main(root, lambda query: listings)  # a rerun the same morning spends nothing
            self.assertEqual((code, calls), (0, []))
            code, calls, _ = self.run_main(root, lambda query: listings, argv=["--force"])
            self.assertEqual((code, len(calls)), (0, 3))

    # 2024-12-30: JSearch is one of six sources. An external failure is a degraded provider and exit 0, so
    # the night and the GitHub run stay green; a rejected token is failed and exit 1; a code bug crashes.

    def test_a_run_where_every_search_failed_is_degraded_and_can_retry_the_same_day(self):
        def offline(query):
            raise urllib.error.URLError("offline")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, calls, text = self.run_main(root, offline)
            self.assertEqual((code, len(calls)), (0, 3))
            self.assertFalse(self.marker(root).exists())  # nothing succeeded: a later pass may retry
            self.assertEqual(provider(text), {"state": rc.DEGRADED, "reason": "3 of 3 searches: network_error"})

    def test_a_partial_failure_merges_the_rest_and_reports_degraded(self):
        def flaky(query):
            if query == jsearch_sweep.QUERIES[1]:
                raise http_error(429, "Too Many Requests")
            return [job()]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, calls, text = self.run_main(root, flaky)
            self.assertEqual((code, len(calls)), (0, 3))
            self.assertEqual(len(self.csv_rows(root)), 1)  # the two working searches merged (one job, deduplicated)
            self.assertTrue(self.marker(root).exists())
            self.assertEqual(provider(text), {"state": rc.DEGRADED, "reason": "1 of 3 searches: http_429"})

    def test_a_rejected_token_is_failed_and_exits_1(self):
        for code_ in (401, 403):
            def rejected(query, code_=code_):
                raise http_error(code_, "Unauthorized")

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                code, calls, text = self.run_main(root, rejected)
                self.assertEqual((code, len(calls)), (1, 3), code_)
                self.assertFalse(self.marker(root).exists())
                self.assertEqual(provider(text), {"state": rc.FAILED, "reason": f"3 of 3 searches: http_{code_}"})

    def test_the_reason_names_the_rejected_token_not_an_earlier_429(self):
        def mixed(query):
            if query == jsearch_sweep.QUERIES[0]:
                raise http_error(429, "Too Many Requests")
            if query == jsearch_sweep.QUERIES[1]:
                raise http_error(401, "Unauthorized")
            return [job()]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, calls, text = self.run_main(root, mixed)
            self.assertEqual((code, len(calls)), (1, 3))
            self.assertEqual(len(self.csv_rows(root)), 1)  # the third search's rows still merged
            self.assertEqual(provider(text), {"state": rc.FAILED, "reason": "2 of 3 searches: http_401"})

    def test_an_unexpected_error_still_crashes(self):
        # A schema change: apply_options is no longer a list. That is a code problem, not a provider one,
        # so main() must not swallow it into a yellow banner.
        broken = [job(apply_options=5)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = io.StringIO()
            with mock.patch.object(jsearch_sweep, "get_key", return_value="k"), \
                    mock.patch.object(jsearch_sweep, "project_root", return_value=root), \
                    mock.patch.object(jsearch_sweep, "search", return_value=broken), \
                    contextlib.redirect_stdout(out), \
                    self.assertRaises(TypeError):
                jsearch_sweep.main([])
            self.assertIsNone(provider(out.getvalue()))
            self.assertFalse(self.marker(root).exists())

    def test_classify_keeps_code_errors_out_of_the_provider_line(self):
        self.assertEqual(jsearch_sweep.classify(http_error(503)), (rc.DEGRADED, "http_503"))
        self.assertEqual(jsearch_sweep.classify(http_error(403)), (rc.FAILED, "http_403"))
        self.assertEqual(jsearch_sweep.classify(TimeoutError("timed out")), (rc.DEGRADED, "network_error"))
        self.assertEqual(jsearch_sweep.classify(json.JSONDecodeError("x", "{", 0)), (rc.DEGRADED, "bad_json"))
        self.assertIsNone(jsearch_sweep.classify(KeyError("data")))

    def test_daily_searches_fit_the_free_plan(self):
        # The owner's free OpenWeb Ninja plan allows about 100 requests a month: one page per search, every day.
        self.assertEqual(jsearch_sweep.PARAMS["num_pages"], "1")
        self.assertLessEqual(len(jsearch_sweep.QUERIES) * 31, 100)
        self.assertTrue(all(query.endswith("in Raleigh, NC") for query in jsearch_sweep.QUERIES))


if __name__ == "__main__":
    unittest.main()
