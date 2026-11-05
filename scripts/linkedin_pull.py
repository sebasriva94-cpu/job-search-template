"""Multi-source JobSpy/API discovery merged into the canonical daily CSV.

Sources: LinkedIn + Indeed via JobSpy (Raleigh-Durham from Raleigh within JobSpy's default
50 miles, and remote-US, one query per title term), plus the RemoteOK and Remotive APIs. Glassdoor, Google Jobs and ZipRecruiter were removed
on 2024-10-29: every daily run for weeks returned 0 rows from them (Glassdoor 400 "location
not parsed", ZipRecruiter 403, Google 0), costing time and log noise for nothing.

LinkedIn rate control (2024-12-10): JobSpy swallows a LinkedIn 429, CAPTCHA or sign-in wall, logs it
and returns an empty result, so a throttled search used to look like a clean "0 jobs". The LinkedIn
work now runs serially, at most LINKEDIN_DAILY_GROUPS query groups a day (every group, rotating
when a run is cut short), 60-120 s apart, up to LINKEDIN_MAX_RESULTS results per group at JobSpy's own
3-7 s page pacing, and the JobSpy log is read after every group: a challenge or auth wall stops
the remaining LinkedIn groups and persists a cooldown (scripts/rate_control.py,
dashboard/provider-state.json) that later runs honour without any call. A run that only honours an
existing pause is not counted as another degraded run; only a run that trips a new pause is.
Indeed and the APIs keep running. No proxies, no workarounds.

429 (2024-12-13): LinkedIn's 429 is a burst throttle of about ten pages that clears in about a
minute. Three nights of logs showed every run pulling jobs first and then losing the whole night,
because one 429 rolled LinkedIn to the next 00:30 window; 16 groups of ten pages was the cause. Now a
group asks for 25 results (three pages), groups are 60-120 s apart, and the first 429 of a run pauses
LINKEDIN_429_PAUSE then retries that group once. Only a second 429 in the same run persists the
next-window cooldown.

Probe log (2024-12-17): the 429s of 10/17 and 10/18 came on the first page of the night, which no
burst explains, so every LinkedIn attempt now appends one JSON line to
automation-logs/linkedin-probe-YYYY-MM.jsonl: when, which group, outcome, rows, seconds since the
previous attempt and since the last 429 (the last 429 is remembered across runs in the provider
state). No URLs and no page content. Ten nights of it decide the next pacing change.

Two lanes (2024-12-17): `--no-linkedin` is the fast lane the 00:30 chain runs (Indeed and the APIs,
done in minutes); `--trickle` is the LinkedIn lane the 01:00 task runs: the same 16 groups of three
pages, one group every TRICKLE_GROUP_GAP, spread over the night, a 429 pausing 15, 30 then 60 minutes
and deferring the group instead of ending the night, and for rows that pass the title, place and pay
gates the posting text fetched from the public job page (linkedin_detail.py) at DETAIL_GAP pacing.
Rows merge into the day's CSV as each group finishes; the 07:30 late phase judges and renders them.
"""
from __future__ import annotations

import datetime as dt
import contextlib
import json
import logging
import math
import re
import sys
import urllib.parse
import urllib.request

try:
    from scripts import judge_store, linkedin_detail, rate_control
    from scripts.sweep_common import (CSV_COLUMNS, clean_description, daily_csv, merge_rows, normalize_company, normalize_title,
                                      pay_interval, remote_location, yearly_factor,
                                      project_root, relevant, role_key)
except ModuleNotFoundError:
    import judge_store
    import linkedin_detail
    import rate_control
    from sweep_common import (CSV_COLUMNS, clean_description, daily_csv, merge_rows, normalize_company, normalize_title,
                              pay_interval, remote_location, yearly_factor,
                              project_root, relevant, role_key)

TERMS = [
    "IT Support Engineer", "IT Support Specialist", "IT Support Analyst",
    "Technical Support Engineer", "Service Desk Analyst",
    "Systems Administrator", "Desktop Support", "IT Specialist",
    "Endpoint Engineer", "IAM Analyst", "Information Technology Specialist",
]
# 2024-12-10 measurement (all 22 LinkedIn sections live, 72 h window): the 50-mile Raleigh
# market held 30 relevant roles and these five terms found every one of them; the other six found
# nothing the five had not, at about 2.5 min and 70 off-target rows a run. Remote keeps every term:
# there the overlap was small and each term found 7-39 relevant roles no other term did.
LOCAL_TERMS = [
    "IT Specialist", "Systems Administrator", "IT Support Engineer",
    "Technical Support Engineer", "Endpoint Engineer",
]
# Greater Minneapolis counts at any office pattern: 21 groups, so with the guest lane's
# 16-group daily budget the rotation cursor spreads them over two nights; no budget was raised.
LOCATIONS = (("Raleigh, NC", False), ("Minneapolis, MN", False), ("United States", True))
INDEED_WANTED = 25
RECENT_DAYS = 3  # the 72-hour window
LINKEDIN_KEY = "linkedin"
LINKEDIN_LOGGER = "JobSpy:LinkedIn"
LINKEDIN_DAILY_GROUPS = rate_control.LINKEDIN.daily_budget   # query groups per day; the rest wait their turn
LINKEDIN_GROUP_GAP = (60, 120)   # seconds between query groups (jittered); JobSpy already sleeps 3-7 s per page
LINKEDIN_MAX_RESULTS = 25        # three 10-job pages (2024-12-13): 16 groups of ten pages tripped the burst
                                 # throttle every night; the 72-hour window rarely holds more than 25 per term
LINKEDIN_429_PAUSE = (600, 900)  # the first 429 of a run: wait 10-15 min, retry the group once, carry on
COOLDOWN_429_MIN_HOURS = 12      # a second 429 in the run: on to the next nightly window
TRICKLE_GROUP_GAP = (720, 1200)  # --trickle: 12-20 min between groups, the night's 16 groups spread over 4-5 hours
TRICKLE_429_PAUSES = (900, 1800, 3600)  # --trickle: the n-th 429 of the run pauses 15, 30, then 60 min; never the night
TRICKLE_BUDGET = 290 * 60  # --trickle: no group starts after 4 h 50 min; the rest is deferred (the step's cap is 300 min; three 429 pauses plus 15 gaps of 12 min need 285)
TRICKLE_GROUP_RESERVE = 5 * 60  # what a group needs once started (three pages, up to 25 detail fetches), kept inside the budget
NIGHT_WINDOW = (0, 30)           # the 00:30 task
COOLDOWN_CHALLENGE_HOURS = 24
COOLDOWN_TRANSIENT_HOURS = 6
COVERAGE_MIN_GROUPS = 3
COVERAGE_EMPTY_RATIO = 0.75      # this run: at least this share of groups came back empty ...
COVERAGE_BASELINE_RATIO = 0.5    # ... while recent clean runs were below this share
PROBE_FILE = "linkedin-probe-{month}.jsonl"   # under automation-logs; one JSON line per LinkedIn attempt


def search_settings(site, remote, wanted):
    """(results_wanted, hours_old) for one JobSpy search.

    JobSpy 1.1.82 builds Indeed filters as if/elif, so hours_old silently drops the remote
    filter. The remote Indeed search therefore runs without the window, takes one full
    100-row page (Indeed sorts by relevance) and keeps the last RECENT_DAYS days locally.
    """
    if site == "indeed" and remote:
        return min(wanted, 100), None
    return (wanted if site == "linkedin" else min(wanted, INDEED_WANTED)), 72


def mark_remote(rows):
    """Rows from a remote search were picked by the board's own remote filter (LinkedIn f_WT=2,
    Indeed DSQF7) but usually carry a city. The CSV has no work-setting column and the importer and
    dashboard read only the location, so say it there: "Birmingham, AL" -> "Remote (Birmingham, AL)".
    """
    for row in rows:
        row["is_remote"] = True
        row["location"] = remote_location(row.get("location"))
    return rows


def keep_pay_interval(rows):
    """Yearly pay amounts plus the interval the board published them in.

    JobSpy's enforce_annual_salary converts hourly pay to yearly and then overwrites the interval
    with "yearly" (convert_to_annual), so an hourly staffing-agency job looked salaried. The pull
    asks for the published figures instead, converts them with the same factors and keeps the
    published interval in pay_interval (the owner's agency rule, 2024-12-09).
    """
    for row in rows:
        interval = pay_interval(row.get("interval"))
        factor = yearly_factor(interval) if interval else None
        row["pay_interval"] = interval
        if factor in (None, 1):
            continue
        for column in ("min_amount", "max_amount"):
            try:
                amount = float(row.get(column))
            except (TypeError, ValueError):
                continue
            if not math.isnan(amount):
                row[column] = amount * factor
    return rows


def posted_within(value, days=RECENT_DAYS, today=None):
    """True when a JobSpy date_posted (a date or ISO text) falls within the last `days` days."""
    today = today or dt.date.today()
    try:
        posted = dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return False
    return (today - posted).days <= days


def api_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def title_ok(title):
    """Shared title vocabulary (sweep_common.relevant): one list for every stage."""
    return relevant(title)


class SectionSkipped(Exception):
    """A section that was deliberately not run (cooldown, budget, rotation); it is neither a success nor a failure."""

    def __init__(self, reason, quiet=False):
        super().__init__(reason)
        self.quiet = quiet


class ProviderThrottled(rate_control.ControlError):
    """LinkedIn refused or challenged the search; the empty result is not a real "0 jobs"."""


@contextlib.contextmanager
def capture_jobspy_errors(name=LINKEDIN_LOGGER):
    """The error lines JobSpy logs (and then swallows) while the block runs."""
    messages = []

    class Collect(logging.Handler):
        def emit(self, record):
            # The raw text: it is only classified, never stored or printed. JobSpy's real 429 message is
            # about 300 characters with the "429" at the end, so a shortened copy read as transient.
            messages.append(record.getMessage())

    handler, logger = Collect(logging.WARNING), logging.getLogger(name)
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


def classify_jobspy_messages(messages):
    """The Failure behind JobSpy's logged LinkedIn errors, or None when it logged none."""
    text = " ".join(messages).lower()
    if not text.strip():
        return None
    if re.search(r"\b429\b|too many requests|rate.?limit", text):
        return rate_control.Failure("rate_limited", "http_429", 429)
    if re.search(r"captcha|authwall|auth wall|challenge|sign.?in|log.?in|\b(?:401|403|999)\b", text):
        return rate_control.Failure("auth", "challenge_or_authwall")
    return rate_control.Failure("transient", "transient_errors")


def next_window(now, min_hours, window=NIGHT_WINDOW):
    """The first nightly pull window (default 00:30 local) at least `min_hours` from now."""
    earliest = now + dt.timedelta(hours=min_hours)
    start = earliest.replace(hour=window[0], minute=window[1], second=0, microsecond=0)
    return start if start >= earliest else start + dt.timedelta(days=1)


def coverage_warning(history, groups, empty):
    """True when this run's empty share is abnormally broad next to the recent clean runs."""
    if groups < COVERAGE_MIN_GROUPS or empty / groups < COVERAGE_EMPTY_RATIO:
        return False
    past = [h["empty"] / h["groups"] for h in history if h.get("groups", 0) >= COVERAGE_MIN_GROUPS]
    return len(past) >= 2 and sum(past) / len(past) < COVERAGE_BASELINE_RATIO


def csv_role_keys(path):
    """Role keys already in the day's CSV (empty when there is none yet)."""
    import csv
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return {role_key(row) for row in csv.DictReader(handle)}
    except (OSError, ValueError):
        return set()


def probe_write(record, root=None):
    """Append one probe line (automation-logs/linkedin-probe-YYYY-MM.jsonl). Never raises: the pull
    must not fail because the log could not be written."""
    try:
        folder = (root or project_root()) / "automation-logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / PROBE_FILE.format(month=str(record.get("ts", ""))[:7] or "unknown")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


class LinkedInRun:
    """One run's LinkedIn work: rotation, pacing, throttle detection and the persisted cooldown."""

    def __init__(self, gate, groups, trickle=False, fetch_detail=None):
        self.gate, self.runtime, self.groups = gate, gate.runtime, list(groups)
        self.trickle, self.fetch_detail = trickle, fetch_detail
        self.pauses = 0               # --trickle: 429 pauses taken this run (indexes TRICKLE_429_PAUSES)
        self.detail_blocked = ""      # --trickle: why detail fetches stopped for the run ('' while they run)
        self.detail_paused = False    # --trickle: a 429 on a job page pauses the fetches for the rest of this group
        self.detail_429s = 0
        self.detail_done = 0
        # Role keys that already have their text or were tried: the day's CSV (the merge keeps the first row per
        # key, so a second fetch for the same key lands nowhere) plus this run's. 2024-12-18: 30 of 47 fetches
        # were repeats of a posting the five local terms had all returned.
        self.detail_keys = csv_role_keys(daily_csv(project_root())) if trickle else set()
        self.last_detail_end = None
        self.stop = None          # (reason, cooldown_until) once the rest of the LinkedIn work must not run
        self.cooled = False       # a cooldown, not just a budget, ended the work
        self.honoured = False     # ... and it was already in force before this run (nothing was tried)
        self.done, self.empty, self.attempted, self.partial = set(), 0, 0, False
        self.partial_reason = ""  # why the run is degraded although LinkedIn work went on
        self.paused = False       # the run's one in-run 429 pause has been spent
        self.last_end, self.announced = None, False
        self.current, self.last_attempt_end, self.retrying = None, None, False  # probe bookkeeping
        self.selected, self.start = [], 0
        # --trickle (2024-12-18): the lane keeps its own clock. On its first night it ran into the step's cap at
        # 06:00 during a 60-minute 429 pause and the night read "failed" with 13 groups landed.
        self.deadline = self.runtime.monotonic() + TRICKLE_BUDGET if trickle else None
        self.deferred = 0             # --trickle: groups left for the next window because the budget ran out
        self.budget_announced = False
        blocked = gate.blocked(LINKEDIN_KEY)
        if blocked:
            self.stop, self.cooled, self.honoured = (blocked.reason, blocked.cooldown_until), True, True
            return
        entry, total = gate.store.get(LINKEDIN_KEY), max(len(self.groups), 1)
        counters = entry["requests"]
        used = counters.get("today", 0) if counters.get("date") == self.runtime.now().date().isoformat() else 0
        allowed = max(0, min(len(self.groups), (gate.policy.daily_budget or len(self.groups)) - used))
        self.start = int(entry.get("rotation_cursor", 0)) % total
        self.selected = [(self.start + i) % total for i in range(allowed)]
        if not self.selected and self.groups:
            self.stop = ("daily_budget", "")

    def _pace_wait(self):
        if self.last_end is None:
            return 0
        gap = TRICKLE_GROUP_GAP if self.trickle else LINKEDIN_GROUP_GAP
        return max(0, self.runtime.uniform(*gap) - (self.runtime.monotonic() - self.last_end))

    def _defer_if_over_budget(self, wait):
        """--trickle: a group whose gap or 429 pause plus its own work would cross the time budget is deferred
        to the next window (the rotation cursor stops at the first group not run, so tomorrow starts there)."""
        if self.deadline is None or self.runtime.monotonic() + wait + TRICKLE_GROUP_RESERVE <= self.deadline:
            return
        self.deferred += 1
        self.partial, self.partial_reason = True, self.partial_reason or "time_budget"
        quiet, self.budget_announced = self.budget_announced, True
        raise SectionSkipped("LinkedIn time budget spent; the remaining groups are deferred to the next window", quiet)

    def _advance_cursor(self):
        """Move the cursor to the first group not run this time. When every group ran (a budget of
        at least len(groups)) the cursor wraps to where this run started, so the next rotation is
        complete too; the bounded walk is what keeps that case from looping forever."""
        total = len(self.groups)
        position = self.start
        for _ in range(total):
            if position % total not in self.done:
                break
            position += 1
        self.gate.store.update(LINKEDIN_KEY, lambda e: e.update(rotation_cursor=position % total))

    def _attempt(self, scrape):
        with capture_jobspy_errors() as messages:
            try:
                rows = scrape() or []
            except rate_control.ControlError:
                raise
            except Exception as exc:  # noqa: BLE001
                failure = classify_jobspy_messages(messages) or rate_control.classify_exception(exc, self.runtime.now())
                if failure.kind == "other":
                    self._probe([], rate_control.Failure("other", type(exc).__name__))
                    raise  # an ordinary section failure, as before
                self._probe([], failure)
                return [], failure
        failure = classify_jobspy_messages(messages)
        self._probe(rows, failure)
        return rows, failure

    def _probe(self, rows, failure, kind="search", outcome=None):
        """One probe line per LinkedIn request: outcome and timing only, never a URL or page text.
        kind is 'search' (a JobSpy group, up to three pages) or 'detail' (one public job page)."""
        now, mono = self.runtime.now(), self.runtime.monotonic()
        entry = self.gate.store.get(LINKEDIN_KEY)
        last_429 = rate_control.parse_time(entry.get("last_429"))
        outcome = outcome or (failure.kind if failure is not None else "ok")
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "kind": kind,
            "lane": "trickle" if self.trickle else "night",
            "group": self.current,
            "outcome": outcome,
            "reason": failure.reason if failure is not None else "",
            "rows": len(rows),
            "pages_est": (math.ceil(len(rows) / 10) if rows else 0) if kind == "search" else 1,
            "retry_after_pause": self.retrying,
            "since_prev_attempt_s": round(mono - self.last_attempt_end) if self.last_attempt_end is not None else None,
            "since_last_429_s": round((now - last_429).total_seconds()) if last_429 else None,
        }
        self.last_attempt_end = mono
        if outcome == "rate_limited":
            stamp = rate_control.iso(now)
            self.gate.store.update(LINKEDIN_KEY, lambda e: e.update(last_429=stamp))
        probe_write(record)

    def enrich(self, rows):
        """--trickle: fetch the posting text from the public job page for rows that pass the title, place
        and pay gates and have none, DETAIL_GAP apart. A 429 or a sign-in wall stops the detail fetches
        for the rest of the run (the judge's Bright Data fallback still exists); other misses are skipped."""
        if not self.trickle or self.fetch_detail is None:
            return rows
        self.detail_paused = False  # a new group: a 429 pause on the job pages ends here
        for row in rows:
            if self.deadline is not None and self.runtime.monotonic() > self.deadline:
                break  # the budget is spent: the judge's Bright Data fallback reads the rest
            if self.detail_blocked or self.detail_paused:
                break
            key = role_key(row)
            if row.get("description") or key in self.detail_keys:
                self.detail_keys.add(key)
                continue
            checks = judge_store.gate(row)
            if not judge_store.passes_gate(checks):
                continue
            url = linkedin_detail.view_url(row.get("job_url", ""))
            if not url:
                continue
            self.detail_keys.add(key)
            if self.last_detail_end is not None:
                wait = self.runtime.uniform(*linkedin_detail.DETAIL_GAP) - (self.runtime.monotonic() - self.last_detail_end)
                if wait > 0:
                    self.runtime.sleep(wait)
            page = self.fetch_detail(url)
            self.last_detail_end = self.runtime.monotonic()
            outcome = linkedin_detail.classify(page)
            self._probe([row] if outcome == "ok" else [], None, kind="detail", outcome=outcome)
            if outcome == "ok":
                row["description"] = clean_description(linkedin_detail.description_from_html(page.get("html") or ""))
                self.detail_done += 1
            elif outcome == "rate_limited":
                # The first 429 on a job page pauses the fetches until the next group (12 to 20 minutes away);
                # a second one in the same run stops them for the night. The judge's Bright Data fallback remains.
                self.detail_429s += 1
                self.partial, self.partial_reason = True, f"detail_{outcome}"
                if self.detail_429s == 1:
                    self.detail_paused = True
                else:
                    self.detail_blocked = outcome
            elif outcome == "auth":
                self.detail_blocked = outcome
                self.partial, self.partial_reason = True, f"detail_{outcome}"
        return rows

    def _fetch(self, scrape):
        """One group's scrape with a single retry for a timeout or 5xx; returns (rows, failure)."""
        rows, failure = [], None
        for attempt in (1, 2):
            rows, failure = self._attempt(scrape)
            if failure is None or failure.kind != "transient" or rows or attempt == 2:
                break
            self.runtime.sleep(self.runtime.uniform(*LINKEDIN_GROUP_GAP))
        return rows, failure

    def run(self, index, scrape):
        """Rows of one query group. Raises SectionSkipped (not run) or ProviderThrottled (refused, nothing usable)."""
        if self.stop:
            quiet, self.announced = self.announced, True  # say it once, not for every skipped group
            raise SectionSkipped(f"LinkedIn {self.stop[0]}" + (f" until {self.stop[1]}" if self.stop[1] else ""), quiet)
        if index not in self.selected:
            raise SectionSkipped("not scheduled today", quiet=True)
        wait = self._pace_wait()
        self._defer_if_over_budget(wait)  # before admit, so a deferred group costs no daily-budget unit
        admitted = self.gate.admit(LINKEDIN_KEY)
        if not admitted.ok:
            self.stop = (admitted.reason, admitted.cooldown_until)
            raise SectionSkipped(f"LinkedIn {admitted.reason}")
        if wait > 0:
            self.runtime.sleep(wait)
        self.current, self.retrying = index, False
        rows, failure = self._fetch(scrape)
        if self.trickle and failure is not None and failure.kind == "rate_limited":
            # The trickle lane never ends the night on a 429: pause 15, 30 then 60 minutes, retry the group
            # once, and when it is still refused leave it for the next window and carry on with the rest.
            self.partial, self.partial_reason = True, "http_429_paused"
            self.gate.failure(LINKEDIN_KEY, self.partial_reason, status=rate_control.DEGRADED)
            pause = TRICKLE_429_PAUSES[min(self.pauses, len(TRICKLE_429_PAUSES) - 1)]
            self.last_end = self.runtime.monotonic()
            self._defer_if_over_budget(pause)  # a pause the budget cannot hold defers the group instead
            self.runtime.sleep(pause)
            self.pauses += 1
            self.retrying = True
            retried, failure = self._fetch(scrape)
            rows = retried or rows
            self.last_end = self.runtime.monotonic()
            if failure is not None and failure.kind == "rate_limited":
                raise SectionSkipped("LinkedIn 429 after the pause; group deferred to the next window")
        elif failure is not None and failure.kind == "rate_limited" and not self.paused:
            # The burst throttle clears in about a minute: pause once, retry this group, carry on.
            self.paused, self.partial, self.partial_reason = True, True, "http_429_paused"
            self.gate.failure(LINKEDIN_KEY, self.partial_reason, status=rate_control.DEGRADED)
            self.runtime.sleep(self.runtime.uniform(*LINKEDIN_429_PAUSE))
            self.retrying = True
            retried, failure = self._fetch(scrape)
            rows = retried or rows
        self.last_end = self.runtime.monotonic()
        if failure is None or (failure.kind == "transient" and rows):
            if failure is not None:  # useful rows, but the page fetch was flaky
                self.partial, self.partial_reason = True, failure.reason
                self.gate.failure(LINKEDIN_KEY, failure.reason, status=rate_control.DEGRADED)
            else:
                self.gate.success(LINKEDIN_KEY)
            self.done.add(index)
            self.attempted += 1
            self.empty += 0 if rows else 1
            self._advance_cursor()
            return self.enrich(rows)
        now = self.runtime.now()
        if failure.kind == "rate_limited":
            until = next_window(now, COOLDOWN_429_MIN_HOURS)
            if failure.retry_after:
                until = max(until, now + dt.timedelta(seconds=failure.retry_after))
            reason = "http_429"
        elif failure.kind in ("auth", "quota"):
            until, reason = now + dt.timedelta(hours=COOLDOWN_CHALLENGE_HOURS), "challenge_or_authwall"
        else:
            until, reason = now + dt.timedelta(hours=COOLDOWN_TRANSIENT_HOURS), "transient_errors"
        stamp = self.gate.cooldown(LINKEDIN_KEY, until=until, reason=reason)
        self.stop, self.cooled = (reason, stamp), True
        if rows:  # keep what the pages before the refusal returned
            return rows
        raise ProviderThrottled(reason, rate_control.FAILED, stamp)

    def finish(self):
        """Record the run's outcome and return {"linkedin": summary} for the pipeline status."""
        gate = self.gate
        if self.cooled and self.honoured:
            pass  # an existing pause was honoured: the count stays as the run that tripped it left it
        elif self.cooled:
            gate.finish_run(LINKEDIN_KEY, True)
        elif self.stop and not self.attempted:  # daily budget spent: intentional, not a degradation
            gate.failure(LINKEDIN_KEY, self.stop[0], status=rate_control.SKIPPED)
        elif self.attempted:
            degraded = self.partial
            if degraded:  # later clean groups reset the status; the run as a whole was not clean
                gate.failure(LINKEDIN_KEY, self.partial_reason or "partial", status=rate_control.DEGRADED)
            else:
                today = self.runtime.now().date().isoformat()
                history = gate.store.get(LINKEDIN_KEY).get("history", [])
                if coverage_warning(history, self.attempted, self.empty):
                    gate.failure(LINKEDIN_KEY, "coverage_warning", status=rate_control.DEGRADED)
                    degraded = True
                else:
                    record = {"date": today, "groups": self.attempted, "empty": self.empty}
                    gate.store.update(LINKEDIN_KEY, lambda e: e.update(
                        history=(e.get("history", []) + [record])[-rate_control.HISTORY_LIMIT:]))
            gate.finish_run(LINKEDIN_KEY, degraded)
        gate.touched[LINKEDIN_KEY] = None
        return {LINKEDIN_KEY: gate.summary(LINKEDIN_KEY)}


def run_sections(sections, save=None):
    """Run every section; `save(rows)` is called after each one that returned rows, so a run that
    is killed by the step timeout keeps what the sections before it found (2024-12-10: three
    timeouts at the 20-min cap lost all 46 sections each time)."""
    rows = []
    attempted = succeeded = 0
    for section in sections:
        attempted += 1
        try:
            found = section() or []
            if save and found:
                save(found)
            rows.extend(found)
            succeeded += 1
        except SectionSkipped as exc:  # not run on purpose: neither a success nor a failure
            attempted -= 1
            if not exc.quiet:
                print(f"SKIPPED - {exc}")
        except Exception as exc:
            print(f"FAILED - {type(exc).__name__}: {rate_control.sanitize(exc)}")
    return rows, attempted, succeeded


def dedupe_rows(rows):
    seen = set()
    output = []
    for row in rows:
        key = (normalize_company(row.get("company")), normalize_title(row.get("title")))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        output.append({column: row.get(column, "") for column in CSV_COLUMNS})
    return output


def main(argv=None, *, scrape=None, gate=None, fetch_detail=None):
    argv = sys.argv[1:] if argv is None else argv
    no_linkedin, trickle = "--no-linkedin" in argv, "--trickle" in argv
    argv = [a for a in argv if not a.startswith("--")]
    # 2024-11-06: LinkedIn sections were capped at 25 and every one returned exactly 25 (the ceiling),
    # so the remote "United States" queries dropped most of the market. LinkedIn now defaults to 100;
    # Indeed keeps 25 (it rarely reaches the cap). LinkedIn alone is capped at LINKEDIN_MAX_RESULTS
    # per query group since 2024-12-10 (rate control, see the module docstring).
    wanted = int(argv[0]) if argv and argv[0].isdigit() else 100
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    gate = gate or rate_control.get_gate(rate_control.LINKEDIN)
    runtime = gate.runtime
    groups = [(location, remote, term) for location, remote in LOCATIONS for term in (TERMS if remote else LOCAL_TERMS)]
    linkedin = None if no_linkedin else LinkedInRun(gate, groups, trickle=trickle,
                                                    fetch_detail=(fetch_detail or linkedin_detail.fetch_page) if trickle else None)
    sites = ("linkedin",) if trickle else (("indeed",) if no_linkedin else ("linkedin", "indeed"))

    sections = []

    def jobspy_section(site, term, location, remote, group=None, **extra):
        def fetch():
            results_wanted, hours_old = search_settings(site, remote, wanted)
            if site == "linkedin":
                results_wanted = min(results_wanted, LINKEDIN_MAX_RESULTS)
            frame = scrape(
                site_name=[site], search_term=term, location=location, is_remote=remote,
                results_wanted=results_wanted, hours_old=hours_old, country_indeed="USA",
                enforce_annual_salary=False, fetch_description=False, description_format="plain", **extra,
            )
            rows = keep_pay_interval(frame.reindex(columns=CSV_COLUMNS + ["interval"], fill_value="").to_dict("records"))
            if hours_old is None:
                rows = [row for row in rows if posted_within(row.get("date_posted"))]
            if remote:
                rows = mark_remote(rows)
            print(f"{location} / {site} / {term}: {len(rows)} jobs")
            return rows

        def execute():
            if site == "linkedin":  # serial, rotated, paced; a refusal is never an empty result
                return linkedin.run(group, fetch)
            rows = fetch()
            runtime.sleep(2)
            return rows
        return execute

    index = 0
    for location, remote in LOCATIONS:
        for term in (TERMS if remote else LOCAL_TERMS):
            for site in sites:
                sections.append(jobspy_section(site, term, location, remote, group=index if site == "linkedin" else None))
            index += 1

    def remoteok():
        rows = [{
            "title": job.get("position"), "company": job.get("company"),
            "location": remote_location(job.get("location")), "date_posted": str(job.get("date") or "")[:10],
            "min_amount": job.get("salary_min") or "", "max_amount": job.get("salary_max") or "",
            "is_remote": True, "job_url": job.get("url"), "site": "remoteok",
        } for job in api_json("https://remoteok.com/api")[1:]
            if isinstance(job, dict) and title_ok(job.get("position"))]
        print(f"remoteok: {len(rows)} support-titled")
        return rows

    def remotive():
        jobs = api_json("https://remotive.com/api/remote-jobs?search=" + urllib.parse.quote("IT support"))["jobs"]
        rows = [{
            "title": job.get("title"), "company": job.get("company_name"),
            "location": remote_location(job.get("candidate_required_location")),   # "Worldwide" and "USA" say Remote (2024-12-20)
            "date_posted": str(job.get("publication_date") or "")[:10],
            "min_amount": "", "max_amount": "", "is_remote": True,
            "job_url": job.get("url"), "site": "remotive",
        } for job in jobs if title_ok(job.get("title"))]
        print(f"remotive: {len(rows)} support-titled")
        return rows

    if not trickle:
        sections.extend([remoteok, remotive])
    merged = {"added": 0, "path": ""}

    def save(found):
        result = merge_rows(dedupe_rows(found), project_root())
        merged["added"] += result["added"]
        merged["path"] = result["path"]

    rows, attempted, succeeded = run_sections(sections, save)
    rows = dedupe_rows(rows)
    if rows:
        print(f"\nTotal unique: {len(rows)}; merged {merged['added']} -> {merged['path']}")
    else:
        print("No jobs returned from successful sections")
    print(f"Sections successful: {succeeded}/{attempted}")
    if linkedin is None:
        print("LinkedIn: not in this lane (the trickle task pulls it through the night)")
        print(rate_control.status_line({}))
        return 0 if succeeded else 1
    providers = linkedin.finish()
    state = providers[LINKEDIN_KEY]
    if trickle:
        print(f"LinkedIn trickle: {linkedin.attempted} groups, {linkedin.pauses} pause(s) on 429, "
              f"{linkedin.detail_done} posting text(s) fetched"
              + (f", detail fetches stopped: {linkedin.detail_blocked}" if linkedin.detail_blocked
                 else (", detail fetches paused once on a 429" if linkedin.detail_429s else ""))
              + (f", {linkedin.deferred} group(s) deferred on the time budget" if linkedin.deferred else ""))
    if state["state"] != rate_control.OK:
        print(f"LinkedIn {state['state']}: {state['reason']}"
              + (f" until {state['cooldown_until']}" if state.get("cooldown_until") else "")
              + " - the run is degraded, not clean.")
    print(rate_control.status_line(providers))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
