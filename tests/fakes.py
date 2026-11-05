"""Deterministic clock, sleeper and jitter for the rate-control tests: nothing here sleeps for real."""
import asyncio  # noqa: F401 -- imported before the msvcrt shim: the stdlib probes for msvcrt at import time
import datetime as dt
import subprocess  # noqa: F401
import sys
import types
import unittest.mock  # noqa: F401
import urllib.error

try:
    import msvcrt  # noqa: F401
except ImportError:  # the project is Windows-first; let these tests run elsewhere with a flock-backed shim
    import fcntl

    shim = types.ModuleType("msvcrt")
    shim.LK_NBLCK, shim.LK_UNLCK = 1, 0
    shim.locking = lambda fd, mode, size: fcntl.flock(fd, fcntl.LOCK_UN if mode == 0 else fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.modules["msvcrt"] = shim

from scripts import rate_control

TZ = dt.timezone(dt.timedelta(hours=-4))


class ManualClock:
    """now(), monotonic() and sleep() share one timeline; sleep() only advances it."""

    def __init__(self, start=None, pick="low"):
        self.t = start or dt.datetime(2024, 12, 10, 1, 0, tzinfo=TZ)
        self.mono = 1000.0
        self.sleeps = []
        self.uniform_calls = []
        self.pick = pick

    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def advance(self, **kwargs):
        delta = dt.timedelta(**kwargs)
        self.t += delta
        self.mono += delta.total_seconds()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds=seconds)

    def uniform(self, low, high):
        self.uniform_calls.append((low, high))
        return {"low": low, "high": high, "mid": (low + high) / 2}[self.pick]

    def runtime(self):
        return rate_control.Runtime(self.now, self.sleep, self.uniform, self.monotonic)


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://example.invalid/x", code, "err", headers, None)


def temp_gate(state_dir, policy=None, clock=None):
    """A Gate whose state lives in `state_dir`, on a manual clock (so nothing sleeps and nothing leaks)."""
    clock = clock or ManualClock()
    store = rate_control.StateStore(f"{state_dir}/provider-state.json", clock.runtime())
    gate = rate_control.Gate(policy or rate_control.BRIGHTDATA, store, clock.runtime())
    gate.clock = clock
    return gate
