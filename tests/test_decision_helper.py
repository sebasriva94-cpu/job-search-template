import http.client
import json
import pathlib
import tempfile
import threading
import unittest

from scripts import decision_helper


class DecisionHelperTests(unittest.TestCase):
    """The helper behind the dashboard's Applied / Later / Not for me buttons (2024-12-09)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "dashboard").mkdir()
        (self.root / "job-dashboard.html").write_text("<html>dashboard</html>", encoding="utf-8")
        self.server = decision_helper.serve(self.root, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = (self.root / "dashboard" / ".decision-token").read_text(encoding="utf-8")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def click(self, decision="skip", token=None, headers=None):
        return self.call("POST", "/decide", {"token": self.token if token is None else token, "decision": decision,
                                             "key": "acme|it support specialist", "company": "Acme",
                                             "title": "IT Support Specialist", "job_url": "https://x/1"}, headers)

    def saved(self):
        path = self.root / "dashboard" / "decisions.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def test_serves_the_dashboard_and_records_a_click(self):
        status, _, body = self.call("GET", "/")
        self.assertEqual((status, body), (200, b"<html>dashboard</html>"))
        status, headers, _ = self.click("later", headers={"Origin": "null"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "null")  # the page opened as a file can read the reply
        self.assertEqual(self.saved()["acme|it support specialist"]["decision"], "later")
        status, _, body = self.call("GET", "/decisions")
        self.assertEqual(json.loads(body), {"acme|it support specialist": "later"})
        self.click("undo")
        self.assertEqual(self.saved(), {})

    def test_refuses_clicks_without_the_token_or_from_other_sites(self):
        self.assertEqual(self.click(token="wrong")[0], 403)
        self.assertEqual(self.click(headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.click(headers={"Host": "evil.example"})[0], 403)  # a renamed address (DNS trick)
        self.assertEqual(self.click(decision="delete-everything")[0], 400)
        self.assertEqual(self.saved(), {})
        status, headers, _ = self.call("GET", "/decisions", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)  # another site cannot read the owner's clicks


if __name__ == "__main__":
    unittest.main()
