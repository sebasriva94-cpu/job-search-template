"""Tests for the listing validator: parsing and decisions run without network."""
import datetime as dt
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.fakes import temp_gate

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Listing dates relative to today: the validator skips listings 3 weeks old or more (2024-12-09).
RECENT = (dt.date.today() - dt.timedelta(days=2)).isoformat()
spec = importlib.util.spec_from_file_location("validate_listing", ROOT / "scripts" / "validate_listing.py")
vl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vl)


def linkedin_page(title, body, salary=None, remote=False, closed=False, applicants="59 applicants"):
    ld = {"@type": "JobPosting", "title": title, "datePosted": RECENT,
          "jobLocation": {"address": {"addressLocality": "Raleigh", "addressRegion": "NC"}},
          "description": body}
    if remote:
        ld["jobLocationType"] = "TELECOMMUTE"
    if salary:
        ld["baseSalary"] = {"value": {"minValue": salary[0], "maxValue": salary[1], "unitText": salary[2]}}
    banner = "No longer accepting applications" if closed else ""
    return (f"<html><head><script type=\"application/ld+json\">{json.dumps(ld)}</script></head>"
            f"<body><h1>{title}</h1> 1 day ago {applicants} {banner} <p>{body}</p></body></html>")


class ExtractTests(unittest.TestCase):
    def listing(self, html):
        return {"source": "unlocker", "closed": False, "html": html, "text": vl._strip_html(html),
                "title": "", "location": "", "posted": "", "status": 200, "error": None}

    def test_json_ld_salary_hourly_becomes_annual_and_remote_flag(self):
        page = linkedin_page("IAM Access Ops Analyst", "Manage the identity lifecycle.", salary=(34.62, 42.0, "HOUR"), remote=True)
        facts = vl.extract_facts(self.listing(page))
        self.assertTrue(facts["remote"])
        self.assertEqual(facts["pay_min"], round(34.62 * 2080))
        self.assertEqual(facts["pay_max"], round(42.0 * 2080))
        self.assertEqual(facts["posted"], RECENT)
        self.assertEqual(facts["applicants"], 59)

    def test_text_pay_range_fallback(self):
        page = "<html><body>Base pay range $38.46/hr - $43.26/hr. Executive support role, 100% onsite in Denver.</body></html>"
        facts = vl.extract_facts(self.listing(page), {"location": "Denver, CO"})
        self.assertEqual(facts["pay_min"], round(38.46 * 2080))
        self.assertEqual(facts["pay_max"], round(43.26 * 2080))
        self.assertTrue(facts["onsite"])
        self.assertFalse(facts["remote"])

    def test_annual_range_with_dash_variants(self):
        page = "<html><body>Base Salary Range: $69,000 — $115,000 USD plus equity.</body></html>"
        facts = vl.extract_facts(self.listing(page))
        self.assertEqual((facts["pay_min"], facts["pay_max"]), (69000, 115000))

    def test_closed_marker_years_and_clearance(self):
        page = linkedin_page("Systems Administrator", "Requires an active Secret clearance and 10+ years of experience.", closed=True)
        facts = vl.extract_facts(self.listing(page))
        self.assertTrue(facts["closed"])
        self.assertTrue(facts["clearance"])
        self.assertEqual(facts["years"], 10)

    def test_years_only_count_as_a_requirement_not_marketing_copy(self):
        # FloQast, 2024-11-03: auto-skipped as "asks for 9+ years" from "since 2017 (that's 9 years!)".
        body = ("2+ years of experience in Technical Support, IT, or a related technical field. "
                "Best Places to Work by LA Business Journal since 2017 (that's 9 years!) "
                "Inc. Magazine's Best Workplaces in 2025, 2024, 2023, 2022, and 2021.")
        facts = vl.extract_facts(self.listing(linkedin_page("Technical Support Engineer (Integrations)", body)))
        self.assertEqual(facts["years"], 2)
        # Other requirement phrasings still count.
        for body, expected in (("3+ years' experience in IT support", 3), ("More than 2 years of experience working in technical support", 2),
                               ("3-5 years of hands-on relevant experience", 5), ("3+ years in a technical support role", 3)):
            facts = vl.extract_facts(self.listing(linkedin_page("IT Support Engineer", body)))
            self.assertEqual(facts["years"], expected, body)
        # A bare number of years with no requirement wording is ignored.
        facts = vl.extract_facts(self.listing(linkedin_page("IT Support Engineer", "Founded 12 years ago; unlimited PTO.")))
        self.assertIsNone(facts["years"])


class DecideTests(unittest.TestCase):
    def facts(self, **over):
        base = {"closed": False, "pay_min": None, "pay_max": None, "remote": False, "onsite": False,
                "location": "Raleigh, NC", "applicants": 40, "years": 5, "clearance": False, "title": "IT Systems Administrator"}
        base.update(over)
        return base

    def test_open_home_region_pay_ok_is_apply_now(self):
        v = vl.decide({"location": "Raleigh, NC"}, self.facts(pay_max=115000))
        self.assertEqual(v["verdict"], "apply_now")

    def test_level_band_is_middle_only(self):
        # The owner's choice on 2024-12-09: Middle (II / Tier 2) only; III / Tier 3 and up are out like senior titles.
        for title in ("IT Support Technician III", "IT Support Technician Level 3", "Tier 3 Support Engineer",
                      "Desktop Support Engineer 3", "IT Support Specialist (Level 3)", "Systems Administrator IV"):
            v = vl.decide({}, self.facts(title=title))
            self.assertEqual(v["verdict"], "skip", title)
            self.assertIn("above the level II band", v["reasons"][0])
        for title in ("IT Support Engineer II", "Help Desk Analyst Tier 2", "IT Support Specialist",
                      "IT Support Specialist (Level II to III)", "IT Specialist - L3Harris"):
            self.assertEqual(vl.decide({}, self.facts(title=title))["verdict"], "apply_now", title)

    def test_listings_three_weeks_old_or_more_are_skipped(self):
        # The owner's rule since 2024-12-09: only listings posted less than 3 weeks ago.
        today = dt.date(2024, 12, 9)
        self.assertEqual(vl.posting_age_days("2024-11-18", today), 21)
        self.assertEqual(vl.posting_age_days("Reposted 3 weeks ago"), 21)
        self.assertEqual(vl.posting_age_days("a month ago"), 30)
        self.assertIsNone(vl.posting_age_days(""))
        old = (dt.date.today() - dt.timedelta(days=22)).isoformat()
        v = vl.decide({}, self.facts(posted=old))
        self.assertEqual(v["verdict"], "skip")
        self.assertIn("3 weeks or more", v["reasons"][0])
        self.assertEqual(vl.decide({}, self.facts(posted="2 weeks ago"))["verdict"], "apply_now")

    def test_austin_onsite_is_now_outside_the_target_area(self):
        # Denver is outside the example target areas.
        v = vl.decide({"location": "Denver, CO"}, self.facts(location="Denver, CO", onsite=True))
        self.assertEqual(v["verdict"], "skip")
        self.assertIn("onsite outside Raleigh-Durham", v["reasons"][0])

    def test_second_metro_onsite_is_in_the_target_area_and_needs_no_home_state_branch(self):
        # Greater Minneapolis is in the target area, any office pattern.
        v = vl.decide({"location": "Minneapolis, MN"}, self.facts(location="Minneapolis, MN", onsite=True))
        self.assertEqual(v["verdict"], "apply_now")
        self.assertTrue(vl.physical_in_area({"location": "Edina, MN"}, {"location": "Edina, MN", "onsite": True}))
        v = vl.decide({"location": "Rochester, MN"}, self.facts(location="Rochester, MN", onsite=True))
        self.assertEqual(v["verdict"], "skip")

    def test_unknown_pay_is_still_apply_now_with_a_note(self):
        v = vl.decide({"location": "Durham, NC"}, self.facts())
        self.assertEqual(v["verdict"], "apply_now")
        self.assertIn("pay not published", v["reasons"])

    def test_closed_is_dead_and_clearance_is_skip(self):
        self.assertEqual(vl.decide({}, self.facts(closed=True))["verdict"], "dead")
        self.assertEqual(vl.decide({}, self.facts(clearance=True))["verdict"], "skip")

    def test_low_pay_skip_and_near_floor_review(self):
        # Floor $70k since 2024-12-09: under $65k is a skip, $65k to $70k is shown but flagged.
        self.assertEqual(vl.decide({}, self.facts(pay_max=62400))["verdict"], "skip")
        near = vl.decide({}, self.facts(pay_max=68640))
        self.assertEqual(near["verdict"], "review")
        self.assertTrue(any("under the $70,000 floor" in r for r in near["reasons"]))
        self.assertEqual(vl.decide({}, self.facts(pay_max=82000))["verdict"], "apply_now")

    def test_onsite_elsewhere_is_skip_and_remote_elsewhere_is_fine(self):
        self.assertEqual(vl.decide({"location": "Charlotte, NC"}, self.facts(location="Charlotte, NC", onsite=True))["verdict"], "skip")
        self.assertEqual(vl.decide({"location": "Remote, US"}, self.facts(location="Draper, UT", remote=True))["verdict"], "apply_now")

    def test_senior_title_and_years_are_skips(self):
        self.assertEqual(vl.decide({}, self.facts(title="Senior Systems Engineer"))["verdict"], "skip")
        self.assertEqual(vl.decide({}, self.facts(years=8))["verdict"], "skip")

    def test_junior_titles_are_skips_but_plain_titles_pass(self):
        for title in ("IT Help Desk Technician L1", "IT Support Specialist I", "IT Operations Specialist (New Grad)",
                      "Junior Systems Administrator", "Technical Support Tech I"):
            self.assertEqual(vl.decide({}, self.facts(title=title))["verdict"], "skip", title)
        for title in ("IT Systems Administrator", "IT Support Engineer", "IAM Access Ops Analyst", "IT Support Technician II"):
            self.assertEqual(vl.decide({}, self.facts(title=title))["verdict"], "apply_now", title)


class ValidateTests(unittest.TestCase):
    def test_validate_uses_injected_fetcher_and_reports_facts(self):
        page = linkedin_page("IT Operations Specialist", "3-5 years of relevant experience. Onsite at our Raleigh, NC office.", applicants="35 applicants")
        fetcher = lambda url: {"source": "unlocker", "closed": False, "html": page, "text": vl._strip_html(page),
                               "title": "", "location": "", "posted": "", "status": 200, "error": None}
        out = vl.validate({"job_url": "https://www.linkedin.com/jobs/view/1", "title": "IT Operations Specialist", "location": "Raleigh, NC"}, fetcher=fetcher)
        self.assertEqual(out["verdict"], "apply_now")
        self.assertEqual(out["facts"]["applicants"], 35)
        self.assertEqual(out["facts"]["years"], 5)

    def test_home_state_presence_rule_counts_a_branch_not_just_headquarters(self):
        # The owner's rule, widened 2024-12-23: any employer with brick-and-mortar in North Carolina qualifies (an out-of-state
        # headquarters with a local branch). A role physically in Raleigh-Durham is itself the branch
        # evidence, so it never triggers the headquarters check and no lookup is spent on it. Only a remote
        # role still asks where the employer sits, and a known out-of-state HQ is no longer an auto-skip
        # (headquarters alone cannot disprove a North Carolina branch); it is the owner's call, shown under review.
        page = linkedin_page("IT Support Specialist", "2-4 years of experience. Onsite at our Durham, NC office.")
        fetcher = lambda url: {"source": "unlocker", "closed": False, "html": page, "text": vl._strip_html(page),
                               "title": "", "location": "", "posted": "", "status": 200, "error": None}
        row = {"job_url": "https://x", "title": "IT Support Specialist", "company": "Some Co", "location": "Durham, NC"}
        calls = []
        out = vl.validate(row, fetcher=fetcher, hq_lookup=lambda c: calls.append(c) or "IL")
        self.assertEqual(out["verdict"], "apply_now")  # Chicago HQ, Durham office: in
        self.assertEqual(calls, [])                    # no paid lookup wasted on a physical role

        remote_page = linkedin_page("IT Support Specialist", "2-4 years of experience. This role is fully remote.", remote=True)
        remote_fetcher = lambda url: {"source": "unlocker", "closed": False, "html": remote_page,
                                      "text": vl._strip_html(remote_page), "title": "", "location": "", "posted": "",
                                      "status": 200, "error": None}
        remote_row = dict(row, location="Remote, US")
        out = vl.validate(remote_row, fetcher=remote_fetcher, hq_lookup=lambda c: "IL")
        self.assertEqual(out["verdict"], "review")
        self.assertTrue(any("North Carolina branch" in r for r in out["reasons"]))
        self.assertEqual(vl.validate(remote_row, fetcher=remote_fetcher, hq_lookup=lambda c: "NC")["verdict"], "apply_now")
        unknown = vl.validate(remote_row, fetcher=remote_fetcher, hq_lookup=lambda c: None)
        self.assertEqual(unknown["verdict"], "review")
        self.assertIn("North Carolina headquarters not confirmed", unknown["reasons"])
        # No lookup is spent on a row that is already a skip, and callers without hq_lookup are unchanged.
        calls = []
        senior_page = linkedin_page("Senior IT Support Specialist", "8+ years of experience. Onsite at our Durham, NC office.")
        senior_fetcher = lambda url: {"source": "unlocker", "closed": False, "html": senior_page,
                                      "text": vl._strip_html(senior_page), "title": "", "location": "", "posted": "",
                                      "status": 200, "error": None}
        senior = dict(row, title="Senior IT Support Specialist")
        self.assertEqual(vl.validate(senior, fetcher=senior_fetcher, hq_lookup=lambda c: calls.append(c) or "NC")["verdict"], "skip")
        self.assertEqual(calls, [])
        self.assertEqual(vl.validate(row, fetcher=fetcher)["verdict"], "apply_now")

    def test_fetch_failure_is_indeterminate_unless_the_title_rules_it_out(self):
        fetcher = lambda url: {"source": "direct", "closed": False, "html": "", "text": "", "title": "", "location": "", "posted": "", "status": None, "error": "TimeoutError"}
        out = vl.validate({"job_url": "https://x", "title": "IT Support", "location": "Durham, NC"}, fetcher=fetcher)
        self.assertEqual(out["verdict"], "indeterminate")
        senior = vl.validate({"job_url": "https://x", "title": "Senior AI infrastructure engineer", "location": "Durham, NC"}, fetcher=fetcher)
        self.assertEqual(senior["verdict"], "skip")
        manager = vl.validate({"job_url": "https://x", "title": "Engineering Manager, IAM", "location": "Durham, NC"}, fetcher=fetcher)
        self.assertEqual(manager["verdict"], "skip")

    def test_greenhouse_url_is_routed_to_the_api(self):
        seen = {}

        def fake_api(url, timeout=25):
            seen["url"] = url
            return {"title": "IT Systems Administrator", "location": {"name": "Denver, Colorado"}, "updated_at": "2024-10-09T00:00:00",
                    "content": "&lt;p&gt;Base Salary Range: $69,000 — $115,000 USD&lt;/p&gt;"}

        original = vl._api_json
        vl._api_json = fake_api
        try:
            listing = vl.fetch_listing("https://boards.greenhouse.io/acmerobotics/jobs/1234567890?gh_jid=1234567890")
        finally:
            vl._api_json = original
        self.assertIn("boards-api.greenhouse.io/v1/boards/acmerobotics/jobs/1234567890", seen["url"])
        self.assertEqual(listing["source"], "greenhouse")
        facts = vl.extract_facts(listing)
        self.assertEqual(facts["pay_max"], 115000)

class UnreadablePageTests(unittest.TestCase):
    """2024-12-09: refusals (401/429), sign-in pages and "page not found" were read as open listings
    and shown as "Apply now". A gone page is closed; anything unread is "could not check"."""

    ROW = {"job_url": "https://www.linkedin.com/jobs/view/123", "title": "IT Support Specialist",
           "company": "Some Co", "location": "Durham, NC"}

    def run_with(self, status, html="", final_url=None, row=None):
        page = {"status": status, "final_url": final_url or self.ROW["job_url"], "html": html, "error": None}
        with mock.patch.dict(os.environ), mock.patch.object(vl, "ats_fetch", return_value=None), \
                mock.patch.object(vl, "_http_get", return_value=page), mock.patch.object(vl, "RETRY_PAUSE", 0):
            os.environ.pop("BRIGHTDATA_API_TOKEN", None)
            return vl.validate(row or self.ROW)

    def test_page_not_found_is_dead(self):
        self.assertEqual(self.run_with(404)["verdict"], "dead")
        self.assertEqual(self.run_with(410)["verdict"], "dead")

    def test_refusals_are_could_not_check_never_apply_now(self):
        for status in (401, 403, 429):
            out = self.run_with(status, html="<html><body>Too Many Requests</body></html>")
            self.assertEqual(out["verdict"], "indeterminate", status)
            self.assertIn(f"blocked (HTTP {status})", out["reasons"][0])
        for status in (500, 503):  # the site's own server failed; it was labeled "blocked" until 2024-12-09
            out = self.run_with(status, html="<html><body>Service Unavailable</body></html>")
            self.assertEqual(out["verdict"], "indeterminate", status)
            self.assertIn(f"server error (HTTP {status})", out["reasons"][0])

    def test_linkedin_sign_in_page_is_could_not_check(self):
        wall = "<html><body>Sign in to view more jobs. Join now. Email or phone. Password.</body></html>"
        self.assertEqual(self.run_with(200, html=wall)["verdict"], "indeterminate")
        self.assertEqual(self.run_with(200, html="<html>x</html>", final_url="https://www.linkedin.com/authwall?trk=x")["verdict"],
                         "indeterminate")

    def test_linkedin_red_banner_is_dead_and_a_real_page_still_reads(self):
        closed = ("<html><body>IT Support Specialist. No longer accepting applications. Seniority level Mid-Senior "
                  "level. Employment type Full-time. Onsite at our Durham, NC office.</body></html>")
        self.assertEqual(self.run_with(200, html=closed)["verdict"], "dead")
        open_page = ("<html><body>IT Support Specialist. 2-4 years of experience. Seniority level Associate. "
                     "Employment type Full-time. Onsite at our Durham, NC office.</body></html>")
        self.assertEqual(self.run_with(200, html=open_page)["verdict"], "apply_now")

    def test_a_title_still_rules_a_blocked_row_out(self):
        senior = dict(self.ROW, title="Senior Systems Engineer")
        self.assertEqual(self.run_with(429, row=senior)["verdict"], "skip")

    def test_a_blocked_remote_row_is_could_not_check_not_onsite(self):
        # Live check 2024-12-09: "Remote Support Engineer II" at "Remote, US" was called onsite elsewhere.
        remote = dict(self.ROW, title="Remote Support Engineer II", location="Remote, US")
        self.assertEqual(self.run_with(401, row=remote)["verdict"], "indeterminate")
        austin = dict(self.ROW, location="Denver, CO")
        self.assertEqual(self.run_with(401, row=austin)["verdict"], "skip")


class LocationFromBodyTests(unittest.TestCase):
    """2024-10-29: Cloudflare's Greenhouse location field said only "Hybrid"; the body said
    "Available Locations: Hybrid - Denver, CO" and the checker skipped a live Denver role.
    The same body cue reads a Raleigh-Durham city."""

    def test_hybrid_ats_location_reads_home_region_from_the_posting_body(self):
        text = ("About Us. Available Locations: Hybrid - Rougemont, NC Schedule: This is a full-time, Tuesday-Saturday "
                "role. This role requires you to be able to come into our Rougemont office twice a week. 3-5 years of experience.")
        listing = {"source": "greenhouse", "closed": False, "text": text, "html": "", "location": "Hybrid",
                   "title": "Technical Support Engineer, Application Performance", "posted": RECENT}
        row = {"title": listing["title"], "company": "Cloudflare", "location": ""}
        facts = vl.extract_facts(listing, row)
        self.assertIn("Rougemont, NC", facts["location"])
        verdict = vl.decide(row, facts)
        self.assertNotEqual(verdict["verdict"], "skip", verdict)

    def test_hybrid_in_another_city_is_still_a_skip(self):
        listing = {"source": "greenhouse", "closed": False, "html": "", "location": "Hybrid", "posted": "",
                   "text": "Available Locations: Hybrid - Denver, CO. Come into our Denver office twice a week.",
                   "title": "Technical Support Engineer"}
        row = {"title": "Technical Support Engineer", "company": "X", "location": ""}
        verdict = vl.decide(row, vl.extract_facts(listing, row))
        self.assertEqual(verdict["verdict"], "skip", verdict)


class AgencyHourlyFlagTests(unittest.TestCase):
    """The owner's rule (2024-12-09): staffing agencies are shown like any employer, flagged only when the pay is hourly."""

    def fetcher(self, page):
        return lambda url: {"source": "unlocker", "closed": False, "html": page, "text": vl._strip_html(page),
                            "title": "", "location": "", "posted": "", "status": 200, "error": None}

    def row(self, company, **over):
        return {"job_url": "https://x", "title": "IT Support Specialist", "company": company, "location": "Durham, NC", **over}

    def test_hourly_agency_listing_is_flagged_and_still_shown(self):
        page = linkedin_page("IT Support Specialist", "2-4 years of experience. Onsite at our Durham, NC office.",
                             salary=(35, 45, "HOUR"))
        out = vl.validate(self.row("Kforce Inc"), fetcher=self.fetcher(page))
        self.assertEqual(out["verdict"], "apply_now")
        self.assertIn(vl.AGENCY_HOURLY, out["reasons"])
        self.assertTrue(out["facts"]["pay_hourly"])

    def test_salaried_agency_and_hourly_employer_are_not_flagged(self):
        yearly = linkedin_page("IT Support Specialist", "Onsite at our Durham, NC office.", salary=(70000, 85000, "YEAR"))
        hourly = linkedin_page("IT Support Specialist", "Onsite at our Durham, NC office.", salary=(35, 45, "HOUR"))
        self.assertNotIn(vl.AGENCY_HOURLY, vl.validate(self.row("Robert Half"), fetcher=self.fetcher(yearly))["reasons"])
        self.assertNotIn(vl.AGENCY_HOURLY, vl.validate(self.row("UNC Chapel Hill"), fetcher=self.fetcher(hourly))["reasons"])

    def test_the_feed_interval_flags_an_agency_listing_that_could_not_be_read(self):
        blocked = lambda url: {"source": "direct", "closed": False, "html": "", "text": "", "title": "", "location": "",
                               "posted": "", "status": 429, "error": "blocked (HTTP 429)"}
        out = vl.validate(self.row("TEKsystems", pay_interval="hourly"), fetcher=blocked)
        self.assertEqual(out["verdict"], "indeterminate")
        self.assertIn(vl.AGENCY_HOURLY, out["reasons"])
        salaried = vl.validate(self.row("TEKsystems", pay_interval="yearly"), fetcher=blocked)
        self.assertNotIn(vl.AGENCY_HOURLY, salaried["reasons"])

    def test_hourly_pay_read_from_page_text(self):
        listing = lambda text: {"source": "direct", "closed": False, "html": "", "text": text,
                                "title": "", "location": "", "posted": "", "status": 200, "error": None}
        self.assertTrue(vl.extract_facts(listing("Pay: $25.00 - $30.00 per hour, Monday to Friday."))["pay_hourly"])
        self.assertTrue(vl.extract_facts(listing("Compensation: $28/hr plus benefits."))["pay_hourly"])
        self.assertFalse(vl.extract_facts(listing("Salary $70,000 - $85,000 a year. $12/hour night differential."))["pay_hourly"])
        self.assertFalse(vl.extract_facts(listing("A $2.00/hour night differential applies."))["pay_hourly"])


class EmployerLinkTests(unittest.TestCase):
    """2024-12-09: Indeed pages time out through Bright Data, so an Indeed row is checked on the
    employer's own posting (the feed's job_url_direct), and only when that page shows the job."""

    ROW = {"job_url": "https://www.indeed.com/viewjob?jk=2ca5a337c958f64f", "title": "IT Support Specialist",
           "company": "Some Co", "location": "Durham, NC",
           "job_url_direct": "https://acme.wd1.myworkdayjobs.com/External/job/Durham/IT-Support-Specialist_R1"}
    # A Workday posting has no visible text, only its JobPosting block.
    WORKDAY = ('<html><head><script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": "IT Support Specialist", "datePosted": RECENT,
        "jobLocation": {"address": {"addressLocality": "Durham", "addressRegion": "NC"}},
        "description": "2-4 years of experience supporting Windows and Mac users."}) + "</script></head><body></body></html>")

    def page(self, html):
        return {"source": "unlocker", "closed": False, "html": html, "text": vl._strip_html(html),
                "title": "", "location": "", "posted": "", "status": 200, "error": None}

    def test_an_indeed_row_is_checked_on_the_employer_posting(self):
        fetched = []
        out = vl.validate(self.ROW, fetcher=lambda url: fetched.append(url) or self.page(self.WORKDAY))
        self.assertEqual(fetched, [self.ROW["job_url_direct"]])
        self.assertEqual(out["verdict"], "apply_now")
        self.assertEqual(out["facts"]["location"], "Durham, NC")
        self.assertEqual(out["facts"]["checked_url"], self.ROW["job_url_direct"])

    def test_other_rows_keep_their_own_link(self):
        rows = [dict(self.ROW, job_url_direct=""),  # no employer link
                dict(self.ROW, job_url_direct="http://www.indeed.com/job/it-support-specialist-2ca5a337c958f64f"),  # applied for on Indeed
                dict(self.ROW, job_url="https://www.linkedin.com/jobs/view/1")]  # not an Indeed row
        for row in rows:
            fetched = []
            out = vl.validate(row, fetcher=lambda url: fetched.append(url) or self.page(self.WORKDAY))
            self.assertEqual(fetched, [row["job_url"]])
            self.assertEqual(out["facts"]["checked_url"], row["job_url"])

    def test_an_employer_page_that_does_not_show_the_job_is_could_not_check(self):
        # Live 2024-12-09: BambooHR and ADP send an app shell that a browser fills in later.
        for shell in ("<html><body>BambooHR</body></html>",
                      "<html><body>Recruitment Please switch to a supported browser listed here.</body></html>"):
            out = vl.validate(self.ROW, fetcher=lambda url: self.page(shell))
            self.assertEqual(out["verdict"], "indeterminate", shell)
            self.assertIn("the employer's page did not show the job", out["reasons"][0])
        closed = vl.validate(self.ROW, fetcher=lambda url: self.page("<html><body>Sorry, this job is no longer available.</body></html>"))
        self.assertEqual(closed["verdict"], "dead")
        gone = vl.validate(self.ROW, fetcher=lambda url: dict(self.page(""), closed=True))  # fetch_listing's 404 / 410
        self.assertEqual(gone["verdict"], "dead")
        visible = "<html><body><h1>IT Support Specialist</h1> Onsite at our Durham, NC office.</body></html>"
        self.assertEqual(vl.validate(self.ROW, fetcher=lambda url: self.page(visible))["verdict"], "apply_now")
        self.assertTrue(vl.shows_listing({"source": "greenhouse", "text": ""}, "IT Support Specialist"))

    def test_a_bright_data_refusal_falls_back_to_a_plain_fetch(self):
        notice = ("Residential Failed (bad_endpoint): Requested site is not available for immediate residential "
                  "(no KYC) access mode in accordance with robots.txt.")

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return notice.encode()

        url = "https://acme.wd3.myworkdayjobs.com/External/job/Durham/IT-Support-Specialist_R1"
        plain = {"status": 200, "final_url": url, "html": self.WORKDAY, "error": None}
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "test"}), \
                mock.patch.object(vl.rate_control, "brightdata_gate", return_value=temp_gate(state_dir)), \
                mock.patch.object(vl.urllib.request, "urlopen", return_value=Answer()), \
                mock.patch.object(vl, "_http_get", return_value=plain) as get:
            self.assertEqual(vl.unlocker_get(url)["error"], "Bright Data declined the site")
            listing = vl.fetch_listing(url)
        get.assert_called_once()
        self.assertIsNone(listing["error"])
        self.assertTrue(vl.job_posting_ld(listing["html"]))


class KeyToolsTests(unittest.TestCase):
    """2024-12-09: the dashboard shows which of the owner's key tools each posting mentions (a signal, never a filter)."""

    TOOLS = ("Intune", "Jamf", "Okta", "Entra", "Active Directory", "ServiceNow", "PowerShell")

    def page(self, html):
        return {"source": "unlocker", "closed": False, "html": html, "text": vl._strip_html(html),
                "title": "", "location": "", "posted": "", "status": 200, "error": None}

    def test_reads_the_posting_not_the_similar_jobs_panel(self):
        html = linkedin_page("IT Support Specialist", "Manage devices in Intune and users in Azure AD; script with PowerShell.")
        html = html.replace("</body>", "<section>Similar jobs: Okta Administrator, Jamf Engineer</section></body>")
        self.assertEqual(vl.mentioned_skills(self.page(html), self.TOOLS), ["Intune", "Entra", "PowerShell"])

    def test_azure_active_directory_is_entra_and_whole_words_only(self):
        listing = lambda text: {"text": text, "html": ""}
        self.assertEqual(vl.mentioned_skills(listing("Azure Active Directory and ServiceNow."), self.TOOLS), ["Entra", "ServiceNow"])
        self.assertEqual(vl.mentioned_skills(listing("On-premises Active Directory, Group Policy."), self.TOOLS), ["Active Directory"])
        self.assertEqual(vl.mentioned_skills(listing("Jamfest volunteer; powershellx"), self.TOOLS), [])

    def test_validate_reports_the_tools_without_changing_the_verdict(self):
        html = linkedin_page("IT Support Specialist", "2-4 years of experience. Okta, Jamf. Onsite at our Durham, NC office.")
        row = {"job_url": "https://www.linkedin.com/jobs/view/9", "title": "IT Support Specialist", "location": "Durham, NC"}
        with_tools = vl.validate(row, fetcher=lambda url: self.page(html), skills=self.TOOLS)
        without = vl.validate(row, fetcher=lambda url: self.page(html))
        self.assertEqual((with_tools["facts"]["skills"], with_tools["facts"]["skills_of"]), (["Jamf", "Okta"], 7))
        self.assertEqual(with_tools["verdict"], without["verdict"])
        self.assertIsNone(without["facts"]["skills"])


class RetryTests(unittest.TestCase):
    """2024-12-09: one more try after a timeout, a DNS or connection failure, or a server error; a
    "page not found" is reported as such so the dashboard can confirm it on a second morning."""

    URL = "https://careers.example.com/job/1"
    PAGE = "<html><body><h1>IT Support Specialist</h1> Onsite at our Durham, NC office.</body></html>"

    def answer(self, status, html="", error=None):
        return {"status": status, "final_url": self.URL, "html": html, "error": error}

    def fetch(self, *answers):
        calls = []

        def fake_get(url, timeout=25):
            calls.append(url)
            return answers[min(len(calls), len(answers)) - 1]

        with mock.patch.dict(os.environ), mock.patch.object(vl, "_http_get", side_effect=fake_get), \
                mock.patch.object(vl, "RETRY_PAUSE", 0):
            os.environ.pop("BRIGHTDATA_API_TOKEN", None)
            return vl.fetch_listing(self.URL), len(calls)

    def test_a_hiccup_is_tried_once_more(self):
        for hiccup in (self.answer(None, error="TimeoutError: timed out"),
                       self.answer(None, error="URLError: <urlopen error [Errno 11001] getaddrinfo failed>"),
                       self.answer(503)):
            listing, calls = self.fetch(hiccup, self.answer(200, self.PAGE))
            self.assertEqual(calls, 2)
            self.assertIsNone(listing["error"])
            self.assertIn("IT Support Specialist", listing["text"])

    def test_only_one_retry_and_none_for_refusals_or_not_found(self):
        listing, calls = self.fetch(self.answer(None, error="TimeoutError: timed out"))
        self.assertEqual((calls, listing["error"]), (2, "TimeoutError: timed out"))
        for status in (401, 403, 404, 410, 429):
            self.assertEqual(self.fetch(self.answer(status))[1], 1, status)

    def test_page_not_found_is_marked_for_a_second_check(self):
        row = {"job_url": self.URL, "title": "IT Support Specialist", "location": "Durham, NC"}
        gone = lambda url: {"source": "direct", "closed": True, "html": "", "text": "", "title": "", "location": "",
                            "posted": "", "status": 404, "error": None}
        out = vl.validate(row, fetcher=gone)
        self.assertEqual((out["verdict"], out["facts"]["not_found"]), ("dead", True))
        banner = "<html><body>IT Support Specialist. No longer accepting applications.</body></html>"
        says_closed = lambda url: {"source": "direct", "closed": False, "html": banner, "text": vl._strip_html(banner),
                                   "title": "", "location": "", "posted": "", "status": 200, "error": None}
        closed = vl.validate(row, fetcher=says_closed)
        self.assertEqual((closed["verdict"], closed["facts"]["not_found"]), ("dead", False))


class ScraplingFallbackTests(unittest.TestCase):
    """2024-12-23: a Bright Data outage (network_error all night) left 87 LinkedIn queue rows unchecked and
    "Apply now" empty. When Bright Data is unavailable, a LinkedIn guest page is fetched directly through
    scrapling's HTTP tier (source "scrapling-direct"), capped and spaced under rate control. Indeed is never
    fetched directly (401 since 2024-12-09): its rows keep the employer-link path only."""

    ROW = {"job_url": "https://www.linkedin.com/jobs/view/123", "title": "IT Support Specialist",
           "company": "Some Co", "location": "Durham, NC"}
    OPEN = ("<html><body>IT Support Specialist. 2-4 years of experience. Seniority level Associate. "
            "Employment type Full-time. Onsite at our Durham, NC office.</body></html>")
    BD_DOWN = {"status": None, "final_url": ROW["job_url"], "html": "",
               "error": "Bright Data paused (network_error)", "bd_unavailable": True}

    def run_with(self, scrapling_page, row=None, bd_page=None):
        calls = []
        def fake_scrapling(url, timeout=30):
            calls.append(url)
            return scrapling_page
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "test"}), \
                mock.patch.object(vl, "ats_fetch", return_value=None), \
                mock.patch.object(vl, "controlled_unlocker_get", return_value=bd_page or self.BD_DOWN), \
                mock.patch.object(vl, "scrapling_get", side_effect=fake_scrapling), \
                mock.patch.object(vl.rate_control, "get_gate",
                                  return_value=temp_gate(state_dir, vl.rate_control.LINKEDIN_DIRECT)):
            out = vl.validate(row or self.ROW)
        return out, calls

    def page(self, status=200, html=""):
        return {"status": status, "final_url": self.ROW["job_url"], "html": html, "error": None}

    def test_a_guest_page_is_read_and_marked_scrapling_direct(self):
        out, calls = self.run_with(self.page(200, self.OPEN))
        self.assertEqual(calls, [self.ROW["job_url"]])
        self.assertEqual(out["verdict"], "apply_now")
        self.assertEqual(out["facts"]["source"], "scrapling-direct")

    def test_an_authwall_or_refusal_is_could_not_check_never_apply_now(self):
        wall = "<html><body>Sign in to view more jobs. Join now. Email or phone. Password.</body></html>"
        for page in (self.page(200, wall), self.page(429), self.page(403),
                     self.page(200, "<html>x</html>")):
            out, _ = self.run_with(page)
            self.assertEqual(out["verdict"], "indeterminate", page["status"])

    def test_indeed_is_never_fetched_directly(self):
        row = dict(self.ROW, job_url="https://www.indeed.com/viewjob?jk=abc123")
        out, calls = self.run_with(self.page(200, self.OPEN), row=row)
        self.assertEqual(calls, [])
        self.assertEqual(out["verdict"], "indeterminate")

    def test_a_bright_data_per_site_decline_is_not_routed_around(self):
        # "Bright Data declined the site" is a robots.txt answer for that site, not a provider outage;
        # going direct anyway would route around the site's own terms.
        declined = {"status": 200, "final_url": self.ROW["job_url"], "html": "",
                    "error": "Bright Data declined the site"}
        out, calls = self.run_with(self.page(200, self.OPEN), bd_page=declined)
        self.assertEqual(calls, [])
        self.assertEqual(out["verdict"], "indeterminate")

    def test_the_run_budget_stops_the_fallback_not_the_queue(self):
        bd_down = dict(self.BD_DOWN)
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "test"}), \
                mock.patch.object(vl, "ats_fetch", return_value=None), \
                mock.patch.object(vl, "controlled_unlocker_get", return_value=bd_down), \
                mock.patch.object(vl, "scrapling_get", return_value=self.page(200, self.OPEN)) as spy, \
                mock.patch.object(vl.rate_control, "get_gate",
                                  return_value=temp_gate(state_dir, vl.rate_control.LINKEDIN_DIRECT)):
            verdicts = [vl.validate(self.ROW)["verdict"] for _ in range(16)]
        self.assertEqual(spy.call_count, 15)  # the queue's own size: one fallback fetch per row, no more
        self.assertEqual(verdicts[:15], ["apply_now"] * 15)
        self.assertEqual(verdicts[15], "indeterminate")  # over budget: could not check, never guessed


if __name__ == "__main__":
    unittest.main()
