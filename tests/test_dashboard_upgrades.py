"""Tests for the 2024-10-29 dashboard upgrades.

Covers: recheck/stale badges with the structured verified_on date, the freshness-ranked
queue, the queue memory (auto-expiry, remembered-dead), the listing check with a fake
checker, and the pipeline-health banner. No network.
"""
import csv
import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "job_dashboard.py"

TODAY = dt.date(2024, 10, 29)
NOW = dt.datetime(2024, 10, 29, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))


def load_module():
    spec = importlib.util.spec_from_file_location("job_dashboard", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_row(company, title, date_posted="2024-10-28", location="Durham, NC"):
    return {
        "title": title, "company": company, "location": location, "date_posted": date_posted,
        "is_remote": "false", "job_url": f"https://example.com/{company.lower()}", "site": "linkedin",
    }


class CuratedStatusTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def test_recent_verified_on_is_ok(self):
        status = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-22"}, {}, TODAY)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["badge"], "✓ Live")

    def test_exactly_fourteen_days_is_still_ok(self):
        status = self.m.curated_status({"verified_on": "2024-10-15"}, {}, TODAY)
        self.assertEqual(status["state"], "ok")

    def test_older_than_fourteen_days_is_stale(self):
        status = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-12"}, {}, TODAY)
        self.assertEqual(status["state"], "stale")
        self.assertIn("re-verify", status["badge"])
        self.assertIn("17 days ago", status["badge"])

    def test_falls_back_to_file_level_last_validated(self):
        status = self.m.curated_status({"verified": "Live"}, {"last_validated": "2024-10-12"}, TODAY)
        self.assertEqual(status["state"], "stale")

    def test_needs_recheck_wins_over_a_fresh_date(self):
        status = self.m.curated_status({"needs_recheck": True, "verified_on": "2024-10-28"}, {}, TODAY)
        self.assertEqual(status["state"], "recheck")
        self.assertEqual(status["badge"], "⚠ Recheck before applying")

    # Curated roles follow the search rules too.
    def test_older_than_the_posting_age_rule_leaves_apply_as_expired(self):
        status = self.m.curated_status({"verified": "Live", "verified_on": "2024-09-28", "location": "Remote, US"}, {}, TODAY)
        self.assertEqual(status["state"], "recheck")
        self.assertIn("Expired: verified 2024-09-28, 31 days ago", status["badge"])
        stale = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-12", "location": "Remote, US"}, {}, TODAY)
        self.assertEqual(stale["state"], "stale")  # 17 days: stale but not expired

    def test_out_of_area_location_leaves_apply(self):
        status = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-28", "location": "Denver, CO (hybrid)"}, {}, TODAY)
        self.assertEqual(status["state"], "recheck")
        self.assertIn("Out of area: Denver, CO (hybrid)", status["badge"])
        for location, extra in (("Chapel Hill, NC", {}), ("Remote, US", {}), ("United States", {"work_model": "Remote"})):
            with self.subTest(location=location):
                ok = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-28", "location": location, **extra}, {}, TODAY)
                self.assertEqual(ok["state"], "ok")
        no_location = self.m.curated_status({"verified": "Live", "verified_on": "2024-10-28"}, {}, TODAY)
        self.assertEqual(no_location["state"], "ok")  # nothing to judge: the old fixtures keep working


class RenderSectionsTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def data(self):
        return {"last_validated": "2024-10-16", "apply": [
            {"company": "OkCo", "title": "IT Support Specialist", "verified": "Live",
             "verified_on": "2024-10-27", "apply_url": "https://ok.example/1"},
            {"company": "OldCo", "title": "Systems Administrator", "verified": "Live",
             "verified_on": "2024-10-12", "apply_url": "https://old.example/1"},
            {"company": "DeadCo", "title": "Desktop Support", "needs_recheck": True,
             "verified": "⚠ NEEDS RECHECK — closed", "apply_url": "https://www.linkedin.com/jobs/view/1"},
            {"company": "ExpiredCo", "title": "IT Analyst", "verified": "Live", "location": "Remote, US",
             "verified_on": "2024-09-28", "apply_url": "https://expired.example/1"},
            {"company": "MileHighCo", "title": "IT Technician", "verified": "Live", "location": "Denver, CO (hybrid)",
             "verified_on": "2024-10-27", "apply_url": "https://tex.example/1"},
        ], "skip": []}

    def test_html_moves_flagged_role_out_of_apply_and_counts_it(self):
        html = self.m.render_html(self.data(), [], today=TODAY)
        self.assertIn("Apply to 2 curated, recheck 3, skip 0", html)
        self.assertLess(html.index("Recheck before applying</h2>"), html.index("DeadCo"))
        self.assertIn('class="badge stale"', html)
        self.assertIn("Stale · re-verify", html)
        self.assertIn("Open listing to recheck", html)

    def test_markdown_marks_stale_and_recheck_roles(self):
        md = self.m.render_markdown(self.data(), [], today=TODAY)
        self.assertIn("### OldCo — Systems Administrator ⏳ STALE, RE-VERIFY", md)
        self.assertIn("## ⚠ Recheck before applying", md)
        self.assertIn("### DeadCo — Desktop Support ⚠ RECHECK", md)
        self.assertIn("recheck 3", md)

    def test_check_line_prefers_liveness_verdict_over_http_status(self):
        role = {"apply_url": "https://www.linkedin.com/jobs/view/1", "link_check": {"ok": True, "status": 200}}
        records = {role["apply_url"]: {"checked_on": "2024-10-29", "verdict": "dead"}}
        css, text = self.m.check_line(role, records)
        self.assertEqual(css, "warn")
        self.assertIn("closed or redirected on linkedin.com", text)
        self.assertEqual(self.m.check_line(role, {}), ("ok", "official apply link checked · HTTP 200"))

    def test_queue_card_shows_memory_and_check_labels(self):
        row = {"company": "Co", "title": "IT Support", "location": "Durham, NC", "date_posted": "2024-10-28",
               "job_url": "https://x", "reason": "r", "shown_label": "new today",
               "check_label": "listing checked live today"}
        card = self.m.needs_validation_card(row)
        self.assertIn("new today", card)
        self.assertIn("listing checked live today", card)


class QueueMemoryTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def cands(self, n, date="2024-10-28"):
        return [self.m.raw_candidate(raw_row(f"Co{i}", "IT Support Specialist", date)) for i in range(n)]

    def test_first_render_marks_new_today_and_records_first_seen(self):
        seen = {}
        sel = self.m.select_queue(self.cands(3), seen, TODAY)
        self.assertEqual([c["shown_label"] for c in sel["queue"]], ["new today"] * 3)
        self.assertEqual(seen[self.m.seen_key(sel["queue"][0])]["first_seen"], "2024-10-29")
        self.assertEqual(sel["queue"][0]["check_label"], "listing not auto-checked")

    def test_candidate_shown_for_more_than_seven_days_expires(self):
        cands = self.cands(2)
        key = self.m.seen_key(cands[0])
        seen = {key: {"first_seen": "2024-10-17", "last_seen": "2024-10-28"}}
        sel = self.m.select_queue(cands, seen, TODAY)
        self.assertEqual(sel["expired"], 1)
        self.assertEqual([c["company"] for c in sel["queue"]], ["Co1"])
        self.assertEqual(seen[key]["last_seen"], "2024-10-29")  # still remembered, stays hidden

    def test_validated_roles_carry_over_after_the_feed_drops_them(self):
        # The feed only holds 72 hours of postings; a validated role must stay for its 7-day window.
        seen = {
            "flywire|technical support engineer ii": {
                "verdict": "apply_now", "first_seen": "2024-10-26", "checked_on": "2024-10-28",
                "company": "Flywire", "title": "Technical Support Engineer II",
                "job_url": "https://example.com/flywire", "facts": {"location": "Durham, NC"}},
            "oldco|it specialist": {"verdict": "apply_now", "first_seen": "2024-10-17",
                                    "company": "OldCo", "title": "IT Specialist"},  # window closed
            "skipco|it specialist": {"verdict": "skip", "skipped_on": "2024-10-27", "first_seen": "2024-10-27",
                                     "company": "SkipCo", "title": "IT Specialist"},
            "curatedco|it specialist": {"verdict": "apply_now", "first_seen": "2024-10-27",
                                        "company": "CuratedCo", "title": "IT Specialist"},
            "infeed|it specialist": {"verdict": "apply_now", "first_seen": "2024-10-28",
                                     "company": "InFeed", "title": "IT Specialist"},
        }
        carried = self.m.carried_candidates(seen, TODAY, {("curatedco", "it specialist")}, exclude={"infeed|it specialist"})
        self.assertEqual([c["company"] for c in carried], ["Flywire"])
        self.assertEqual((carried[0]["job_url"], carried[0]["location"]), ("https://example.com/flywire", "Durham, NC"))
        sel = self.m.select_queue(carried, seen, TODAY, checker=lambda c: {"verdict": "apply_now", "reasons": [], "facts": {}})
        self.assertEqual([(c["company"], c["tier"]) for c in sel["queue"]], [("Flywire", "apply_now")])
        self.assertEqual(sel["queue"][0]["shown_label"], "shown since 2024-10-26 · 3 day(s)")

    def test_render_without_checker_reuses_the_remembered_verdict_with_its_date(self):
        cands = self.cands(1)
        key = self.m.seen_key(cands[0])
        seen = {key: {"first_seen": "2024-10-27", "checked_on": "2024-10-28", "verdict": "apply_now",
                      "reasons": [], "facts": {"location": "Durham, NC"}}}
        sel = self.m.select_queue(cands, seen, TODAY, checker=None)
        self.assertEqual(sel["queue"][0]["tier"], "apply_now")
        self.assertIn("checked 2024-10-28", sel["queue"][0]["check_label"])
        self.assertEqual(seen[key]["checked_on"], "2024-10-28")  # not rewritten: a later checker run must still re-check it

    def test_memory_keeps_the_link_so_a_role_can_resurface(self):
        cands = self.cands(1)
        seen = {}
        self.m.select_queue(cands, seen, TODAY)
        entry = seen[self.m.seen_key(cands[0])]
        self.assertEqual(entry["job_url"], cands[0]["job_url"])
        self.assertEqual(entry["location"], cands[0]["location"])

    def test_shown_days_label(self):
        cands = self.cands(1)
        seen = {self.m.seen_key(cands[0]): {"first_seen": "2024-10-26"}}
        sel = self.m.select_queue(cands, seen, TODAY)
        self.assertEqual(sel["queue"][0]["shown_label"], "shown since 2024-10-26 · 3 day(s)")

    def test_dead_listing_is_dropped_refilled_and_remembered(self):
        cands = self.cands(16)
        dead_url = cands[0]["job_url"]
        calls = []

        def checker(cand):
            calls.append(cand["job_url"])
            return "dead" if cand["job_url"] == dead_url else "live"

        seen = {}
        sel = self.m.select_queue(cands, seen, TODAY, limit=15, checker=checker)
        companies = [c["company"] for c in sel["queue"]]
        self.assertEqual((sel["dead"], len(companies)), (1, 15))
        self.assertNotIn("Co0", companies)
        self.assertIn("Co15", companies)  # the slot is refilled from the pool
        self.assertEqual(seen[self.m.seen_key(cands[0])]["dead_on"], "2024-10-29")
        self.assertEqual(sel["queue"][0]["check_label"], "listing checked live today")

        # A same-day re-render without a checker reuses today's verdicts.
        sel2 = self.m.select_queue(cands, seen, TODAY, limit=15, checker=None)
        self.assertEqual(len(sel2["queue"]), 15)
        self.assertNotIn("Co0", [c["company"] for c in sel2["queue"]])
        self.assertEqual(sel2["queue"][0]["check_label"], "listing checked live today")

        # A remembered-dead listing is never fetched again.
        calls.clear()
        self.m.select_queue(cands, seen, TODAY + dt.timedelta(days=1), limit=15, checker=checker)
        self.assertNotIn(dead_url, calls)

    def test_indeterminate_is_kept_and_labelled(self):
        sel = self.m.select_queue(self.cands(1), {}, TODAY, checker=lambda c: "indeterminate")
        self.assertEqual(sel["queue"][0]["check_label"], "listing could not be auto-checked")

    def test_a_check_still_running_at_the_time_budget_is_left_for_the_next_run_not_lost(self):
        # 2024-12-18: the queue-check step hit its 10-minute cap on a slow Bright Data night and lost every verdict.
        import threading
        import time
        first = threading.Event()

        def checker(cand):
            if not first.is_set():
                first.set()
                return "live"
            time.sleep(1.5)
            return "live"
        seen = {}
        sel = self.m.select_queue(self.cands(2), seen, TODAY, checker=checker, budget_s=0.3)
        self.assertEqual((len(sel["queue"]), sel["checked"], sel["unfinished"]), (2, 1, 1))
        checked_today = [e for e in seen.values() if e.get("checked_on") == TODAY.isoformat()]
        self.assertEqual(len(checked_today), 1)  # the finished check is kept; the other row is tried again next run
        self.assertEqual(sorted(q["check_label"].startswith("listing not auto-checked") for q in sel["queue"]), [False, True])

    def test_checker_exception_never_breaks_the_queue(self):
        def boom(cand):
            raise RuntimeError("network down")
        sel = self.m.select_queue(self.cands(2), {}, TODAY, checker=boom)
        self.assertEqual(len(sel["queue"]), 2)

    def test_prune_forgets_entries_not_seen_for_thirty_days(self):
        seen = {"a": {"last_seen": "2024-08-28"}, "b": {"last_seen": "2024-10-28"}}
        self.assertEqual(set(self.m.prune_seen(seen, TODAY)), {"b"})


class BannerTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()

    def status(self, **over):
        base = {
            "run_date": "2024-10-29", "started": "2024-10-29T06:15:05.1109135-05:00",
            "updated": "2024-10-29T06:22:17.5530941-05:00", "finished": "2024-10-29T06:22:17.5530941-05:00",
            "steps": [{"name": "pull", "exit": 0}, {"name": "render", "exit": 0}], "alert": {"status": "none"},
        }
        base.update(over)
        return base

    def test_no_status_file_is_a_warning(self):
        self.assertEqual(self.m.pipeline_banner(None, NOW)["state"], "warn")

    def test_clean_finished_run_is_ok(self):
        banner = self.m.pipeline_banner(self.status(), NOW)
        self.assertEqual(banner["state"], "ok")
        self.assertIn("2 steps clean", banner["headline"])

    def test_failed_step_is_bad_and_the_alert_headline_is_shown(self):
        # 2024-12-19: the banner renders the chain's own alert.headline (it only ever read alert.status before,
        # and still printed a GMAIL message for an email that was retired on 2024-12-17).
        status = self.status(
            steps=[{"name": "apply-check", "exit": 1, "error": "1 curated Apply role(s) flagged closed: Example Staffing"}],
            alert={"status": "status-file", "level": "failed",
                   "headline": "Night run 2024-10-29: 1 step(s) failed (apply-check)."},
        )
        banner = self.m.pipeline_banner(status, NOW)
        self.assertEqual(banner["state"], "bad")
        self.assertTrue(any("apply-check: exit 1" in d and "Example Staffing" in d for d in banner["details"]))
        self.assertIn("Chain: Night run 2024-10-29: 1 step(s) failed (apply-check).", banner["details"])
        self.assertFalse(any("GMAIL" in d or "Alert email" in d for d in banner["details"]))

    def test_a_legacy_email_alert_record_adds_nothing(self):
        banner = self.m.pipeline_banner(self.status(alert={"status": "failed", "detail": "5.7.0 Authentication Required"}), NOW)
        self.assertEqual(banner["state"], "ok")
        self.assertFalse(any("Alert email" in d for d in banner["details"]))

    def test_overdue_run_is_bad(self):
        banner = self.m.pipeline_banner(self.status(), NOW + dt.timedelta(days=3))
        self.assertEqual(banner["state"], "bad")
        self.assertIn("No pipeline run since", banner["headline"])

    def test_in_progress_run_is_warn(self):
        self.assertEqual(self.m.pipeline_banner(self.status(finished=None), NOW)["state"], "warn")

    def test_queue_stats_line(self):
        banner = self.m.pipeline_banner(self.status(), NOW, {"shown": 15, "dead": 2, "expired": 1, "checked": 15})
        self.assertIn("Queue: 15 shown · 2 dead listing(s) dropped · 1 expired after 7 days · 15 auto-checked",
                      banner["details"])


class BuildIntegrationTests(unittest.TestCase):
    def test_build_reads_status_and_liveness_and_writes_queue_memory(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for sub in ("dashboard", "weekly-sweeps", "automation-logs"):
                (root / sub).mkdir()
            url = "https://www.linkedin.com/jobs/view/9"
            data = {"last_validated": "2024-10-16", "apply": [
                {"company": "LiveCo", "title": "IT Support Specialist", "verified": "Live",
                 "verified_on": "2024-10-27", "apply_url": url}], "skip": []}
            (root / "dashboard" / "job-dashboard-data.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "dashboard" / "apply-liveness.json").write_text(
                json.dumps({url: {"checked_on": "2024-10-29", "verdict": "live"}}), encoding="utf-8")
            (root / "automation-logs" / "pipeline-status.json").write_text(json.dumps({
                "updated": "2024-10-29T06:22:17-05:00", "finished": "2024-10-29T06:22:17-05:00",
                "steps": [{"name": "pull", "exit": 1, "error": "exit 1"}],
                "alert": {"status": "status-file", "level": "failed", "headline": "Night run 2024-10-29: 1 step(s) failed (pull)."},
            }), encoding="utf-8")
            with (root / "weekly-sweeps" / "linkedin-raw-2024-10-29.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(raw_row("x", "y")))
                writer.writeheader()
                writer.writerow(raw_row("NewCo", "Desktop Support Engineer", "2024-10-29"))
                writer.writerow(raw_row("OldCo", "IT Support Specialist", "2024-10-26"))

            result = m.build_dashboard(root, check_links=False,
                                       queue_checker=lambda c: "live", today=TODAY, now=NOW)
            html = (root / "job-dashboard.html").read_text(encoding="utf-8")
            md = (root / "job-dashboard.md").read_text(encoding="utf-8")
            self.assertEqual(result["pipeline_state"], "bad")
            self.assertEqual(result["queue_checked"], 2)
            self.assertIn("Chain: Night run 2024-10-29: 1 step(s) failed (pull).", html)
            self.assertIn("Pipeline (BAD)", md)
            self.assertIn("auto-check 2024-10-29: listing live on linkedin.com", html)
            self.assertLess(html.index("NewCo"), html.index("OldCo"))  # newest first
            seen = json.loads((root / "dashboard" / "queue-seen.json").read_text(encoding="utf-8"))
            self.assertEqual({v["first_seen"] for v in seen.values()}, {"2024-10-29"})


if __name__ == "__main__":
    unittest.main()


class NoRunTests(unittest.TestCase):
    """2024-12-17: the laptop slept through the 00:30 task; the status file was from the day before and
    the 36-hour rule kept the banner green all day. A status file from another date now reads as a
    missed night once the morning grace hour has passed."""

    def setUp(self):
        self.m = load_module()
        self.status = {"run_date": "2024-10-29", "started": "2024-10-29T00:30:05-05:00", "updated": "2024-10-29T01:24:12-05:00",
                       "finished": "2024-10-29T01:24:12-05:00", "steps": [{"name": "pull", "exit": 0}], "alert": {"status": "status-file"}}

    def test_a_status_file_from_yesterday_is_a_missed_night_after_the_grace_hour(self):
        banner = self.m.pipeline_banner(self.status, NOW + dt.timedelta(days=1, hours=1))  # 09:00 the next day
        self.assertEqual(banner["state"], "bad")
        self.assertIn("nothing recorded for 2024-10-30", banner["headline"])
        self.assertIn("catch-up task", banner["headline"])

    def test_before_the_grace_hour_the_previous_night_still_counts(self):
        early = dt.datetime(2024, 10, 30, 0, 10, tzinfo=NOW.tzinfo)  # the next night's task has not fired yet
        self.assertEqual(self.m.pipeline_banner(self.status, early)["state"], "ok")

    def test_todays_run_is_not_a_missed_night(self):
        self.assertEqual(self.m.pipeline_banner(self.status, NOW)["state"], "ok")
