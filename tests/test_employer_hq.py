"""North Carolina-headquarters check. No network: lookups are injected."""
import datetime as dt
import json
import pathlib
import tempfile
import unittest

from scripts import employer_hq as hq


def serp(organic=(), ai=None, knowledge=None):
    data = {"organic": [{"title": t, "description": d, "link": f"https://example.com/{i}"} for i, (t, d) in enumerate(organic)]}
    if ai:
        data["ai_overview"] = {"texts": [{"type": "paragraph", "snippet": ai}]}
    if knowledge:
        data["knowledge"] = knowledge
    return data


class ParseTests(unittest.TestCase):
    def test_ai_overview_street_address_in_home_state(self):
        # Shape of the real 2024-12-09 answer for Example Homes.
        data = serp(ai="Example Homes Corporation is headquartered at 5505 Waterford District Drive, Durham, North Carolina 33126.")
        self.assertEqual(hq.hq_from_serp(data, "Example Homes")["state"], "NC")

    def test_city_without_state_counts_when_it_is_a_home_state_city(self):
        data = serp(organic=[("Example Capital | About", "Example Capital is headquartered in Durham, with offices in "
                                                         "Atlanta, Minneapolis, Chicago, Dallas, Los Angeles and New York.")])
        self.assertEqual(hq.hq_from_serp(data, "Example Capital")["state"], "NC")

    def test_out_of_state_headquarters(self):
        data = serp(organic=[("Cumming Group | About", "Cumming Group is headquartered in Los Angeles, California."),
                             ("Cumming Careers", "Cumming Group headquarters: Los Angeles, CA 90017")])
        self.assertEqual(hq.hq_from_serp(data, "Cumming Group")["state"], "CA")

    def test_knowledge_panel_fact(self):
        data = serp(knowledge={"facts": [{"key": "Headquarters", "value": "Houston, TX"}]})
        self.assertEqual(hq.hq_from_serp(data, "FCC Environmental")["state"], "TX")

    def test_majority_wins_and_unrelated_snippets_are_ignored(self):
        data = serp(organic=[("Acme Tools", "Acme Tools is headquartered in Charlotte, North Carolina."),
                             ("Acme Tools jobs", "Acme Tools headquarters: Charlotte, NC"),
                             ("Someone else", "Other Co is headquartered in Denver, Colorado.")])
        self.assertEqual(hq.hq_from_serp(data, "Acme Tools")["state"], "NC")

    def test_big_city_without_a_state(self):
        data = serp(organic=[("Acme Analytics", "Acme Analytics is headquartered in Chicago, with offices in Durham.")])
        self.assertEqual(hq.hq_from_serp(data, "Acme Analytics")["state"], "IL")

    def test_nothing_found_is_unknown(self):
        self.assertIsNone(hq.hq_from_serp(serp(organic=[("Kith", "Shop sneakers and apparel.")]), "Kith")["state"])


def linkedin_item(name, city, area, country, headquarter=True):
    # Shape of harvestapi/linkedin-company results seen 2024-12-09.
    return {"name": name, "locations": [
        {"country": "GB", "city": "London", "geographicArea": None, "headquarter": False, "parsed": {"state": "England"}},
        {"country": country, "city": city, "geographicArea": area, "headquarter": headquarter, "parsed": {"state": area}},
    ]}


class LinkedInTests(unittest.TestCase):
    def test_flagged_headquarters_gives_the_state(self):
        self.assertEqual(hq.hq_from_linkedin([linkedin_item("Example Capital", "Durham", "North Carolina", "US")], "Example Capital")["state"], "NC")
        self.assertEqual(hq.hq_from_linkedin([linkedin_item("UNC Chapel Hill", "Cary", "nc", "US")], "UNC Chapel Hill")["state"], "NC")
        self.assertEqual(hq.hq_from_linkedin([linkedin_item("Cumming Group", "New York", "New York", "US")], "Cumming Group")["state"], "NY")

    def test_headquarters_abroad_is_not_home_state(self):
        found = hq.hq_from_linkedin([linkedin_item("DLA Piper", "Global", None, "GB")], "DLA Piper")
        self.assertEqual(found["state"], "non-US:GB")

    def test_a_different_company_or_no_flag_is_unknown(self):
        self.assertIsNone(hq.hq_from_linkedin([linkedin_item("Kithara Labs", "Denver", "Texas", "US")], "Kith")["state"])
        self.assertIsNone(hq.hq_from_linkedin([linkedin_item("Kith", "Brooklyn", "NY", "US", headquarter=False)], "Kith")["state"])
        self.assertIsNone(hq.hq_from_linkedin([], "Norwegian Cruise Line Holdings")["state"])

    def test_best_lookup_falls_back_to_google_when_apify_has_no_answer(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"APIFY_TOKEN": "test"}), \
                mock.patch.object(hq, "apify_lookup", return_value={"state": None, "evidence": "", "source": "a"}), \
                mock.patch.object(hq, "google_lookup", return_value={"state": "NC", "evidence": "x", "source": "g"}):
            self.assertEqual(hq.best_lookup("Example Homes")["state"], "NC")
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(hq, "apify_lookup", side_effect=AssertionError("no token, no Apify call")), \
                mock.patch.object(hq, "google_lookup", return_value={"state": "TX", "evidence": "x", "source": "g"}):
            self.assertEqual(hq.best_lookup("FCC Environmental")["state"], "TX")

    def test_the_apify_key_is_read_under_either_name(self):
        # The owner saved it as APIFY_API_TOKEN (2024-12-09); APIFY_TOKEN wins when both exist.
        from unittest import mock
        with mock.patch.dict("os.environ", {"APIFY_API_TOKEN": "k"}, clear=True):
            self.assertEqual(hq.apify_token(), "k")
        with mock.patch.dict("os.environ", {"APIFY_TOKEN": "a", "APIFY_API_TOKEN": "b"}, clear=True):
            self.assertEqual(hq.apify_token(), "a")
        with mock.patch.dict("os.environ", {"APIFY_API_TOKEN": "k"}, clear=True), \
                mock.patch.object(hq, "apify_lookup", return_value={"state": "NC", "evidence": "x", "source": "a"}) as apify, \
                mock.patch.object(hq, "google_lookup", side_effect=AssertionError("Apify answered first")):
            self.assertEqual(hq.best_lookup("Example Staffing Co")["state"], "NC")
            apify.assert_called_once()


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "employer-hq.json"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def lookup(self, result):
        def fake(employer):
            self.calls.append(employer)
            if isinstance(result, Exception):
                raise result
            return result
        return fake

    def test_found_state_is_cached_and_reused(self):
        today = dt.date(2024, 12, 9)
        fake = self.lookup({"state": "NC", "evidence": "headquartered in Durham, North Carolina", "source": "test"})
        self.assertEqual(hq.hq_state("Example Homes Corp.", lookup=fake, today=today, path=self.path), "NC")
        self.assertEqual(hq.hq_state("Example Homes", lookup=fake, today=today, path=self.path), "NC")
        self.assertEqual(len(self.calls), 1)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[hq.employer_key("Example Homes")]["state"], "NC")

    def test_unknown_is_retried_after_30_days_and_errors_are_not_cached(self):
        unknown = self.lookup({"state": None, "evidence": "", "source": "test"})
        self.assertIsNone(hq.hq_state("Mystery Co", lookup=unknown, today=dt.date(2024, 12, 9), path=self.path))
        self.assertIsNone(hq.hq_state("Mystery Co", lookup=unknown, today=dt.date(2024, 12, 29), path=self.path))
        self.assertEqual(len(self.calls), 1)
        hq.hq_state("Mystery Co", lookup=unknown, today=dt.date(2025, 1, 9), path=self.path)
        self.assertEqual(len(self.calls), 2)
        failing = self.lookup(RuntimeError("CLI missing"))
        self.assertIsNone(hq.hq_state("Broken Co", lookup=failing, today=dt.date(2024, 12, 9), path=self.path))
        self.assertNotIn(hq.employer_key("Broken Co"), json.loads(self.path.read_text(encoding="utf-8")))

    def test_a_home_state_name_needs_no_lookup(self):
        fake = self.lookup({"state": "MD", "evidence": "", "source": "test"})
        for name in ("UNC Chapel Hill", "Wake County", "North Carolina State University"):
            self.assertEqual(hq.hq_state(name, lookup=fake, today=dt.date(2024, 12, 9), path=self.path), "NC", name)
        self.assertEqual(self.calls, [])

    def test_known_outside_home_state_reads_the_cache_only(self):
        self.path.write_text(json.dumps({"dla piper": {"state": "MD"}, "example homes": {"state": "NC"},
                                         "mystery co": {"state": None}}), encoding="utf-8")
        self.assertTrue(hq.known_outside_home_state("DLA Piper", path=self.path))
        self.assertFalse(hq.known_outside_home_state("Example Homes", path=self.path))
        self.assertFalse(hq.known_outside_home_state("Mystery Co", path=self.path))
        self.assertFalse(hq.known_outside_home_state("Never Seen Inc", path=self.path))


if __name__ == "__main__":
    unittest.main()
