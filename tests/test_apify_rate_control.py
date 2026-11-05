"""Apify employer-HQ lookups under rate control: budgets, retries, cooldowns and the outage cache."""
import datetime as dt
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.fakes import ManualClock, http_error, temp_gate
from scripts import employer_hq as hq
from scripts import rate_control as rc

FL_ITEMS = [{"name": "Acme", "headquarters": "Durham, North Carolina"}]


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ApifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock()
        self.gate = temp_gate(self.tmp.name, rc.APIFY, self.clock)
        for patcher in (mock.patch.object(hq.rate_control, "get_gate", return_value=self.gate),
                        mock.patch.dict(os.environ, {"APIFY_TOKEN": "k"}),
                        mock.patch.object(hq, "hq_from_linkedin", side_effect=lambda items, e: {"state": "NC", "evidence": "x", "source": "apify"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, effects, employer="Acme"):
        queue = list(effects)

        def fake(request, timeout=None):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return Reply(json.dumps(item).encode())

        with mock.patch.object(hq.urllib.request, "urlopen", side_effect=fake) as urlopen:
            try:
                return hq.apify_lookup(employer)
            finally:
                self.urlopen = urlopen

    def test_success_is_paced_and_serialised(self):
        self.call([FL_ITEMS])
        self.call([FL_ITEMS])
        self.assertEqual(self.clock.sleeps, [2.0])
        self.assertEqual(self.gate.limiter._global._value, 1)

    def test_run_budget_is_30_new_employers(self):
        for n in range(30):
            self.call([FL_ITEMS], f"Co {n}")
        with self.assertRaises(rc.ProviderUnavailable) as caught:
            self.call([FL_ITEMS], "Co 31")
        self.assertEqual(caught.exception.reason, "run_budget")
        self.urlopen.assert_not_called()

    def test_daily_budget_is_60(self):
        self.gate.store.update("apify", lambda e: e["requests"].update(date=self.clock.now().date().isoformat(), today=60))
        with self.assertRaises(rc.ProviderUnavailable) as caught:
            self.call([FL_ITEMS])
        self.assertEqual(caught.exception.reason, "daily_budget")

    def test_429_retries_with_jittered_backoff_then_succeeds(self):
        self.clock.pick = "high"
        self.call([http_error(429), http_error(429), FL_ITEMS])
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual(self.clock.uniform_calls, [(1, 2), (2, 4)])

    def test_exhausted_429_starts_a_30_minute_cooldown(self):
        with self.assertRaises(rc.CallFailed):
            self.call([http_error(429)] * 3)
        self.assertEqual(self.urlopen.call_count, 3)
        until = rc.parse_time(self.gate.summary("apify")["cooldown_until"])
        self.assertEqual(until - self.clock.now(), dt.timedelta(minutes=30))
        with self.assertRaises(rc.ProviderUnavailable):
            self.call([FL_ITEMS])
        self.urlopen.assert_not_called()

    def test_5xx_retries_at_most_twice_with_5_10_then_15_30(self):
        with self.assertRaises(rc.CallFailed):
            self.call([http_error(503)] * 3)
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual(self.clock.uniform_calls, [(5, 10), (15, 30)])

    def test_auth_failure_is_not_retried_and_cools_down_for_24_hours(self):
        for code in (401, 403):
            self.setUp()
            with self.assertRaises(rc.CallFailed):
                self.call([http_error(code)])
            self.assertEqual(self.urlopen.call_count, 1)
            summary = self.gate.summary("apify")
            self.assertEqual(summary["state"], "failed")
            self.assertEqual(rc.parse_time(summary["cooldown_until"]) - self.clock.now(), dt.timedelta(hours=24))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "employer-hq.json"
        self.now = dt.datetime(2024, 12, 10, 1, 0, tzinfo=dt.timezone.utc)
        self.calls = 0

    def state(self, lookup, employer="Mystery Co", now=None):
        def counted(name):
            self.calls += 1
            return lookup(name)
        return hq.hq_state(employer, lookup=counted, today=(now or self.now).date(), now=now or self.now, path=self.path)

    @staticmethod
    def outage(name):
        raise hq.LookupFailed("apify_lookup: CallFailed", cache=True)

    def test_a_provider_outage_is_cached_for_6_hours_only(self):
        self.assertIsNone(self.state(self.outage))
        self.assertIsNone(self.state(self.outage, now=self.now + dt.timedelta(hours=5, minutes=59)))
        self.assertEqual(self.calls, 1)
        self.assertIsNone(self.state(self.outage, now=self.now + dt.timedelta(hours=6, minutes=1)))
        self.assertEqual(self.calls, 2)
        entry = json.loads(self.path.read_text(encoding="utf-8"))[hq.employer_key("Mystery Co")]
        self.assertIsNone(entry["state"])
        self.assertNotIn("checked_on", entry)

    def test_a_cooldown_or_budget_skip_is_not_cached(self):
        def held(name):
            raise hq.LookupFailed("held", cache=False)
        self.assertIsNone(self.state(held))
        self.assertIsNone(self.state(held))
        self.assertEqual(self.calls, 2)
        self.assertFalse(self.path.exists() and json.loads(self.path.read_text(encoding="utf-8")))

    def test_a_recovered_provider_is_asked_again_after_the_backoff(self):
        self.state(self.outage)
        found = lambda name: {"state": "NC", "evidence": "x", "source": "t"}
        self.assertEqual(self.state(found, now=self.now + dt.timedelta(hours=7)), "NC")

    def test_unknown_still_caches_for_30_days_and_home_state_is_permanent(self):
        unknown = lambda name: {"state": None, "evidence": "", "source": "t"}
        self.state(unknown)
        self.state(unknown, now=self.now + dt.timedelta(days=29))
        self.assertEqual(self.calls, 1)
        self.state(unknown, now=self.now + dt.timedelta(days=31))
        self.assertEqual(self.calls, 2)
        nc = lambda name: {"state": "NC", "evidence": "x", "source": "t"}
        self.assertEqual(self.state(nc, "Sunny Co"), "NC")
        self.assertEqual(self.state(nc, "Sunny Co", now=self.now + dt.timedelta(days=400)), "NC")
        self.assertEqual(self.calls, 3)

    def test_best_lookup_reports_failure_instead_of_unknown(self):
        def failing(employer):
            raise rc.CallFailed(rc.Failure("transient", "http_503", 503))

        def paused(employer):
            raise rc.ProviderUnavailable("http_429")

        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "k"}), \
                mock.patch.object(hq, "apify_lookup", failing), \
                mock.patch.object(hq, "google_lookup", paused):
            with self.assertRaises(hq.LookupFailed) as caught:
                hq.best_lookup("Acme")
        self.assertTrue(caught.exception.cache)

    def test_known_outside_home_state_stays_cache_only(self):
        self.path.write_text(json.dumps({hq.employer_key("Far Co"): {"name": "Far Co", "state": "TX"}}), encoding="utf-8")
        with mock.patch.object(hq.urllib.request, "urlopen", side_effect=AssertionError("network")), \
                mock.patch.object(hq.subprocess, "run", side_effect=AssertionError("network")):
            self.assertTrue(hq.known_outside_home_state("Far Co", self.path))
            self.assertFalse(hq.known_outside_home_state("Never Seen Co", self.path))


if __name__ == "__main__":
    unittest.main()
