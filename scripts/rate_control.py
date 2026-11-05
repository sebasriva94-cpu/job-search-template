"""Provider-aware rate control shared by every network source of the job-search automation.

One persistent state file (dashboard/provider-state.json, git-ignored) remembers, per provider or
per provider host: status, reason, cooldown_until, consecutive_degraded_runs, request counters and
the last success / failure. A cooldown set by one process run is honoured by the next one, so a
throttled provider is never retried by the scheduler or hammered on the next morning.

Building blocks:
  Runtime      injectable clock, sleep and jitter, so tests never sleep for real
  StateStore   atomic, lock-protected JSON; a missing or corrupt file starts clean
  Limiter      max concurrency, same-host concurrency and minimum request-start interval
  Gate         limiter + budgets (per run, per day) + cooldowns for one provider policy
  retry_call   bounded retries with jittered waits, classified by HTTP status / exception
  Outcome      explicit result of a request admission: ok, degraded, skipped or failed

Compliance: nothing here rotates proxies, spoofs identities or works around a challenge page. A
429, CAPTCHA, auth wall or quota error stops the work and starts a cooldown.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import email.utils
import json
import os
import random
import re
import socket
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

OK, DEGRADED, SKIPPED, FAILED = "ok", "degraded", "skipped", "failed"
COOLDOWN = "cooldown"
STATE_PATH = Path("dashboard") / "provider-state.json"
STATUS_PREFIX = "PROVIDER_STATUS "   # one stdout line per step; pull-jobs.ps1 folds it into pipeline-status.json
MAX_RETRY_AFTER = 24 * 3600
HISTORY_LIMIT = 7


# --- time, randomness ----------------------------------------------------------------------

class Runtime:
    """Clock, sleep, jitter and monotonic time in one injectable bundle."""

    def __init__(self, now=None, sleep=None, uniform=None, monotonic=None):
        self._now = now or (lambda: dt.datetime.now().astimezone())
        self.sleep = sleep or time.sleep
        self.uniform = uniform or random.uniform
        self.monotonic = monotonic or time.monotonic

    def now(self) -> dt.datetime:
        value = self._now()
        return value if value.tzinfo else value.astimezone()


def parse_time(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


# --- sanitising ----------------------------------------------------------------------------

_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_SECRET = re.compile(r"(?i)\bbearer\s+\S+|\b(?:token|password|authorization|api[_-]?key|secret)\b\s*[=:]?\s*(?:(?:bearer|basic)\s+)?\S+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(text: object, limit: int = 160) -> str:
    """Short single-line text with URLs, credentials and control characters removed."""
    safe = _URL.sub("[redacted-url]", str(text or ""))
    safe = _SECRET.sub("[redacted-secret]", safe)
    return _CONTROL.sub(" ", safe).strip()[:limit]


# --- failures and retries ------------------------------------------------------------------

class ControlError(Exception):
    """A request was not made, or failed, under rate control. Never carries raw third-party content."""

    def __init__(self, reason: str, outcome: str = FAILED, cooldown_until: str = ""):
        super().__init__(sanitize(reason))
        self.reason, self.outcome, self.cooldown_until = sanitize(reason, 60), outcome, cooldown_until


class ProviderUnavailable(ControlError):
    """Cooldown, circuit breaker or budget: the request was deliberately not made (outcome skipped)."""

    def __init__(self, reason: str, cooldown_until: str = ""):
        super().__init__(reason, SKIPPED, cooldown_until)


class CallFailed(ControlError):
    """A request was made and failed after its bounded retries."""

    def __init__(self, failure: "Failure"):
        super().__init__(failure.reason, FAILED)
        self.failure = failure


@dataclasses.dataclass(frozen=True)
class Failure:
    kind: str            # rate_limited | quota | auth | not_found | transient | client_error | other
    reason: str          # short code such as http_429, timeout
    status: int | None = None
    retry_after: float | None = None   # seconds, from a Retry-After header


@dataclasses.dataclass(frozen=True)
class Outcome:
    status: str = OK
    reason: str = ""
    cooldown_until: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


def parse_retry_after(value: object, now: dt.datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date); None when absent or unusable."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        seconds = (when - (now or dt.datetime.now().astimezone())).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def classify_status(status: int | None, retry_after: object = None, now: dt.datetime | None = None) -> Failure:
    delay = parse_retry_after(retry_after, now)
    if status == 429:
        return Failure("rate_limited", "http_429", status, delay)
    if status == 402:
        return Failure("quota", "http_402", status)
    if status in (401, 403):
        return Failure("auth", f"http_{status}", status)
    if status in (404, 410):
        return Failure("not_found", f"http_{status}", status)
    if isinstance(status, int) and (status >= 500 or status == 408):
        return Failure("transient", f"http_{status}", status, delay)
    if isinstance(status, int) and status >= 400:
        return Failure("client_error", f"http_{status}", status)
    return Failure("other", "error")


def classify_exception(exc: BaseException, now: dt.datetime | None = None) -> Failure:
    """Classify an exception from urllib/requests style clients (duck-typed .code / .status / .headers)."""
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        return classify_status(status, retry_after, now)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return Failure("transient", "timeout")
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return Failure("transient", "network_error")
    return Failure("other", f"error_{type(exc).__name__}"[:60])


def retry_call(fn, runtime: Runtime, schedule: dict, classify=classify_exception):
    """Call fn(); retry only failure kinds listed in `schedule`, each with its own jittered waits.

    schedule maps a failure kind to a sequence of (low, high) second ranges, one per retry. Anything
    else, or an exhausted schedule, raises CallFailed (carrying the Failure) at once.
    """
    tries: dict[str, int] = {}
    while True:
        try:
            return fn()
        except ControlError:
            raise
        except Exception as exc:  # noqa: BLE001 -- classified below, never propagated raw
            failure = classify(exc, runtime.now())
            waits = schedule.get(failure.kind, ())
            used = tries.get(failure.kind, 0)
            if used >= len(waits):
                raise CallFailed(failure) from exc
            tries[failure.kind] = used + 1
            runtime.sleep(runtime.uniform(*waits[used]))


# --- persistent state ----------------------------------------------------------------------

def default_root() -> Path:
    if os.environ.get("JOBSEARCH_ROOT"):
        return Path(os.environ["JOBSEARCH_ROOT"])
    try:
        try:
            from scripts.sweep_common import project_root
        except ModuleNotFoundError:
            from sweep_common import project_root
        return project_root()
    except (ImportError, RuntimeError):  # sweep_common needs Windows locking
        return Path.cwd()


def blank_entry() -> dict:
    return {
        "status": OK, "reason": "", "cooldown_until": "", "consecutive_degraded_runs": 0,
        "requests": {"date": "", "today": 0, "total": 0},
        "last_success": "", "last_failure": "", "last_failure_reason": "", "last_error": "",
        "transient": [],
    }


class StateStore:
    """Provider-keyed JSON state with atomic writes and a cross-process lock file."""

    LOCK_WAIT = 5.0
    LOCK_STALE = 30.0

    def __init__(self, path: Path | str, runtime: Runtime | None = None):
        self.path = Path(path)
        self.runtime = runtime or Runtime()
        self._thread_lock = threading.RLock()
        self._memory: dict | None = None  # used when the file cannot be written

    # -- file handling
    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), dict):
                return data
            raise ValueError("unexpected state shape")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            with contextlib.suppress(OSError):  # keep the bad file for inspection, start clean
                os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
        return self._memory if self._memory is not None else {"version": 1, "providers": {}}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=1, sort_keys=True, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            self._memory = None
        except OSError:
            self._memory = data

    @contextlib.contextmanager
    def _file_lock(self):
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + self.LOCK_WAIT
        held = False
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    held = True
                    break
                except FileExistsError:
                    with contextlib.suppress(OSError):
                        if time.time() - lock.stat().st_mtime > self.LOCK_STALE:
                            lock.unlink()
                            continue
                    if time.monotonic() >= deadline:
                        break  # proceed unlocked rather than stall the pipeline
                    time.sleep(0.02)
        except OSError:
            pass
        try:
            yield
        finally:
            if held:
                with contextlib.suppress(OSError):
                    lock.unlink()

    # -- access
    def get(self, key: str) -> dict:
        with self._thread_lock:
            return {**blank_entry(), **(self.load()["providers"].get(key) or {})}

    def update(self, key: str, mutate) -> dict:
        """Load, apply mutate(entry), save; returns the updated entry. Safe across threads and processes."""
        with self._thread_lock, self._file_lock():
            data = self.load()
            entry = {**blank_entry(), **(data["providers"].get(key) or {})}
            entry["requests"] = {**blank_entry()["requests"], **(entry.get("requests") or {})}
            mutate(entry)
            data["providers"][key] = entry
            self._save(data)
            return entry

    def all(self) -> dict:
        with self._thread_lock:
            return dict(self.load()["providers"])


# --- concurrency and pacing ----------------------------------------------------------------

class Limiter:
    """Max concurrency (global and per host) plus a minimum interval between request starts."""

    def __init__(self, runtime: Runtime, max_concurrency: int = 1, per_host: int = 0,
                 min_interval: float = 0.0, host_interval: float = 0.0):
        self.runtime, self.per_host = runtime, per_host
        self.min_interval, self.host_interval = min_interval, host_interval
        self._global = threading.BoundedSemaphore(max(1, max_concurrency))
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._mutex = threading.Lock()
        self._next_global = 0.0
        self._next_host: dict[str, float] = {}

    def _pace(self, host: str, interval: float | None) -> None:
        with self._mutex:
            now = self.runtime.monotonic()
            global_gap = max(self.min_interval, interval or 0.0)
            host_gap = max(self.host_interval, interval or 0.0)
            start = max(now, self._next_global, self._next_host.get(host, 0.0))
            self._next_global = start + global_gap
            self._next_host[host] = start + host_gap
            wait = start - now
        if wait > 0:
            self.runtime.sleep(wait)

    @contextlib.contextmanager
    def slot(self, host: str = "", interval: float | None = None):
        """Hold a concurrency slot (same-host first, so a busy host never starves the others) and pace the start."""
        host_sem = None
        if self.per_host and host:
            with self._mutex:
                host_sem = self._hosts.setdefault(host, threading.BoundedSemaphore(self.per_host))
        if host_sem:
            host_sem.acquire()
        self._global.acquire()
        try:
            self._pace(host, interval)
            yield
        finally:
            self._global.release()
            if host_sem:
                host_sem.release()


# --- policies and gates --------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Policy:
    name: str
    max_concurrency: int = 1
    per_host: int = 0
    min_interval: float = 0.0       # between any two request starts
    host_interval: float = 0.0      # between two request starts to the same host
    run_budget: int | None = None
    daily_budget: int | None = None


# Budgets settled 2024-12-10: every LinkedIn group each night (5 local + 11 remote; the measured
# coverage needs all of them), Bright Data above the 14-30 listing checks a render actually makes,
# Apify above the handful of new employers a day the headquarters cache does not already hold.
LINKEDIN = Policy("linkedin", max_concurrency=1, per_host=1, daily_budget=16)       # budget unit: one query group
BRIGHTDATA = Policy("brightdata", max_concurrency=2, min_interval=1.5, run_budget=60, daily_budget=150)
APIFY = Policy("apify", max_concurrency=1, min_interval=2.0, run_budget=30, daily_budget=60)  # unit: one new employer
BRIGHTDATA_FEED = Policy("brightdata_feed", max_concurrency=1, daily_budget=300)  # unit: one LinkedIn record requested (the owner's cap, 2024-12-18)
ATS_FAST = Policy("ats-fast", max_concurrency=4, per_host=1, host_interval=0.25)
ATS_SLOW = Policy("ats-slow", max_concurrency=2, per_host=1, host_interval=1.0)
# 2024-12-23: the queue-check's LinkedIn fallback for a Bright Data outage (scrapling direct guest-page
# fetch). One page at a time, spaced like a reader, one fetch per queue row (15) and three chains a day.
LINKEDIN_DIRECT = Policy("linkedin_direct", max_concurrency=1, per_host=1, host_interval=4.0,
                         run_budget=15, daily_budget=45)


class Gate:
    """Admission control for one policy: cooldowns, budgets, pacing and the persisted outcome of every call."""

    def __init__(self, policy: Policy, store: StateStore, runtime: Runtime | None = None):
        self.policy, self.store = policy, store
        self.runtime = runtime or store.runtime
        self.limiter = Limiter(self.runtime, policy.max_concurrency, policy.per_host,
                               policy.min_interval, policy.host_interval)
        self._lock = threading.Lock()
        self._run_used: dict[str, int] = {}
        self.touched: dict[str, None] = {}

    def slot(self, host: str = "", interval: float | None = None):
        return self.limiter.slot(host, interval)

    # -- admission
    def blocked(self, key: str) -> Outcome | None:
        """A skipped Outcome while a cooldown is active (no budget is spent), else None."""
        entry = self.store.get(key)
        until = parse_time(entry.get("cooldown_until"))
        if until and until > self.runtime.now():
            self.touched[key] = None
            return Outcome(SKIPPED, entry.get("reason") or "cooldown", iso(until))
        return None

    def admit(self, key: str, units: int = 1) -> Outcome:
        """Check cooldown, then the per-run and per-day budgets, and spend `units` when all pass."""
        self.touched[key] = None
        with self._lock:
            now = self.runtime.now()
            decision: list[Outcome] = []

            def mutate(entry: dict) -> None:
                until = parse_time(entry.get("cooldown_until"))
                if until and until > now:
                    decision.append(Outcome(SKIPPED, entry.get("reason") or "cooldown", iso(until)))
                    return
                entry["cooldown_until"] = ""
                counters, today = entry["requests"], now.date().isoformat()
                if counters.get("date") != today:
                    counters["date"], counters["today"] = today, 0
                reason = ""
                if self.policy.daily_budget is not None and counters["today"] + units > self.policy.daily_budget:
                    reason = "daily_budget"
                elif self.policy.run_budget is not None and self._run_used.get(key, 0) + units > self.policy.run_budget:
                    reason = "run_budget"
                if reason:
                    entry["status"], entry["reason"] = SKIPPED, reason
                    decision.append(Outcome(SKIPPED, reason))
                    return
                counters["today"] += units
                counters["total"] = int(counters.get("total", 0)) + units
                decision.append(Outcome(OK))

            self.store.update(key, mutate)
            if decision[0].ok:
                self._run_used[key] = self._run_used.get(key, 0) + units
            return decision[0]

    def refund(self, key: str, units: int) -> None:
        """Give back units that admit() spent when the paid call never happened (2024-12-19: a refused feed
        trigger otherwise consumed the night's records and the catch-up chain's feed step was told 'already spent')."""
        self.touched[key] = None
        with self._lock:
            def mutate(entry: dict) -> None:
                counters = entry.setdefault("requests", {})
                counters["today"] = max(0, int(counters.get("today", 0)) - units)
                counters["total"] = max(0, int(counters.get("total", 0)) - units)

            self.store.update(key, mutate)
            self._run_used[key] = max(0, self._run_used.get(key, 0) - units)

    # -- recorded results
    def cooldown(self, key: str, *, seconds: float | None = None, until: dt.datetime | None = None,
                 reason: str, status: str = COOLDOWN, error: str = "") -> str:
        """Start (or extend, never shorten) a cooldown; returns cooldown_until as ISO text."""
        self.touched[key] = None
        now = self.runtime.now()
        end = until if until is not None else now + dt.timedelta(seconds=seconds or 0)
        out: list[str] = []

        def mutate(entry: dict) -> None:
            current = parse_time(entry.get("cooldown_until"))
            final = max(end, current) if current and current > now else end
            entry.update(status=status, reason=sanitize(reason, 60), cooldown_until=iso(final), last_failure=iso(now),
                         last_failure_reason=sanitize(reason, 60), last_error=sanitize(error))
            out.append(iso(final))

        self.store.update(key, mutate)
        return out[0]

    def success(self, key: str) -> None:
        self.touched[key] = None
        now = self.runtime.now()

        def mutate(entry: dict) -> None:
            entry["last_success"] = iso(now)
            until = parse_time(entry.get("cooldown_until"))
            if until and until > now:  # a parallel request tripped a cooldown; one success does not lift it
                return
            entry.update(status=OK, reason="", cooldown_until="", transient=[])

        self.store.update(key, mutate)

    def failure(self, key: str, reason: str, error: str = "", status: str = DEGRADED) -> None:
        self.touched[key] = None
        now = self.runtime.now()

        def mutate(entry: dict) -> None:
            entry.update(last_failure=iso(now), last_failure_reason=sanitize(reason, 60), last_error=sanitize(error))
            until = parse_time(entry.get("cooldown_until"))
            if not (until and until > now):  # an active cooldown keeps its own status and reason
                entry.update(status=status, reason=sanitize(reason, 60))

        self.store.update(key, mutate)

    def transient(self, key: str, reason: str, error: str = "", window: float = 600, threshold: int = 3,
                  cooldown_seconds: float = 1800) -> str:
        """Record a transient failure; `threshold` of them within `window` seconds open the circuit. Returns
        cooldown_until when the circuit opened, else ""."""
        now = self.runtime.now()
        opened: list[bool] = []

        def mutate(entry: dict) -> None:
            recent = [t for t in entry.get("transient", []) if (parse_time(t) or now) > now - dt.timedelta(seconds=window)]
            recent.append(iso(now))
            entry["transient"] = recent[-threshold:]
            opened.append(len(recent) >= threshold)

        self.store.update(key, mutate)
        if opened[0]:
            return self.cooldown(key, seconds=cooldown_seconds, reason="circuit_open", error=error or reason)
        self.failure(key, reason, error)
        return ""

    def finish_run(self, key: str, degraded: bool) -> int:
        """Count consecutive degraded runs (reset by a clean run); returns the new count."""
        self.touched[key] = None
        count: list[int] = []

        def mutate(entry: dict) -> None:
            entry["consecutive_degraded_runs"] = int(entry.get("consecutive_degraded_runs", 0)) + 1 if degraded else 0
            count.append(entry["consecutive_degraded_runs"])

        self.store.update(key, mutate)
        return count[0]

    # -- reporting
    def summary(self, key: str) -> dict:
        entry, now = self.store.get(key), self.runtime.now()
        until = parse_time(entry.get("cooldown_until"))
        active = bool(until and until > now)
        status = entry.get("status")
        if active:
            state = FAILED if status == FAILED else COOLDOWN
        elif status in (DEGRADED, FAILED, SKIPPED):
            state = status
        else:
            state = OK
        out = {"state": state, "reason": entry.get("reason") if state != OK else "",
               "consecutive_degraded_runs": int(entry.get("consecutive_degraded_runs", 0))}
        if active:
            out["cooldown_until"] = iso(until)
        return out

    def summaries(self) -> dict:
        return {key: self.summary(key) for key in self.touched}


BRIGHTDATA_KEY = "brightdata"
BRIGHTDATA_RATE_LIMIT_COOLDOWN = 15 * 60   # a 429 without Retry-After
BRIGHTDATA_LONG_COOLDOWN = 24 * 3600       # quota / billing (402) and auth (401, 403)


def apply_brightdata_failure(gate: Gate, failure: Failure, error: str = "") -> bool:
    """Start the Bright Data cooldown a failure calls for; False when it needs none (transient, other)."""
    if failure.kind == "rate_limited":
        seconds = failure.retry_after if failure.retry_after is not None else BRIGHTDATA_RATE_LIMIT_COOLDOWN
        gate.cooldown(BRIGHTDATA_KEY, seconds=seconds, reason="http_429", error=error)
    elif failure.kind == "quota":
        gate.cooldown(BRIGHTDATA_KEY, seconds=BRIGHTDATA_LONG_COOLDOWN, reason="quota_exceeded", error=error)
    elif failure.kind == "auth":  # a configuration problem (token), so the state is "failed", not just paused
        gate.cooldown(BRIGHTDATA_KEY, seconds=BRIGHTDATA_LONG_COOLDOWN, reason="auth_failed", status=FAILED, error=error)
    else:
        return False
    return True


def finish_shared_runs() -> dict:
    """End of a process: count a degraded / clean run for every shared provider used, return their summaries."""
    out: dict = {}
    with _GATES_LOCK:
        gates = list(_GATES.values())
    for gate in gates:
        for key in list(gate.touched):
            out[key] = gate.summary(key)
            out[key]["consecutive_degraded_runs"] = gate.finish_run(key, out[key]["state"] != OK)
    return out


def overall_state(providers: dict) -> str:
    states = {p.get("state") for p in providers.values()}
    if FAILED in states:
        return FAILED
    return DEGRADED if states - {OK} else OK


def status_line(providers: dict) -> str:
    """The single stdout line a pipeline step prints so pull-jobs.ps1 can record provider health."""
    return STATUS_PREFIX + json.dumps({"state": overall_state(providers), "providers": providers}, sort_keys=True)


# --- shared gates --------------------------------------------------------------------------

_ROOT: Path | None = None
_GATES: dict[tuple[str, str], Gate] = {}
_GATES_LOCK = threading.Lock()


def set_root(root: Path | str | None) -> None:
    """Point the shared gates at a Job Search root (the dashboard's --root)."""
    global _ROOT
    _ROOT = Path(root) if root else None


def get_gate(policy: Policy) -> Gate:
    """The process-wide Gate for a policy, so every module shares one limiter and one run budget."""
    root = _ROOT or default_root()
    with _GATES_LOCK:
        key = (policy.name, str(root))
        if key not in _GATES:
            _GATES[key] = Gate(policy, StateStore(root / STATE_PATH))
        return _GATES[key]


def brightdata_gate() -> Gate:
    return get_gate(BRIGHTDATA)
