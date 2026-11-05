"""linkedin_detail: the posting text from LinkedIn's public job page, parsed without network."""
import unittest

from scripts import linkedin_detail as ld

LD = ('<html><head><script type="application/ld+json">{"@context":"https://schema.org/","@type":"JobPosting",'
      '"title":"IT Support Specialist","description":"&lt;p&gt;Provide Tier 2 support for 600 employees.&lt;/p&gt;'
      '&lt;ul&gt;&lt;li&gt;Manage Intune and Jamf&lt;/li&gt;&lt;/ul&gt;"}</script></head><body></body></html>')
MARKUP = ('<div class="show-more-less-html__markup relative overflow-hidden"><p>Desktop support for a Chapel Hill office.</p>'
          '<p>Okta and Entra administration.</p></div><button class="show-more-less-html__button">Show more</button>')


class JobIdTests(unittest.TestCase):
    def test_ids_from_the_url_shapes_the_feed_and_alert_emails_use(self):
        for url, want in (
            ("https://www.linkedin.com/jobs/view/4479638419", "4479638419"),
            ("https://www.linkedin.com/jobs/view/systems-administrator-at-acme-4479638419?refId=x", "4479638419"),
            ("https://www.linkedin.com/comm/jobs/view/4479638419/?trackingId=abc", "4479638419"),
            ("https://www.linkedin.com/jobs/search/?currentJobId=4479638419&keywords=it", "4479638419"),
            ("https://jobs.ashbyhq.com/acme/uuid", ""),
        ):
            with self.subTest(url=url):
                self.assertEqual(ld.job_id(url), want)
        self.assertEqual(ld.view_url("https://www.linkedin.com/jobs/view/it-x-4479638419"), "https://www.linkedin.com/jobs/view/4479638419")
        self.assertEqual(ld.view_url("4479638419"), "https://www.linkedin.com/jobs/view/4479638419")
        self.assertEqual(ld.view_url("https://indeed.com/viewjob?jk=1"), "")


class ParseTests(unittest.TestCase):
    def test_json_ld_description_wins_and_is_plain_text(self):
        self.assertEqual(ld.description_from_html(LD), "Provide Tier 2 support for 600 employees. Manage Intune and Jamf")

    def test_markup_fallback(self):
        self.assertEqual(ld.description_from_html(MARKUP), "Desktop support for a Chapel Hill office. Okta and Entra administration.")

    def test_nothing_readable_is_empty(self):
        self.assertEqual(ld.description_from_html("<html><body><title>Sign in</title></body></html>"), "")
        self.assertEqual(ld.description_from_html(""), "")


class ClassifyTests(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(ld.classify({"status": 200, "html": LD, "final_url": "https://www.linkedin.com/jobs/view/1"}), "ok")
        self.assertEqual(ld.classify({"status": 200, "html": "<html></html>", "final_url": "https://www.linkedin.com/jobs/view/1"}), "empty")
        self.assertEqual(ld.classify({"status": 429, "html": "", "final_url": ""}), "rate_limited")
        self.assertEqual(ld.classify({"status": 999, "html": "", "final_url": ""}), "auth")
        self.assertEqual(ld.classify({"status": 200, "html": LD, "final_url": "https://www.linkedin.com/authwall?x=1"}), "auth")
        self.assertEqual(ld.classify({"status": 503, "html": "", "final_url": ""}), "transient")
        self.assertEqual(ld.classify({"status": None, "html": "", "final_url": "", "error": "timeout"}), "transient")


if __name__ == "__main__":
    unittest.main()
