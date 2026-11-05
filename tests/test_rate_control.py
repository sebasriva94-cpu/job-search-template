"""Shared rate-control layer: state, cooldowns, budgets, pacing, retries. No real sleeps or network."""
import datetime as dt
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from scripts import rate_control as rc
from tests.fakes import ManualClock, http_error


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = ManualClock()
        self.path = self.dir / "dashboard" / "provider-state.json"
        self.store = rc.StateStore(self.path, self.clock.runtime())

    def gate(self, policy):
        return rc.Gate(policy, self.store, self.clock.runtime())

    def test_active_then_expired_cooldown(self):
        gate = self.gate(rc.Policy("p"))
        until = gate.cooldown("p", seconds=3600, reason="http_429")
        blocked = gate.admit("p")
        self.assertEqual((blocked.status, blocked.reason, blocked.cooldown_until), (rc.SKIPPED, "http_429", until))
        self.assertEqual(gate.summary("p")["state"], "cooldown")
        self.clock.advance(hours=1, seconds=1)
        self.assertTrue(gate.admit("p").ok)
        gate.success("p")
        self.assertEqual(gate.summary("p")["state"], "ok")

    def test_cooldown_survives_a_new_process(self):
        self.gate(rc.Policy("p")).cooldown("p", seconds=600, reason="http_429")
        again = rc.Gate(rc.Policy("p"), rc.StateStore(self.path, self.clock.runtime()), self.clock.runtime())
        self.assertEqual(again.admit("p").status, rc.SKIPPED)

    def test_cooldown_is_never_shortened_and_a_success_does_not_lift_it(self):
        gate = self.gate(rc.Policy("p"))
        long = gate.cooldown("p", seconds=7200, reason="http_429")
        self.assertEqual(gate.cooldown("p", seconds=60, reason="http_503"), long)
        gate.success("p")
        self.assertEqual(gate.summary("p")["state"], "cooldown")

    def test_retry_after_seconds_and_http_date(self):
        now = self.clock.now()
        self.assertEqual(rc.parse_retry_after("120"), 120)
        self.assertEqual(rc.parse_retry_after("999999999"), rc.MAX_RETRY_AFTER)
        self.assertIsNone(rc.parse_retry_after("soon"))
        self.assertIsNone(rc.parse_retry_after(None))
        when = (now + dt.timedelta(seconds=90)).astimezone(dt.timezone.utc)
        header = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(rc.parse_retry_after(header, now), 90, delta=1)
        failure = rc.classify_exception(http_error(429, "45"), now)
        self.assertEqual((failure.kind, failure.retry_after), ("rate_limited", 45))

    def test_daily_budget_resets_on_the_next_day(self):
        gate = self.gate(rc.Policy("p", daily_budget=2))
        self.assertTrue(gate.admit("p").ok)
        self.assertTrue(gate.admit("p").ok)
        third = gate.admit("p")
        self.assertEqual((third.status, third.reason), (rc.SKIPPED, "daily_budget"))
        self.clock.advance(days=1)
        self.assertTrue(self.gate(rc.Policy("p", daily_budget=2)).admit("p").ok)

    def test_run_budget_is_per_gate_instance(self):
        gate = self.gate(rc.Policy("p", run_budget=1))
        self.assertTrue(gate.admit("p").ok)
        self.assertEqual(gate.admit("p").reason, "run_budget")
        self.assertTrue(self.gate(rc.Policy("p", run_budget=1)).admit("p").ok)  # the next run

    def test_save_is_atomic_and_leaves_no_residue(self):
        gate = self.gate(rc.Policy("p"))
        gate.success("p")
        gate.failure("p", "http_500")
        names = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(names, ["provider-state.json"])
        self.assertEqual(json.loads(self.path.read_text())["providers"]["p"]["last_failure_reason"], "http_500")

    def test_corrupt_or_missing_state_recovers(self):
        self.assertEqual(self.store.get("x")["status"], "ok")
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        gate = self.gate(rc.Policy("p"))
        self.assertTrue(gate.admit("p").ok)
        self.assertEqual(json.loads(self.path.read_text())["providers"]["p"]["requests"]["today"], 1)
        self.assertTrue((self.path.parent / "provider-state.json.corrupt").exists())

    def test_concurrent_updates_are_not_lost(self):
        gate = rc.Gate(rc.Policy("p"), rc.StateStore(self.path), self.clock.runtime())
        threads = [threading.Thread(target=lambda: [gate.admit("p") for _ in range(10)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.get("p")["requests"]["total"], 40)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["provider-state.json"])

    def test_consecutive_degraded_runs_count_and_reset(self):
        gate = self.gate(rc.Policy("p"))
        self.assertEqual([gate.finish_run("p", True), gate.finish_run("p", True), gate.finish_run("p", False)], [1, 2, 0])

    def test_circuit_opens_after_three_transients_in_the_window(self):
        gate = self.gate(rc.Policy("p"))
        self.assertEqual(gate.transient("p", "http_503"), "")
        self.clock.advance(minutes=11)  # the first one ages out of the 10-minute window
        self.assertEqual(gate.transient("p", "http_503"), "")
        self.clock.advance(minutes=1)
        self.assertEqual(gate.transient("p", "http_503"), "")
        until = gate.transient("p", "http_503")
        self.assertTrue(until)
        self.assertEqual(gate.summary("p")["reason"], "circuit_open")
        self.clock.advance(minutes=31)
        self.assertTrue(gate.admit("p").ok)

    def test_sanitize_removes_urls_and_secrets(self):
        text = rc.sanitize("failed https://api.x.io/run?token=abc Authorization: Bearer " + "sk" + "-123 pass" + "word=hunter2 " + "z" * 500)
        self.assertNotIn("https", text)
        self.assertNotIn("sk-123", text)
        self.assertNotIn("hunter2", text)
        self.assertLessEqual(len(text), 160)

    def test_status_line_is_json_and_states_degradation(self):
        line = rc.status_line({"a": {"state": "ok"}, "b": {"state": "cooldown", "reason": "http_429"}})
        self.assertTrue(line.startswith(rc.STATUS_PREFIX))
        self.assertEqual(json.loads(line[len(rc.STATUS_PREFIX):])["state"], "degraded")
        self.assertEqual(rc.overall_state({"a": {"state": "failed"}, "b": {"state": "cooldown"}}), "failed")


class LimiterTests(unittest.TestCase):
    def test_starts_are_paced_without_real_sleep(self):
        clock = ManualClock()
        limiter = rc.Limiter(clock.runtime(), 1, 0, min_interval=1.5)
        for _ in range(3):
            with limiter.slot():
                pass
        self.assertEqual(clock.sleeps, [1.5, 1.5])

    def test_same_host_interval_does_not_delay_another_host(self):
        clock = ManualClock()
        limiter = rc.Limiter(clock.runtime(), 4, 1, host_interval=1.0)
        for host in ("a", "b", "a"):
            with limiter.slot(host):
                pass
        self.assertEqual(clock.sleeps, [1.0])

    def test_interval_override_for_html_pages(self):
        clock = ManualClock()
        limiter = rc.Limiter(clock.runtime(), 2, 1, host_interval=1.0)
        for _ in range(2):
            with limiter.slot("x", interval=1.5):
                pass
        self.assertEqual(clock.sleeps, [1.5])

    def run_threads(self, limiter, workers, hosts):
        active, peak, lock = {}, {"all": 0, "host": 0}, threading.Lock()
        release = threading.Event()

        def work(host):
            with limiter.slot(host):
                with lock:
                    active[host] = active.get(host, 0) + 1
                    peak["all"] = max(peak["all"], sum(active.values()))
                    peak["host"] = max(peak["host"], active[host])
                release.wait(0.05)
                with lock:
                    active[host] -= 1

        threads = [threading.Thread(target=work, args=(hosts[i % len(hosts)],)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return peak

    def test_global_concurrency_cap(self):
        limiter = rc.Limiter(ManualClock().runtime(), 2)
        self.assertEqual(self.run_threads(limiter, 8, [""])["all"], 2)

    def test_same_host_serialization_with_parallel_hosts(self):
        limiter = rc.Limiter(ManualClock().runtime(), 4, 1)
        peak = self.run_threads(limiter, 12, ["a", "b", "c"])
        self.assertEqual(peak["host"], 1)
        self.assertLessEqual(peak["all"], 3)


class RetryTests(unittest.TestCase):
    def call(self, errors, schedule, pick="low"):
        clock = ManualClock(pick=pick)
        queue = list(errors)

        def fn():
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return clock, rc.retry_call(fn, clock.runtime(), schedule)

    def test_retries_use_the_scheduled_ranges_then_succeed(self):
        schedule = {"rate_limited": ((1, 2), (2, 4), (4, 8))}
        clock, result = self.call([http_error(429), http_error(429), "done"], schedule)
        self.assertEqual((result, clock.uniform_calls, clock.sleeps), ("done", [(1, 2), (2, 4)], [1, 2]))

    def test_exhausted_schedule_raises_a_classified_failure(self):
        clock = ManualClock()
        with self.assertRaises(rc.CallFailed) as ctx:
            rc.retry_call(lambda: (_ for _ in ()).throw(http_error(503)), clock.runtime(), {"transient": ((5, 10),)})
        self.assertEqual((ctx.exception.failure.kind, clock.sleeps), ("transient", [5]))

    def test_unscheduled_kinds_are_not_retried(self):
        for error, kind in ((http_error(404), "not_found"), (http_error(403), "auth"), (http_error(402), "quota"),
                            (ValueError("bad json"), "other")):
            clock = ManualClock()
            calls = []
            with self.assertRaises(rc.CallFailed) as ctx:
                rc.retry_call(lambda: calls.append(1) or (_ for _ in ()).throw(error), clock.runtime(), {"transient": ((1, 2),)})
            self.assertEqual((ctx.exception.failure.kind, len(calls), clock.sleeps), (kind, 1, []))

    def test_timeouts_and_network_errors_are_transient(self):
        for exc in (socket.timeout(), TimeoutError(), ConnectionResetError(), OSError("dns")):
            self.assertEqual(rc.classify_exception(exc).kind, "transient")


if __name__ == "__main__":
    unittest.main()
