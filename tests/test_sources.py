import contextlib
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import hubspot_sweep, linkedin_pull, target_sweep, usajobs_sweep


class TargetSweepMappingTests(unittest.TestCase):
    def test_greenhouse_ashby_and_atlassian_mapping(self):
        gh = target_sweep.greenhouse_rows(
            "Acme", "acme", [{"title": "IT Support", "location": {"name": "Remote US"},
                                 "updated_at": "2024-09-11T10:00:00Z", "absolute_url": "https://g/1"}]
        )[0]
        ashby = target_sweep.ashby_rows(
            "Beta", "beta", [{"title": "Systems Administrator", "location": "Denver, CO",
                                "isRemote": False, "jobUrl": "https://a/1"}]
        )[0]
        atl = target_sweep.atlassian_rows(
            [{"title": "IT Operations Engineer", "locations": ["Denver", "Remote"],
              "applyUrl": "https://at/1"}]
        )[0]
        self.assertEqual((gh["site"], gh["date_posted"], gh["is_remote"]),
                         ("greenhouse:acme", "2024-09-11", "true"))
        self.assertEqual((ashby["site"], ashby["location"], ashby["is_remote"]),
                         ("ashby:beta", "Denver, CO", "false"))
        self.assertEqual((atl["site"], atl["location"], atl["is_remote"]),
                         ("atlassian", "Denver, Remote", "true"))


class USAJobsMappingTests(unittest.TestCase):
    def item(self, *, interval="PA", remote=False, location="Denver, Colorado", low="90000", high="110000"):
        return {"MatchedObjectDescriptor": {
            "PositionTitle": "IT Specialist (Customer Support)",
            "OrganizationName": "Agency",
            "PositionLocationDisplay": location,
            "PublicationStartDate": "2024-09-10T00:00:00",
            "PositionURI": "https://usa/1",
            "PositionRemuneration": [{"MinimumRange": low, "MaximumRange": high,
                                       "RateIntervalCode": interval}],
            "UserArea": {"Details": {"LowGrade": "9", "HighGrade": "12",
                                        "RemoteIndicator": remote, "TeleworkEligible": True}},
        }}

    def test_annual_salary_and_explicit_remote_only(self):
        annual = usajobs_sweep.item_to_row(self.item(remote=False))
        # USAJOBS RateIntervalCode is a code (PA, PH, ...); the words live in a sibling Description field (2024-12-20)
        hourly = usajobs_sweep.item_to_row(self.item(interval="PH", location="Remote", low="30", high="40"))
        telework = usajobs_sweep.item_to_row(self.item(remote=False, location="Washington, DC"))
        self.assertEqual((annual["min_amount"], annual["max_amount"]), ("90000", "110000"))
        # 2024-12-19: hourly federal pay is converted like every other lane and keeps its interval
        self.assertEqual((hourly["min_amount"], hourly["max_amount"], hourly["pay_interval"]), ("62400", "83200", "hourly"))
        self.assertEqual(annual["pay_interval"], "yearly")
        self.assertEqual(telework["is_remote"], "false")
        self.assertEqual(hourly["is_remote"], "true")

    def test_remote_section_excludes_onsite_out_of_state_keyword_matches(self):
        onsite_oklahoma = self.item(remote=False, location="Oklahoma City, Oklahoma")
        remote_us = self.item(remote=True, location="Remote, United States")
        rows = usajobs_sweep.remote_rows([onsite_oklahoma, remote_us])
        self.assertEqual([row["location"] for row in rows], ["Remote, United States"])

    def test_duty_station_searches_are_home_region_and_second_metro_not_austin(self):
        asked = []
        with mock.patch.object(usajobs_sweep, "get_key", return_value="k"), \
                mock.patch.object(usajobs_sweep, "search", side_effect=lambda key, **p: asked.append(p) or []), \
                contextlib.redirect_stdout(io.StringIO()):
            usajobs_sweep.main()
        self.assertEqual(asked[0], {"LocationName": "Raleigh, North Carolina", "Radius": "50"})
        self.assertEqual(asked[1], {"LocationName": "Minneapolis, Minnesota", "Radius": "30"})
        self.assertNotIn("Denver", str(asked))


class WatchlistTests(unittest.TestCase):
    """The employer watchlist: an example configuration, read from each employer's own public board."""

    def test_watchlist_entries_are_typed(self):
        for board in (target_sweep.GREENHOUSE, target_sweep.ASHBY, target_sweep.WORKDAY, target_sweep.ULTIPRO,
                      target_sweep.WORKABLE, target_sweep.BREEZY, target_sweep.SMARTRECRUITERS, target_sweep.ICIMS):
            self.assertIsInstance(board, dict)

    def test_an_empty_watchlist_is_not_a_failed_run(self):
        with mock.patch.object(target_sweep, "boards", return_value=[]):
            self.assertEqual(target_sweep.main(), 0)

    def test_workday_dates_and_links(self):
        today = __import__("datetime").date(2024, 12, 9)
        self.assertEqual(target_sweep.workday_posted("Posted Today", today), "2024-12-09")
        self.assertEqual(target_sweep.workday_posted("Posted Yesterday", today), "2024-12-08")
        self.assertEqual(target_sweep.workday_posted("Posted 11 Days Ago", today), "2024-11-28")
        self.assertEqual(target_sweep.workday_posted("Posted 30+ Days Ago", today), "2024-11-08")
        self.assertEqual(target_sweep.workday_posted("", today), "")
        rows = target_sweep.workday_rows("UNC Chapel Hill", "exampleuniversity.wd1.myworkdayjobs.com", "Careers", [
            {"title": "Desktop Support Technician.", "externalPath": "/job/Cary-NC/Desktop-Support-Technician_R1",
             "locationsText": "Cary, NC", "postedOn": "Posted 11 Days Ago"},
            {"title": "Registered Nurse", "externalPath": "/job/x", "locationsText": "Durham, NC", "postedOn": "Posted Today"},
        ], today)
        self.assertEqual(len(rows), 1)  # IT titles only
        self.assertEqual(rows[0]["job_url"], "https://exampleuniversity.wd1.myworkdayjobs.com/en-US/Careers/job/Cary-NC/Desktop-Support-Technician_R1")
        self.assertEqual((rows[0]["date_posted"], rows[0]["site"], rows[0]["is_remote"]), ("2024-11-28", "workday:exampleuniversity", "false"))

    def test_other_board_mappings(self):
        up = target_sweep.ultipro_rows("Example Telecom", "HOT1", "B1", [
            {"Id": "abc", "Title": "IT Analyst I", "PostedDate": "2024-12-09T20:42:53Z",
             "Locations": [{"Address": {"City": "Raleigh", "State": {"Code": "NC"}}}]}])[0]
        self.assertEqual((up["location"], up["date_posted"]), ("Raleigh, NC", "2024-12-09"))
        self.assertTrue(up["job_url"].endswith("/HOT1/JobBoard/B1/OpportunityDetail?opportunityId=abc"))
        wk = target_sweep.workable_rows("Example Health Village", "trv", [
            {"title": "Helpdesk Support Specialist", "url": "https://apply.workable.com/j/F4", "published_on": "2024-12-09",
             "city": "Morrisville", "state": "North Carolina", "telecommuting": False,
             "description": "<p>Fix PCs for 300 staff.</p>", "requirements": "<ul><li>CompTIA A+</li></ul>"}])[0]
        self.assertEqual((wk["location"], wk["job_url"], wk["site"]), ("Morrisville, North Carolina", "https://apply.workable.com/j/F4", "workable:trv"))
        self.assertIn("Fix PCs for 300 staff.", wk["description"])  # 2024-12-20: the widget's details carry the text
        self.assertIn("CompTIA A+", wk["description"])
        bz = target_sweep.breezy_rows("Example Benefits", "nb", [
            {"name": "Network Administrator", "url": "https://nb.breezy.hr/p/1", "published_date": "2024-12-05T14:17:18Z",
             "location": {"name": "Remote", "is_remote": True}, "description": "<p>Run the network.</p>"}])[0]
        self.assertEqual((bz["is_remote"], bz["date_posted"], bz["description"]), ("true", "2024-12-05", "Run the network."))
        sr = target_sweep.smartrecruiters_rows("Example Systems", "ExampleSystems", [
            {"id": "7440", "name": "IT Support Engineer", "releasedDate": "2024-08-20T16:40:51Z",
             "location": {"city": "Research Triangle Park", "region": "NC", "remote": False}}])[0]
        self.assertEqual((sr["location"], sr["job_url"]), ("Research Triangle Park, NC", "https://jobs.smartrecruiters.com/ExampleSystems/7440"))
        # 2024-12-20: one posting detail per IT title gives the text; the chef posting gets no request
        listing = {"content": [{"id": "7440", "name": "IT Support Engineer", "location": {"city": "Research Triangle Park", "region": "NC"}},
                               {"id": "7441", "name": "Sous Chef", "location": {"city": "Research Triangle Park", "region": "NC"}}]}
        detail = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Support hotel IT.</p>"}, "qualifications": {"text": "<p>3 years.</p>"}}}}
        with mock.patch.object(target_sweep, "get_json", side_effect=lambda url: detail if url.endswith("/postings/7440") else listing) as calls:
            postings = target_sweep.fetch_smartrecruiters("ExampleSystems")
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(target_sweep.smartrecruiters_rows("Example Systems", "ExampleSystems", postings)[0]["description"], "Support hotel IT. 3 years.")

    def test_icims_listing_page_is_parsed_paged_and_filtered(self):
        page = '''
        <div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span>
          <span title="6/3/2024 6:23 PM"> 6 months ago<span class="sr-only">(6/3/2024 6:23 PM)</span></span></div>
        <div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/9400/service-desk-technician/job?in_iframe=1"
          class="iCIMS_Anchor" title="9400 - Service Desk Technician"><span class="sr-only field-label">Job Title</span>
          <h3 > Service Desk Technician</h3></a></div>
        <dl><div><dt><span class="glyphicons" aria-hidden="true"></span><span class="sr-only field-label">Job Locations</span></dt>
          <dd class="iCIMS_JobHeaderData"><span > US-NC-Chapel Hill</span></dd></div></dl>
        <div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span>
          <span title="11/28/2024 9:00 AM"> 2 weeks ago</span></div>
        <div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/9401/dental-assistant/job?in_iframe=1"
          class="iCIMS_Anchor" title="9401 - Dental Assistant"><h3 > Dental Assistant</h3></a></div>
        <dl><div><dt><span class="sr-only field-label">Job Locations</span></dt><dd class="iCIMS_JobHeaderData"><span > US-NC</span></dd></div>
          <div><dt><span class="sr-only field-label">Location : Name</span></dt><dd class="iCIMS_JobHeaderData"><span > Downtown Greensboro</span></dd></div></dl>
        <div class="col-xs-6 header left"><span class="sr-only field-label">Posting City/State</span><span > Canton, GA</span></div>
        <div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span><span title="11/29/2024"> 1 week ago</span></div>
        <div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/9402/help-desk-analyst/job?in_iframe=1"
          class="iCIMS_Anchor" title="9402 - Help Desk Analyst"><h3 > Help Desk Analyst</h3></a></div>
        <div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span><span title="11/30/2024"> 1 week ago</span></div>
        <div class="col-xs-12 title"><a href="https://careers-x.icims.com/jobs/9403/it-infrastructure-technician/job?in_iframe=1"
          class="iCIMS_Anchor" title="9403 - IT | Infrastructure Technician"><h3 > IT | Infrastructure Technician</h3></a></div>
        '''
        jobs = target_sweep.icims_jobs(page)
        self.assertEqual([(j["id"], j["title"], j["posted"], j["location"], j["place"], j["city_state"]) for j in jobs], [
            ("9400", "Service Desk Technician", "6/3/2024 6:23 PM", "US-NC-Chapel Hill", "", ""),
            ("9401", "Dental Assistant", "11/28/2024 9:00 AM", "US-NC", "Downtown Greensboro", ""),
            ("9402", "Help Desk Analyst", "11/29/2024", "", "", "Canton, GA"),
            ("9403", "IT | Infrastructure Technician", "11/30/2024", "", "", "")])
        rows = target_sweep.icims_rows("Example Dental", "careers-x", jobs, home="Research Triangle Park, NC")
        self.assertEqual([(r["title"], r["location"], r["date_posted"]) for r in rows], [  # IT titles only
            ("Service Desk Technician", "Chapel Hill, NC", "2024-06-03"),
            ("Help Desk Analyst", "Canton, GA", "2024-11-29"),  # the board's own city wins over the home place
            ("IT | Infrastructure Technician", "Research Triangle Park, NC", "2024-11-30")])  # nothing named: home place
        self.assertEqual((rows[0]["job_url"], rows[0]["site"], rows[0]["is_remote"]),
                         ("https://careers-x.icims.com/jobs/9400/service-desk-technician/job", "icims:careers-x", "false"))
        self.assertEqual(target_sweep.icims_location(jobs[1]), "Downtown Greensboro, NC")
        pages = iter([page, page, ""])  # a repeated page (past the end) stops the paging
        job_page = '<html><body><nav>menu</nav><div class="iCIMS_JobContent"><p>Provide <b>Tier 2</b> support.</p></div></body></html>'
        fetched = []
        def fake_get_text(url):
            fetched.append(url)
            return next(pages) if "/jobs/search" in url else job_page
        with mock.patch.object(target_sweep, "get_text", side_effect=fake_get_text):
            found = target_sweep.fetch_icims("careers-x")
        self.assertEqual(len(found), 4)
        # 2024-12-20: the framed job view is fetched for the three IT titles, not for the dental assistant
        self.assertEqual(sum(1 for u in fetched if u.endswith("/job?in_iframe=1")), 3)
        self.assertEqual([j.get("_description", "") for j in found], ["Provide Tier 2 support.", "", "Provide Tier 2 support.", "Provide Tier 2 support."])
        self.assertEqual(target_sweep.icims_rows("Example Dental", "careers-x", found)[0]["description"], "Provide Tier 2 support.")

    def test_workday_building_names_map_to_towns_and_multi_location_jobs_expand(self):
        today = __import__("datetime").date(2024, 12, 10)
        rows = target_sweep.workday_rows("Example Resorts", "exampleresorts.wd5.myworkdayjobs.com",
                                         "exampleresortscareers", [
            {"title": "Systems Analyst II", "externalPath": "/job/x/Systems-Analyst-II_1",
             "locationsText": "Example Headquarters Building", "postedOn": "Posted Today"}], today)
        self.assertEqual(rows[0]["location"], "Apex, NC")
        listing = {"jobPostings": [{"title": "Field Support Technician", "externalPath": "/job/ExampleImaging-Garner/FST_1",
                                    "locationsText": "7 Locations", "postedOn": "Posted Today"},
                                   {"title": "Registered Nurse", "externalPath": "/job/x/RN_2", "locationsText": "Durham, NC", "postedOn": "Posted Today"}]}
        detail = {"jobPostingInfo": {"location": "ExampleImaging Garner", "additionalLocations": ["Durham, NC", "Garner, NC", "Durham, NC"],
                                     "jobDescription": "<p>Provide <b>Tier 2</b> desktop support for imaging centers.</p>"}}
        with mock.patch.object(target_sweep, "post_json", return_value=listing), \
                mock.patch.object(target_sweep, "get_json", return_value=detail) as detail_get:
            jobs = target_sweep.fetch_workday("exampleimaging.wd5.myworkdayjobs.com", "exampleimaging", "exampleimagingcareers")
        self.assertEqual(jobs[0]["locationsText"], "ExampleImaging Garner; Durham, NC; Garner, NC")
        self.assertEqual(detail_get.call_count, 1)  # one detail request per IT-titled job (the nurse gets none)
        # 2024-12-20: the detail's jobDescription becomes the row's text, so the judge never buys a Workday page
        self.assertEqual(jobs[0]["_description"], "Provide Tier 2 desktop support for imaging centers.")
        row = target_sweep.workday_rows("ExampleImaging", "exampleimaging.wd5.myworkdayjobs.com", "exampleimagingcareers", jobs, today)[0]
        self.assertEqual(row["description"], "Provide Tier 2 desktop support for imaging centers.")
        self.assertTrue(detail_get.call_args.args[0].endswith("/wday/cxs/exampleimaging/exampleimagingcareers/job/ExampleImaging-Garner/FST_1"))

    def test_only_home_region_or_remote_rows_are_kept(self):
        self.assertTrue(target_sweep.in_area({"location": "Cary, NC", "is_remote": "false"}))
        self.assertTrue(target_sweep.in_area({"location": "Anywhere", "is_remote": "true"}))
        self.assertFalse(target_sweep.in_area({"location": "Asheville, NC", "is_remote": "false"}))
        self.assertFalse(target_sweep.in_area({"location": "Reno, Nevada", "is_remote": "false"}))


class HubSpotMappingTests(unittest.TestCase):
    def test_rows_map_context_and_zero_cards_fail(self):
        cards = [{"title": "IT Support Engineer", "href": "https://hub/1",
                  "ctx": ["IT Support Engineer", "Remote - USA", "Information Technology"]}]
        row = hubspot_sweep.cards_to_rows(cards)[0]
        self.assertEqual((row["site"], row["location"], row["is_remote"]),
                         ("hubspot", "Remote - USA", "true"))
        with self.assertRaisesRegex(RuntimeError, "zero cards"):
            hubspot_sweep.validate_scan(0, first_page_timed_out=False)
        with self.assertRaisesRegex(RuntimeError, "all-openings selector"):
            hubspot_sweep.validate_scan(0, first_page_timed_out=True)

    def test_all_openings_url_is_single_page_and_never_uses_browser_credentials(self):
        self.assertEqual(hubspot_sweep.CAREERS_URL, "https://www.hubspot.com/careers/jobs/all")
        self.assertFalse(hasattr(hubspot_sweep, "resolve_browser_credentials"))

    def test_extract_cards_dedupes_job_links_and_keeps_role_context(self):
        cards = hubspot_sweep.extract_cards([
            {"href": "https://www.hubspot.com/careers/jobs/1", "title": "IT Support Engineer",
             "department": "Business Technology", "location": "Remote - USA"},
            {"href": "https://www.hubspot.com/careers/jobs/1", "title": "IT Support Engineer",
             "department": "Business Technology", "location": "Remote - USA"},
            {"href": "https://www.hubspot.com/careers/jobs/2", "title": "Account Executive",
             "department": "Sales", "location": "Remote - USA"},
        ])
        self.assertEqual(cards, [{"title": "IT Support Engineer", "href": "https://www.hubspot.com/careers/jobs/1",
                                  "ctx": ["IT Support Engineer", "Business Technology", "Remote - USA"]}])
        # A healthy board with no target roles is not a fetch failure.
        self.assertEqual(hubspot_sweep.extract_cards([
            {"href": "https://www.hubspot.com/careers/jobs/2", "title": "Account Executive",
             "department": "Sales", "location": "Remote - USA"},
        ]), [])

    def test_failure_line_survives_a_cp1252_stdout(self):
        # 2024-12-24: Playwright's "playwright install" box (U+2550 etc.) crashed print() on the night
        # task's cp1252 stdout, so the step died in its own error handler and hid the real error.
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        boom = RuntimeError("Executable doesn't exist\n╔═╗ playwright install ╚═╝")
        with mock.patch.object(hubspot_sweep, "fetch_cards", side_effect=boom), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(hubspot_sweep.main(), 1)
            out.flush()
        text = out.buffer.getvalue().decode("cp1252")
        self.assertIn("FETCH FAILED - RuntimeError: Executable doesn't exist", text)
        self.assertIn("playwright install", text)

    def test_credential_redaction(self):
        message = hubspot_sweep.redact_error(
            "connect wss://user:secret-password@brd.superproxy.io:9222 token=abc123",
            secrets=["secret-password", "abc123"],
        )
        self.assertNotIn("secret-password", message)
        self.assertNotIn("abc123", message)
        self.assertNotIn("wss://", message)


class LinkedInExitTests(unittest.TestCase):
    def test_zero_row_success_is_success_but_all_raised_fails(self):
        success = linkedin_pull.run_sections([lambda: []])
        with contextlib.redirect_stdout(io.StringIO()):  # the section failure line is expected here
            failure = linkedin_pull.run_sections([mock.Mock(side_effect=RuntimeError("boom"))])
        self.assertEqual(success, ([], 1, 1))
        self.assertEqual(failure[1:], (1, 0))

    def test_each_section_is_saved_before_the_next_one_runs(self):
        # 2024-12-10: the pull was killed at the step timeout three times and lost every section
        # because it merged only at the end. Now a section's rows are saved before the next starts.
        saved = []
        first = lambda: [{"title": "IT Specialist", "company": "A"}]
        def second():
            self.assertEqual(saved, [[{"title": "IT Specialist", "company": "A"}]])
            raise RuntimeError("killed here")
        with contextlib.redirect_stdout(io.StringIO()):
            rows, attempted, succeeded = linkedin_pull.run_sections([first, second, lambda: []], saved.append)
        self.assertEqual((len(rows), attempted, succeeded, len(saved)), (1, 3, 2, 1))

    def test_local_terms_are_the_five_that_covered_the_market_and_remote_keeps_all(self):
        self.assertEqual(len(linkedin_pull.LOCAL_TERMS), 5)
        self.assertTrue(set(linkedin_pull.LOCAL_TERMS) <= set(linkedin_pull.TERMS))
        self.assertEqual(len(linkedin_pull.TERMS), 11)

    def test_import_has_no_network_side_effect(self):
        code = "import scripts.linkedin_pull; print('ok')"
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_searches_home_region_second_metro_and_remote_us(self):
        # Example target areas: Raleigh-Durham and Greater Minneapolis.
        self.assertEqual(linkedin_pull.LOCATIONS,
                         (("Raleigh, NC", False), ("Minneapolis, MN", False), ("United States", True)))

    def test_remote_indeed_runs_without_the_time_window_that_drops_its_remote_filter(self):
        # JobSpy 1.1.82 ignores Indeed's remote filter whenever hours_old is set.
        self.assertEqual(linkedin_pull.search_settings("indeed", True, 100), (100, None))
        self.assertEqual(linkedin_pull.search_settings("indeed", False, 100), (25, 72))
        self.assertEqual(linkedin_pull.search_settings("linkedin", True, 100), (100, 72))

    def test_remote_search_rows_say_remote_in_the_location(self):
        rows = linkedin_pull.mark_remote([
            {"location": "Birmingham, AL"}, {"location": "Remote - US"}, {"location": float("nan")}, {"location": ""},
        ])
        self.assertEqual([r["location"] for r in rows], ["Remote (Birmingham, AL)", "Remote - US", "Remote", "Remote"])
        self.assertTrue(all(r["is_remote"] is True for r in rows))

    def test_posted_within_keeps_the_last_three_days(self):
        today = linkedin_pull.dt.date(2024, 12, 9)
        self.assertTrue(linkedin_pull.posted_within("2024-12-06", today=today))
        self.assertTrue(linkedin_pull.posted_within(linkedin_pull.dt.date(2024, 12, 9), today=today))
        self.assertFalse(linkedin_pull.posted_within("2024-12-05", today=today))
        self.assertFalse(linkedin_pull.posted_within("", today=today))

    def test_pay_keeps_the_published_interval_in_yearly_amounts(self):
        # JobSpy's own conversion rewrites the interval as "yearly", which hid hourly agency jobs (2024-12-09).
        nan = float("nan")
        rows = linkedin_pull.keep_pay_interval([
            {"interval": "hourly", "min_amount": 35.0, "max_amount": 45.0},
            {"interval": "yearly", "min_amount": 70000.0, "max_amount": 90000.0},
            {"interval": "monthly", "min_amount": 6000.0, "max_amount": nan},
            {"interval": "hourly", "min_amount": nan, "max_amount": nan},
            {"interval": nan, "min_amount": "", "max_amount": ""},
        ])
        self.assertEqual([r["pay_interval"] for r in rows], ["hourly", "yearly", "monthly", "hourly", ""])
        self.assertEqual((rows[0]["min_amount"], rows[0]["max_amount"]), (72800.0, 93600.0))
        self.assertEqual((rows[1]["min_amount"], rows[1]["max_amount"]), (70000.0, 90000.0))
        self.assertEqual(rows[2]["min_amount"], 72000.0)
        self.assertTrue(math.isnan(rows[2]["max_amount"]))
        self.assertEqual((rows[4]["min_amount"], rows[4]["max_amount"]), ("", ""))

    def test_hourly_pay_and_the_employer_link_reach_the_daily_file(self):
        import pandas as pd
        frame = pd.DataFrame([{
            "title": "IT Support Specialist", "company": "Kforce Inc", "location": "Durham, NC",
            "date_posted": linkedin_pull.dt.date.today().isoformat(), "interval": "hourly",
            "min_amount": 35.0, "max_amount": 45.0, "is_remote": False,
            "job_url": "https://www.indeed.com/viewjob?jk=1", "site": "indeed",
            "job_url_direct": "https://kforce.wd1.myworkdayjobs.com/x/job/1",  # the checker reads this, not Indeed's page
        }])
        from scripts import rate_control
        from tests.fakes import temp_gate
        calls = []
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(linkedin_pull, "api_json", side_effect=RuntimeError("offline")), \
                mock.patch.object(linkedin_pull, "merge_rows", return_value={"added": 1, "path": "x"}) as merge, \
                contextlib.redirect_stdout(io.StringIO()):
            # a gate on a manual clock: every sleep is virtual and the provider state stays out of the repo
            gate = temp_gate(state_dir, rate_control.LINKEDIN)
            self.assertEqual(linkedin_pull.main(["1"], scrape=lambda **kw: calls.append(kw) or frame, gate=gate), 0)
        self.assertTrue(calls and all(kw["enforce_annual_salary"] is False for kw in calls))
        (row,) = merge.call_args.args[0]
        self.assertEqual((row["min_amount"], row["max_amount"], row["pay_interval"]), (72800.0, 93600.0, "hourly"))
        self.assertEqual(row["job_url_direct"], "https://kforce.wd1.myworkdayjobs.com/x/job/1")


if __name__ == "__main__":
    unittest.main()
