"""Build the candidate's one-file job dashboard.

Inputs:
- dashboard/job-dashboard-data.json: human/agent-validated apply + skip records.
- weekly-sweeps/linkedin-raw-YYYY-MM-DD.csv: latest raw discovery feed.
- dashboard/apply-liveness.json: per-URL verdicts written by scripts/check_apply_liveness.py.
- dashboard/queue-seen.json: memory of which raw candidates were shown when (auto-maintained).
- automation-logs/pipeline-status.json: step results of the morning chain (pull-jobs.ps1).

Outputs, always overwritten in the project root:
- job-dashboard.html: durable browser dashboard.
- job-dashboard.md: same facts in chat-friendly Markdown.

The script never invents fit validation. Curated roles can be marked Apply/Skip only after
manual/agent verification against employer systems. Raw feed rows not in curated data are
shown as "Needs validation"; the optional queue check only proves a listing is still up.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import html
import json
import os
import pathlib
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable

try:
    from scripts import check_apply_liveness as liveness
    from scripts import employer_hq
    from scripts import rate_control
    from scripts import validate_listing
    from scripts.sweep_common import in_area, is_home_region, normalize_company, normalize_title, project_root, relevant
except ModuleNotFoundError:  # Direct execution: scripts/ is sys.path[0].
    import check_apply_liveness as liveness
    import employer_hq
    import rate_control
    import validate_listing
    from sweep_common import in_area, is_home_region, normalize_company, normalize_title, project_root, relevant

ROOT = project_root()
DATA_PATH = pathlib.Path("dashboard/job-dashboard-data.json")
LIVENESS_PATH = pathlib.Path("dashboard/apply-liveness.json")
SEEN_PATH = pathlib.Path("dashboard/queue-seen.json")
STATUS_PATH = pathlib.Path("automation-logs/pipeline-status.json")
HTML_PATH = pathlib.Path("job-dashboard.html")
MD_PATH = pathlib.Path("job-dashboard.md")
STALE_AFTER_DAYS = 14      # a curated Apply role validated longer ago than this reads "stale"
QUEUE_MAX_DAYS_SHOWN = 7   # a raw candidate shown longer than this is auto-dropped
QUEUE_CHECK_BUDGET_S = 6 * 60  # listing checks still running after this are left for the next run (the step's cap is 10 min)
MAX_POSTING_AGE_DAYS = 21  # the owner's rule (2024-12-09): only listings posted less than 3 weeks ago
SEEN_PRUNE_DAYS = 30       # queue-memory entries not seen for this long are forgotten
RUN_OVERDUE_HOURS = 36     # no pipeline run for this long turns the banner red
NO_RUN_GRACE_HOUR = 3      # after this hour of the day, a status file from another date means last night did not run
QUEUE_LIMIT = 15
RENDER_DAYS = 2            # the queue ranks the pulls of today and yesterday (2024-12-23), see render_raw_csvs
# Reposts (2024-12-09): the boards never say "reposted" to a logged-out visitor, so the same job is found
# again in the owner's own daily pulls. Many jobs come back under a new link at the same place.
REPOST_WINDOW_DAYS = 28    # how far back the daily pulls are searched for earlier links to the same job
REPOST_GAP_DAYS = 3        # a new link first seen this many days after the job's first sighting is a repost
CHRONIC_REPOSTS = 3        # the owner's choice: a job reposted this often is shown under Review, never Apply now
# The owner's Applied / Later / Not for me clicks (2024-12-09), saved by scripts/decision_helper.py.
DECISIONS_PATH = pathlib.Path("dashboard/decisions.json")
DECISION_TOKEN_PATH = pathlib.Path("dashboard/.decision-token")
HELPER_PORT = 8765
DECISION_SHOW_DAYS = 30    # a Saved-for-later click stays visible this long; an Applied click is a record and stays (2024-12-19)
DECISION_LABELS = {"applied": "Applied", "later": "Later", "skip": "Not for me"}


DEFAULT_PROFILE = {
    "name": "Alex Example",
    "search": "Raleigh-Durham + Greater Minneapolis + Remote; remote roles need a North Carolina HQ or branch",
    "target": "IT Support / Sysadmin / Infrastructure",
    "salary_floor": "$70k–$150k target range",
    "toolset": "Jamf · Intune · Okta · CrowdStrike · M365",
}


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def today_label() -> str:
    # Windows-safe no-leading-zero day if strftime %-d is unavailable.
    now = dt.datetime.now()
    return f"{now.strftime('%a %b')} {now.day}, {now.year}"


def parse_date(value: object) -> dt.date | None:
    text = str(value or "").strip()[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def posted_recently(date_posted: object, today: dt.date | None) -> bool:
    """False only for a known posting date three weeks old or more. Unknown dates stay, for review.
    Without `today` there is no age check (direct callers); build_dashboard always passes it."""
    if today is None:
        return True
    posted = parse_date(date_posted)
    return posted is None or (today - posted).days < MAX_POSTING_AGE_DAYS


def parse_datetime(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # PowerShell's 7-digit fractions
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


PULL_CSV = re.compile(r"^linkedin-raw-(\d{4}-\d{2}-\d{2})\.csv$")


def pull_csvs(root: pathlib.Path) -> list[tuple[dt.date, pathlib.Path]]:
    """The daily pulls, oldest first: exactly linkedin-raw-YYYY-MM-DD.csv.

    Anything else in weekly-sweeps is not a pull: the _filtered siblings the retired prefilter left behind
    (nothing has written one since 2024-12-17) and hand-made files with a suffix.
    """
    found = []
    for path in (root / "weekly-sweeps").glob("linkedin-raw-*.csv"):
        match = PULL_CSV.match(path.name)
        day = parse_date(match.group(1)) if match else None
        if day is not None:
            found.append((day, path))
    return sorted(found)


def latest_raw_csv(root: pathlib.Path) -> pathlib.Path | None:
    pulls = pull_csvs(root)
    return pulls[-1][1] if pulls else None


def render_raw_csvs(root: pathlib.Path, today: dt.date, days: int = RENDER_DAYS) -> list[pathlib.Path]:
    """The pulls the queue is ranked from, newest first: those dated within the last `days` days, else the latest.

    Until 2024-12-23 the queue read the latest pull alone. On 10/25 the catch-up chain was killed between the
    judge and the render, nothing retried it, and at 00:30 the new day's pull replaced it: 16 of that day's
    17 Apply verdicts were never shown. Reading yesterday's pull too makes a missed render recoverable by the
    next one; the queue memory records first_seen per role, so a role from yesterday's pull is shown once and
    ages as usual. A pull older than the window is used only when nothing newer exists (a laptop off for days).
    """
    pulls = pull_csvs(root)
    recent = [path for day, path in reversed(pulls) if 0 <= (today - day).days < days]
    return recent or ([pulls[-1][1]] if pulls else [])


def role_key(role: dict) -> tuple[str, str]:
    return (normalize_company(role.get("company")), normalize_title(role.get("title")))


def read_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"profile": DEFAULT_PROFILE, "apply": [], "skip": [], "last_validated": "not yet"}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_json_file(path: pathlib.Path) -> dict | None:
    """Optional JSON side file: missing or unreadable simply means 'no data'."""
    try:
        with path.open("r", encoding="utf-8-sig") as f:  # tolerate a PowerShell BOM
            loaded = json.load(f)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_raw_jobs(path: pathlib.Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


FEED_EXTRAS = ("pay_interval", "job_url_direct")


def feed_extras(root: pathlib.Path, today: dt.date) -> dict[str, dict[str, str]]:
    """{job_url: {"pay_interval": ..., "job_url_direct": ...}} from the last three weeks of daily pulls.

    Two feed fields are read back from the CSVs (the queue rows carry neither): the published pay interval (the staffing-agency hourly flag) and an Indeed
    row's employer posting (the checker reads it instead of Indeed's page). Files from before
    2024-12-09 have neither.
    """
    extras: dict[str, dict[str, str]] = {}
    for day, path in pull_csvs(root):
        if not 0 <= (today - day).days < MAX_POSTING_AGE_DAYS:
            continue
        for row in read_raw_jobs(path):
            found = {field: row[field] for field in FEED_EXTRAS if row.get(field)}
            if found and row.get("job_url"):
                extras.setdefault(row["job_url"], {}).update(found)
    return extras


def repost_key(row: dict) -> tuple[str, str, str]:
    """Same employer, same title, same place: a job listed in several towns is not a repost."""
    place = re.sub(r"[^a-z0-9]+", " ", str(row.get("location") or "").lower()).strip()
    return (normalize_company(row.get("company")), normalize_title(row.get("title")), place)


def repost_history(root: pathlib.Path, today: dt.date, window_days: int = REPOST_WINDOW_DAYS) -> dict[tuple, dict[str, dt.date]]:
    """{repost_key: {link: first day seen}} from the daily pulls of the last four weeks."""
    history: dict[tuple, dict[str, dt.date]] = {}
    for day, path in pull_csvs(root):
        if not 0 <= (today - day).days < window_days:
            continue
        for row in read_raw_jobs(path):
            url = row.get("job_url")
            if url:
                links = history.setdefault(repost_key(row), {})
                links[url] = min(day, links.get(url, day))
    return history


def repost_info(role: dict, history: dict[tuple, dict[str, dt.date]]) -> dict:
    """{"reposts": n, "since": first day} when the job came back under new links; {} otherwise.
    A second link on the same day (LinkedIn and Indeed copies) is not a repost."""
    links = history.get(repost_key(role)) or {}
    if len(links) < 2:
        return {}
    first = min(links.values())
    later = sum(1 for day in links.values() if (day - first).days >= REPOST_GAP_DAYS)
    return {"reposts": later, "since": first.isoformat()} if later else {}


def read_decisions(root: pathlib.Path) -> dict[str, dict]:
    """{seen_key: {"decision": applied|later|skip, "on": date, ...}}: the owner's clicks on the dashboard."""
    return read_json_file(root / DECISIONS_PATH) or {}


def decision_token(root: pathlib.Path) -> str:
    """The secret the dashboard's buttons send with each click, so no other web page can record one."""
    path = root / DECISION_TOKEN_PATH
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:  # first writer wins; a racing reader re-reads it
            token = secrets.token_hex(16)
            handle.write(token)
        return token
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()


def is_target_location(row: dict) -> bool:
    """The owner's place rule, one copy for every stage since 2024-12-19: sweep_common.in_area."""
    return in_area(row)


def is_probably_relevant(row: dict) -> bool:
    """Raleigh-Durham/remote AND an IT-support title per the shared sweep_common vocabulary.

    Until 2024-12-23 this also excluded employers known to be headquartered outside North Carolina.
    The widened rule counts any North Carolina brick-and-mortar, and a headquarters elsewhere cannot
    disprove a North Carolina branch, so the gate no longer excludes them: the queue-check flags their
    remote roles for the owner's branch confirmation instead of hiding them.
    """
    return is_target_location(row) and relevant(row.get("title", ""))


def raw_candidate(row: dict) -> dict:
    site = row.get("site") or "unknown source"
    return {
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "location": row.get("location", ""),
        "date_posted": str(row.get("date_posted") or "")[:10],
        "job_url": row.get("job_url", ""),
        "site": site,
        "reason": f"Fresh raw match from {site}. Not yet validated against the employer system.",
    }


def rank_raw_candidates(raw_jobs: Iterable[dict], curated_keys: set[tuple[str, str]],
                        today: dt.date | None = None) -> list[dict]:
    """Relevant, uncurated raw rows under three weeks old, newest posting first (undated last), one per role."""
    rows = [row for row in raw_jobs if is_probably_relevant(row) and posted_recently(row.get("date_posted"), today)]
    rows.sort(key=lambda row: str(row.get("date_posted") or "")[:10], reverse=True)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = role_key(row)
        if key in curated_keys or key in seen:
            continue
        seen.add(key)
        out.append(raw_candidate(row))
    return out


def unvalidated_from_raw(raw_jobs: Iterable[dict], curated_keys: set[tuple[str, str]], limit: int = QUEUE_LIMIT) -> list[dict]:
    return rank_raw_candidates(raw_jobs, curated_keys)[:limit]


# --- queue memory + listing check -------------------------------------------------------

def seen_key(cand: dict) -> str:
    return "|".join(role_key(cand))


def validate_candidate(cand: dict) -> dict:
    """Full validation of a raw queue row (open, location, pay, knockouts, North Carolina presence, key tools mentioned); the daily default."""
    return validate_listing.validate(cand, hq_lookup=employer_hq.hq_state, skills=validate_listing.KEY_TOOLS)


def check_listing(cand: dict) -> str:
    """'live' | 'dead' | 'indeterminate' for a raw queue row, via the curated-Apply classifier."""
    url = cand.get("job_url") or ""
    if not url:
        return "indeterminate"
    try:
        return liveness.classify({"title": cand.get("title", ""), "apply_url": url}, liveness.fetch(url, timeout=10))
    except Exception:  # a listing check must never kill dashboard generation
        return "indeterminate"


CHECK_LABELS = {
    "apply_now": "checked today: open, Raleigh-Durham or remote, pay clears the floor or is not published",
    "live": "listing checked live today",
    "review": "checked today, needs a decision",
    "indeterminate": "listing could not be auto-checked",
}


def _normalize_verdict(result: object) -> dict:
    """Checkers may return a bare verdict string (liveness) or a dict (full validation)."""
    if isinstance(result, dict):
        return {
            "verdict": str(result.get("verdict") or "indeterminate"),
            "reasons": [str(r) for r in (result.get("reasons") or [])],
            "facts": dict(result.get("facts") or {}),
        }
    return {"verdict": str(result or "indeterminate"), "reasons": [], "facts": {}}


def carried_candidates(seen: dict, today: dt.date, curated_keys: set[tuple[str, str]], exclude: set[str],
                       max_days: int = QUEUE_MAX_DAYS_SHOWN) -> list[dict]:
    """Validated apply_now roles from the queue memory whose posting has left the feed.

    The daily feed only carries postings from the last 72 hours, so without this a validated role
    was visible for one to three mornings and then vanished: 57 such roles between Sep 1 and Sep 8.
    A remembered role stays a candidate until it is curated, found dead or skipped, or its
    `max_days` window closes; select_queue applies those rules and re-checks the listing.
    """
    curated_flat = {"|".join(k) for k in curated_keys}
    out: list[dict] = []
    for key, entry in seen.items():
        if key in exclude or key in curated_flat:
            continue
        # A first "page not found" is re-checked the next morning even after the feed drops the role.
        if (entry.get("verdict") != "apply_now" and not entry.get("not_found_on")) or entry.get("dead_on") or entry.get("skipped_on"):
            continue
        first = parse_date(entry.get("first_seen"))
        if first is None or (today - first).days > max_days:
            continue
        if not posted_recently(entry.get("date_posted"), today):
            continue
        facts = entry.get("facts") or {}
        out.append({
            "company": entry.get("company", ""),
            "title": entry.get("title", ""),
            "location": entry.get("location") or facts.get("location") or "",
            "date_posted": entry.get("date_posted") or "",
            "job_url": entry.get("job_url") or "",
            "site": entry.get("site") or "queue memory",
            "reason": (f"Validated {entry.get('checked_on') or 'earlier'}; kept on the dashboard for {max_days} days "
                       f"after first seen although the posting has left the 72-hour feed."),
        })
    return out


def select_queue(
    candidates: list[dict],
    seen: dict,
    today: dt.date,
    limit: int = QUEUE_LIMIT,
    checker: Callable[[dict], object] | None = None,
    max_days_shown: int = QUEUE_MAX_DAYS_SHOWN,
    budget_s: float | None = QUEUE_CHECK_BUDGET_S,
) -> dict:
    """Apply the queue memory (and the optional listing check) to ranked candidates.

    The checker returns either a liveness verdict ('live' | 'dead' | 'indeterminate') or a
    full validation dict ('apply_now' | 'review' | 'skip' | 'dead' with reasons and facts).
    'apply_now' rows form their own tier above the review queue and are not capped by
    `limit`; 'dead' and 'skip' rows are dropped and remembered so they are never fetched
    again. A candidate first shown more than `max_days_shown` days ago is dropped
    (auto-expiry). A verdict recorded today is reused so re-renders make no network calls.
    `seen` is updated in place.
    """
    today_s = today.isoformat()
    eligible: list[tuple[dict, str, dict, dt.date, int]] = []
    expired = 0
    for cand in candidates:
        key = seen_key(cand)
        entry = dict(seen.get(key) or {})
        if entry.get("dead_on") or entry.get("skipped_on"):
            entry["last_seen"] = today_s
            seen[key] = entry
            continue
        first = parse_date(entry.get("first_seen")) or today
        age = (today - first).days
        if age > max_days_shown:
            expired += 1
            entry["last_seen"] = today_s
            seen[key] = entry
            continue
        eligible.append((cand, key, entry, first, age))
        if len(eligible) >= limit * 2:  # bounded network work per run
            break

    verdicts: dict[str, dict] = {}
    unfinished = 0
    if checker is not None:
        pending = [(cand, key) for cand, key, entry, _, _ in eligible if entry.get("checked_on") != today_s]

        def safe(cand: dict) -> dict:
            try:
                return _normalize_verdict(checker(cand))
            except Exception as exc:  # a checker bug must never kill the dashboard
                return {"verdict": "indeterminate", "reasons": [f"checker error: {type(exc).__name__}"], "facts": {}}

        if pending:
            # 2024-12-18: a slow Bright Data night (read timeouts) ran this past the step's 10-minute cap twice and
            # the kill lost every verdict. Checks still running at the budget are left unchecked: those rows show as
            # not auto-checked tonight and are tried again on the next run; the finished ones are kept.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
            futures = {pool.submit(safe, cand): key for cand, key in pending}
            try:
                for future in concurrent.futures.as_completed(futures, timeout=budget_s):
                    verdicts[futures[future]] = future.result()
            except concurrent.futures.TimeoutError:
                unfinished = sum(1 for future in futures if not future.done())
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

    queue: list[dict] = []
    dead = skipped = checked = apply_now = 0
    for cand, key, entry, first, age in eligible:
        result = verdicts.get(key)
        reused = False
        if result is None and entry.get("verdict") and (entry.get("checked_on") == today_s or checker is None):
            # Same-day re-renders reuse today's verdict; a render without a checker reuses the last
            # remembered verdict (labelled with its date) instead of showing a validated role as unchecked.
            reused = entry.get("checked_on") != today_s
            result = {"verdict": entry["verdict"], "reasons": list(entry.get("reasons") or []), "facts": dict(entry.get("facts") or {})}
        verdict = result["verdict"] if result else None
        if result and not reused:
            entry["checked_on"] = today_s
            entry["verdict"] = verdict
            entry["reasons"] = result["reasons"]
            entry["facts"] = result["facts"]
            if verdict in ("apply_now", "review"):  # the page is back: forget an earlier "not found"
                entry.pop("not_found_on", None)
        entry["company"] = cand.get("company", "")
        entry["title"] = cand.get("title", "")
        for field in ("job_url", "site", "location", "date_posted"):  # so the memory can re-surface the role by itself
            if cand.get(field):
                entry[field] = cand[field]
        if verdict == "dead" and result["facts"].get("not_found") and (reused or entry.get("not_found_on", today_s) == today_s):
            # "Page not found" can be an employer's site down for maintenance (2024-12-09): hide the role
            # today and check it again tomorrow. Only a second "not found" on a later morning drops it.
            entry["not_found_on"] = entry.get("not_found_on") or today_s
            entry["last_seen"] = today_s
            seen[key] = entry
            continue
        if verdict == "dead":
            dead += 1
            entry["dead_on"] = today_s
            entry["last_seen"] = today_s
            seen[key] = entry
            continue
        if verdict == "skip":
            skipped += 1
            entry["skipped_on"] = today_s
            entry["last_seen"] = today_s
            seen[key] = entry
            continue
        tier = "apply_now" if verdict == "apply_now" else "review"
        if tier == "review" and sum(1 for q in queue if q["tier"] == "review") >= limit:
            seen[key] = entry  # checked but not shown: keep the verdict, not a first_seen
            continue
        checked += 1 if verdict else 0
        apply_now += 1 if tier == "apply_now" else 0
        entry["first_seen"] = first.isoformat()
        entry["last_seen"] = today_s
        seen[key] = entry
        shown = dict(cand)
        shown["tier"] = tier
        shown["first_seen"] = first.isoformat()
        shown["days_shown"] = age
        shown["shown_label"] = "new today" if age == 0 else f"shown since {first.isoformat()} · {age} day(s)"
        label = CHECK_LABELS.get(verdict or "", "listing not auto-checked")
        if reused and entry.get("checked_on"):
            label = (label.replace("checked live today", f"checked live {entry['checked_on']}")
                     .replace("checked today", f"checked {entry['checked_on']}"))
        if result and result["reasons"]:
            label += " (" + "; ".join(result["reasons"]) + ")"
        shown["check_label"] = label
        shown["facts"] = result["facts"] if result else {}
        shown["reason"] = f"{cand.get('reason', '')} {shown['shown_label'].capitalize()}; {shown['check_label']}."
        queue.append(shown)
    return {"queue": queue, "expired": expired, "dead": dead, "skipped": skipped, "checked": checked, "apply_now": apply_now,
            "unfinished": unfinished}


def pipeline_role_keys(pipeline_text: str) -> set[tuple[str, str]]:
    """(company, title) keys of every role row in pipeline.md, so the queue never re-surfaces
    a role already applied to or in an interview loop."""
    keys: set[tuple[str, str]] = set()
    for line in (pipeline_text or "").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[1].lower() in {"company", "---", ""} or set(cells[1]) <= {"-", " "}:
            continue
        company = re.sub(r"\*+", "", cells[1])
        title = re.sub(r"\*+", "", cells[2])
        key = (normalize_company(company), normalize_title(title))
        if all(key):
            keys.add(key)
    return keys


def reconcile_with_pipeline(data: dict) -> tuple[list[dict], list[dict]]:
    """Split the curated Apply roles into (still open, tracked). A role carrying `applied_on` (and `outcome`) is
    tracked and leaves the Apply column, rendering under "Already applied or in process". Until 2024-12-19 a
    regex over pipeline.md rows also set a status (a cell containing "interview" flipped it); pipeline.md is
    narrative now and the check mark in job-dashboard-data.json is the one writer."""
    remaining: list[dict] = []
    tracked: list[dict] = []
    for role in data.get("apply", []):
        if role.get("applied_on"):
            marked = dict(role)
            marked["tracked"] = f"Applied {role['applied_on']}" + (f" · {role['outcome']}" if role.get("outcome") else "")
            tracked.append(marked)
        else:
            remaining.append(role)
    return remaining, tracked


def prune_seen(seen: dict, today: dt.date) -> dict:
    keep = {}
    for key, entry in seen.items():
        last = parse_date(entry.get("last_seen")) or parse_date(entry.get("first_seen")) or today
        if (today - last).days <= SEEN_PRUNE_DAYS:
            keep[key] = entry
    return keep


# --- curated Apply status ------------------------------------------------------------------

def curated_status(role: dict, data: dict | None = None, today: dt.date | None = None) -> dict:
    """{'state': 'recheck'|'stale'|'ok', 'badge': text, 'verified_on': date|None}.

    A role flagged by check_apply_liveness leaves the Apply column ('recheck'). So does a curated
    role whose location fails the search rules (the configured areas, or remote), and one whose `verified_on` (falling back to the file-level
    last_validated) is older than MAX_POSTING_AGE_DAYS: a hand-verified posting is treated like any
    other posting after three weeks. Between STALE_AFTER_DAYS and that it reads 'stale, re-verify'.
    """
    today = today or dt.date.today()
    data = data or {}
    verified_on = parse_date(role.get("verified_on")) or parse_date(data.get("last_validated"))
    if role.get("needs_recheck"):
        return {"state": "recheck", "badge": "⚠ Recheck before applying", "verified_on": verified_on}
    location = str(role.get("location") or "").strip()
    remote_model = "remote" in str(role.get("work_model") or "").lower()
    if location and not remote_model and not is_target_location(role):
        return {"state": "recheck", "badge": f"⚠ Out of area: {location} (Raleigh-Durham or remote only)",
                "verified_on": verified_on}
    if verified_on is not None and (today - verified_on).days > STALE_AFTER_DAYS:
        age = (today - verified_on).days
        if age > MAX_POSTING_AGE_DAYS:
            return {
                "state": "recheck",
                "badge": f"⚠ Expired: verified {verified_on.isoformat()}, {age} days ago; re-verify or skip",
                "verified_on": verified_on,
            }
        return {
            "state": "stale",
            "badge": f"⏳ Stale · re-verify (validated {verified_on.isoformat()}, {age} days ago)",
            "verified_on": verified_on,
        }
    return {"state": "ok", "badge": f"✓ {role.get('verified') or 'Verified'}", "verified_on": verified_on}


def host_of(url: str) -> str:
    match = re.search(r"https?://([^/]+)", url or "")
    host = (match.group(1) if match else "").lower()
    return host[4:] if host.startswith("www.") else host


def check_line(role: dict, records: dict | None) -> tuple[str, str]:
    """(css class, text) for the listing-status line of an Apply card.

    Prefers the ATS-aware verdict recorded by check_apply_liveness over the plain HTTP
    status check, because LinkedIn answers a removed posting with HTTP 200.
    """
    rec = (records or {}).get(role.get("apply_url") or "")
    if rec:
        day = rec.get("checked_on") or "today"
        host = host_of(role.get("apply_url", "")) or "employer site"
        verdict = rec.get("verdict")
        if verdict == "live":
            return "ok", f"auto-check {day}: listing live on {host}"
        if verdict == "dead":
            return "warn", f"auto-check {day}: listing closed or redirected on {host}"
        return "muted", f"auto-check {day}: could not confirm on {host}"
    check = role.get("link_check") or {}
    if check:
        if check.get("ok"):
            return "ok", f"official apply link checked · HTTP {check.get('status')}"
        return "warn", f"link needs manual check · {check.get('status') or 'no response'}"
    return "ok", "official employer link"


# --- pipeline health banner ----------------------------------------------------------------

def pipeline_banner(status: dict | None, now: dt.datetime | None = None, queue_stats: dict | None = None) -> dict:
    """{'state': 'ok'|'warn'|'bad', 'headline': str, 'details': [str]} from pipeline-status.json.

    The morning chain writes that file after every step, so a failed step or a failed alert
    email is visible in the dashboard itself, even when no email arrives.
    """
    now = now or dt.datetime.now().astimezone()
    details: list[str] = []
    if queue_stats:
        parts = [f"{queue_stats.get('shown', 0)} shown"]
        if queue_stats.get("apply_now"):
            parts.insert(0, f"{queue_stats['apply_now']} apply-now")
        if queue_stats.get("skipped"):
            parts.append(f"{queue_stats['skipped']} skipped on pay, level, location or clearance")
        if queue_stats.get("dead"):
            parts.append(f"{queue_stats['dead']} dead listing(s) dropped")
        if queue_stats.get("expired"):
            parts.append(f"{queue_stats['expired']} expired after {QUEUE_MAX_DAYS_SHOWN} days")
        if queue_stats.get("checked"):
            parts.append(f"{queue_stats['checked']} auto-checked")
        details.append("Queue: " + " · ".join(parts))
    if not status:
        return {"state": "warn", "headline": "Pipeline status unknown — no run has been recorded yet.", "details": details}

    prefix = "[TEST] " if status.get("test_mode") else ""
    updated = parse_datetime(status.get("updated") or status.get("started"))
    stamp = updated.strftime("%a %b %d %H:%M") if updated else "unknown time"
    steps = status.get("steps") or []
    failed = [step for step in steps if step.get("exit") not in (0, None)]
    for step in failed:
        line = f"{step.get('name')}: exit {step.get('exit')}"
        if step.get("error"):
            line += f" — {rate_control.sanitize(step['error'], 200)}"
        details.append(line)
    problems = provider_problems(steps, now)
    details.extend(problem["line"] for problem in problems)
    # The alert record (pull-jobs.ps1, since 2024-12-17) is the chain's own one-line verdict on the run; the
    # SMTP email it replaced is gone, and so is the GMAIL message that stood here until 2024-12-19.
    alert = status.get("alert") or {}
    if alert.get("status") == "status-file" and alert.get("headline"):
        details.append("Chain: " + rate_control.sanitize(str(alert["headline"]), 300))

    red_providers = [problem for problem in problems if problem["red"]]
    all_down = all_discovery_down(steps)
    if all_down:
        details.insert(0, "Every discovery source was unavailable on this run — no new roles were pulled.")
    run_date = parse_date(status.get("run_date"))
    if run_date and run_date != now.date() and now.hour >= NO_RUN_GRACE_HOUR:
        # 2024-12-17: the laptop slept through the 00:30 task and the 36-hour rule still read green all day.
        return {"state": "bad", "headline": f"{prefix}No pipeline run since {stamp}: nothing recorded for {now.date().isoformat()}. "
                "The night task did not fire or the laptop was asleep; the catch-up task runs the chain at the next sign-in.",
                "details": details}
    if updated and now - updated > dt.timedelta(hours=RUN_OVERDUE_HOURS):
        return {"state": "bad", "headline": f"{prefix}No pipeline run since {stamp} — the scheduled task may not be firing.", "details": details}
    if failed or red_providers or all_down:
        bits = []
        if failed:
            bits.append(f"{len(failed)} step(s) failed")
        if all_down:
            bits.append("all discovery sources unavailable")
        if red_providers:
            bits.append("; ".join(problem["short"] for problem in red_providers))
        return {"state": "bad", "headline": f"{prefix}Pipeline problems on the {stamp} run: {', '.join(bits)}.", "details": details}
    if problems:
        names = ", ".join(problem["short"] for problem in problems)
        lead = "Pipeline degraded" if status.get("finished") else "Pipeline run in progress, degraded so far"
        return {"state": "warn", "headline": f"{prefix}{lead} on the {stamp} run — {names}. "
                "The rest of the run completed; this is not a clean run.", "details": details}
    if not status.get("finished"):
        return {"state": "warn", "headline": f"{prefix}Pipeline run in progress (started {stamp}); {len(steps)} step(s) clean so far.", "details": details}
    return {"state": "ok", "headline": f"{prefix}Pipeline OK — last run {stamp}, {len(steps)} steps clean.", "details": details}


DISCOVERY_STEPS = ("pull", "target", "usajobs", "jsearch", "hubspot")
REASON_TEXT = {
    "http_429": "rate limited (HTTP 429)", "http_429_paused": "rate limited once (HTTP 429), paused 10-15 min and resumed", "http_402": "quota or billing limit", "quota_exceeded": "quota or billing limit",
    "auth_failed": "credentials rejected — check the API key", "captcha": "CAPTCHA or sign-in wall",
    "authwall": "CAPTCHA or sign-in wall", "circuit_open": "repeated failures, paused", "daily_budget": "daily request budget used",
    "run_budget": "this run's request budget used", "timeout": "timeouts", "network_error": "network errors",
}
PROVIDER_NAMES = {"linkedin": "LinkedIn", "brightdata": "Bright Data", "apify": "Apify (employer lookups)"}


def readable_reason(reason: object) -> str:
    text = str(reason or "").strip()
    if text in REASON_TEXT:
        return REASON_TEXT[text]
    if re.fullmatch(r"http_5\d\d", text) or text.startswith("blocked_"):
        return "server errors" if text.startswith("http_5") else f"blocked ({text[8:]})"
    return rate_control.sanitize(text.replace("_", " "), 80)


def provider_problems(steps: list[dict], now: dt.datetime) -> list[dict]:
    """One entry per provider that was not healthy on a step: {'line', 'short', 'red'} (all text sanitized)."""
    problems: list[dict] = []
    seen: set[str] = set()
    for step in steps:
        providers = step.get("providers") or {}
        if not isinstance(providers, dict):
            continue
        for name, info in providers.items():
            if not isinstance(info, dict) or info.get("state") in (None, "ok") or name in seen:
                continue
            seen.add(name)
            state, runs = info.get("state"), info.get("consecutive_degraded_runs") or 0
            label = PROVIDER_NAMES.get(name) or rate_control.sanitize(str(name).replace("_", " "), 40)
            reason = readable_reason(info.get("reason"))
            until = parse_datetime(info.get("cooldown_until"))
            what = {"cooldown": "paused", "skipped": "skipped", "failed": "failing", "degraded": "degraded"}.get(state, str(state))
            line = f"{label} {what}: {reason}"
            if until and until > now:
                line += f", paused until {until.strftime('%a %b %d %H:%M')}"
            line += " — skipped on purpose to protect the account; other sources ran." if state in ("cooldown", "skipped") else "."
            red = state == "failed" or (isinstance(runs, int) and runs >= 2)
            if isinstance(runs, int) and runs >= 2 and state != "failed":
                line += f" Degraded {runs} runs in a row."
            problems.append({"line": line, "short": f"{label} {what} ({reason})" + (f", {runs} runs in a row" if red and state != "failed" else ""),
                             "red": red})
    return problems


def step_unavailable(step: dict) -> bool:
    """A step that read nothing: it failed, or every provider it used was paused, skipped or failing."""
    if step.get("exit") not in (0, None) or step.get("state") == "failed":
        return True
    providers = step.get("providers")
    return bool(providers) and isinstance(providers, dict) and all(
        isinstance(info, dict) and info.get("state") not in (None, "ok") for info in providers.values())


def all_discovery_down(steps: list[dict]) -> bool:
    """True when two or more discovery steps ran and none of them could read anything."""
    found = [step for step in steps if step.get("name") in DISCOVERY_STEPS]
    return len(found) >= 2 and all(step_unavailable(step) for step in found)


# --- link check (plain HTTP) ---------------------------------------------------------------

def check_url(url: str, timeout: int = 12) -> dict:
    if not url:
        return {"ok": False, "status": None, "final_url": "", "error": "missing URL"}
    headers = {"User-Agent": "Mozilla/5.0 (job-dashboard link check)"}
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {"ok": 200 <= resp.status < 400, "status": resp.status, "final_url": resp.geturl(), "error": ""}
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405, 429}:
                continue
            return {"ok": False, "status": exc.code, "final_url": url, "error": str(exc)}
        except Exception as exc:  # network checks should never kill dashboard generation
            if method == "HEAD":
                continue
            return {"ok": False, "status": None, "final_url": url, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "status": None, "final_url": url, "error": "unknown link-check failure"}


# --- rendering -----------------------------------------------------------------------------

def apply_card(role: dict, status: dict | None = None, records: dict | None = None) -> str:
    status = status or curated_status(role)
    state = status["state"]
    link = role.get("apply_url", "")
    if state == "recheck":
        check_html = f'<p class="note">{esc(role.get("verified"))}</p>'
        button = "Open listing to recheck →"
    else:
        check_class, check_text = check_line(role, records)
        check_html = f'<p class="linkcheck {check_class}">{esc(check_text)}</p>'
        button = f"Apply on {esc(role.get('source', 'company careers'))} →"
    return f"""
    <section class="card apply {state}">
      <div class="card-top">
        <div>
          <h3>{esc(role.get('title'))}</h3>
          <p class="company">{esc(role.get('company'))} — {esc(role.get('company_blurb'))}</p>
        </div>
        <span class="badge {state}">{esc(status['badge'])}</span>
      </div>
      <div class="facts">
        <span>{esc(role.get('location'))}</span><span>{esc(role.get('work_model'))}</span><span>{esc(role.get('salary'))}</span>
      </div>
      <p><b>Why it fits:</b> {esc(role.get('why'))}</p>
      {f'<p class="note">{esc(role.get("note"))}</p>' if role.get('note') else ''}
      {check_html}
      <a class="button" href="{esc(link)}" target="_blank" rel="noopener">{button}</a>
      {decision_buttons(role, link)}
    </section>"""


def skip_card(role: dict) -> str:
    return f"""
    <section class="skip-card">
      <div class="skip-top"><b>{esc(role.get('title'))} — {esc(role.get('company'))}</b><span>{esc(role.get('tag', 'Skip'))}</span></div>
      <p class="muted">{esc(role.get('location'))}</p>
      <p>{esc(role.get('reason'))}</p>
    </section>"""


def skills_chip(role: dict) -> str:
    """The "4 of 7 key tools: Intune, Entra, Active Directory, PowerShell" chip, once the checker has read the posting."""
    f = role.get("facts") or {}
    found, total = f.get("skills"), f.get("skills_of")
    if not isinstance(found, list) or not total:
        return ""
    return f"{len(found)} of {total} key tools" + (": " + ", ".join(found) if found else "")


def skills_found(role: dict) -> int:
    return len((role.get("facts") or {}).get("skills") or [])


def repost_chip(role: dict) -> str:
    info = role.get("repost") or {}
    n = info.get("reposts")
    return f"reposted {n} time{'s' if n != 1 else ''} since {info['since']}" if n else ""


def decision_buttons(role: dict, link: str) -> str:
    """Applied / Later / Not for me. The click is saved by the decision helper (see DECISION_SCRIPT)."""
    data = {"key": seen_key(role), "company": role.get("company"), "title": role.get("title"),
            "url": link, "location": role.get("location")}
    attrs = " ".join(f'data-{name}="{esc(value)}"' for name, value in data.items())
    buttons = "".join(f'<button type="button" data-d="{d}">{label}</button>' for d, label in DECISION_LABELS.items())
    return f'<div class="decide" {attrs}>{buttons}<span class="decide-msg"></span></div>'


def _fact_chips(role: dict) -> list[str]:
    f = role.get("facts") or {}
    chips: list[str] = [c for c in (skills_chip(role), repost_chip(role)) if c]
    if f.get("pay_text"):
        chips.append(f"pay {f['pay_text']}")
    elif f.get("pay_max"):
        chips.append(f"pay to ${int(f['pay_max']):,}")
    if f.get("remote"):
        chips.append("remote")
    if f.get("hybrid"):
        chips.append("hybrid")
    elif f.get("onsite"):
        chips.append("onsite")
    if f.get("applicants") is not None:
        chips.append(f"{f['applicants']} applicants")
    if f.get("posted"):
        chips.append(f"posted {f['posted']}")
    return chips


def queue_link(role: dict) -> str:
    """The page the checker read (an Indeed row's employer posting since 2024-12-09), else the feed link."""
    return (role.get("facts") or {}).get("checked_url") or role.get("job_url", "")


def apply_now_card(role: dict) -> str:
    link = queue_link(role)
    chips = "".join(f"<span>{esc(c)}</span>" for c in [esc(role.get("location"))] + _fact_chips(role))
    return f"""
    <section class="card apply autonow">
      <div class="card-top">
        <div>
          <h3>{esc(role.get('title'))}</h3>
          <p class="company">{esc(role.get('company'))} · via {esc(role.get('site') or 'feed')}</p>
        </div>
        <span class="badge autonow">Checked today · {esc(role.get('shown_label') or 'new')}</span>
      </div>
      <div class="facts">{chips}</div>
      <p class="linkcheck ok">{esc(role.get('check_label'))}</p>
      <p class="muted">Auto-validated for open, location and pay only. Fit is your call; apply direct within 48 hours if it is in lane.</p>
      <a class="button" href="{esc(link)}" target="_blank" rel="noopener">Open and apply →</a>
      {decision_buttons(role, link)}
    </section>"""


def tracked_icon(label: str) -> str:
    """The check mark for an applied or progressing role; a cross once the outcome is a rejection."""
    return "❌" if "rejected" in (label or "").lower() else "✅"


def tracked_card(role: dict) -> str:
    label = role.get("tracked", "Tracked")
    return f"""
    <section class="skip-card tracked">
      <div class="skip-top"><b>{esc(role.get('title'))} — {esc(role.get('company'))}</b><span>{tracked_icon(label)} {esc(label)}</span></div>
      <p class="muted">{esc(role.get('location'))} · applied or in process; not an apply action</p>
    </section>"""


def needs_validation_card(role: dict) -> str:
    link = queue_link(role)
    facts = [esc(role.get("location")), f"posted {esc(role.get('date_posted') or 'n/a')}"]
    for chip in (skills_chip(role), repost_chip(role)):
        if chip:
            facts.append(esc(chip))
    for label in ("shown_label", "check_label"):
        if role.get(label):
            facts.append(esc(role[label]))
    return f"""
    <section class="review-card">
      <div class="skip-top"><b>{esc(role.get('title'))} — {esc(role.get('company'))}</b><span>Needs validation</span></div>
      <p class="muted">{' · '.join(facts)}</p>
      <p>{esc(role.get('reason'))}</p>
      {f'<a href="{esc(link)}" target="_blank" rel="noopener">Source listing →</a>' if link else ''}
      {decision_buttons(role, link)}
    </section>"""


def saved_card(entry: dict) -> str:
    """A job the owner saved with "Later": stays here until the owner clicks Applied or Not for me, or for 30 days."""
    link = entry.get("job_url", "")
    role = {"company": entry.get("company"), "title": entry.get("title"), "location": entry.get("location")}
    return f"""
    <section class="review-card saved">
      <div class="skip-top"><b>{esc(role['title'])} — {esc(role['company'])}</b><span>Saved {esc(entry.get('on'))}</span></div>
      <p class="muted">{esc(role['location'])}</p>
      {f'<a href="{esc(link)}" target="_blank" rel="noopener">Open listing →</a>' if link else ''}
      {decision_buttons(role, link)}
    </section>"""


def apply_decisions(decisions: dict[str, dict], today: dt.date) -> tuple[list[dict], list[dict]]:
    """(saved for later, applied on the dashboard) from the owner's recent clicks, newest first."""
    recent = []
    for entry in decisions.values():
        on = parse_date(entry.get("on"))
        if on is None or (today - on).days < 0:
            continue
        if entry.get("decision") == "later" and (today - on).days > DECISION_SHOW_DAYS:
            continue  # Applied clicks never expire (2024-12-19); the saved cards do
        recent.append(entry)
    recent.sort(key=lambda e: str(e.get("on")), reverse=True)
    saved = [e for e in recent if e.get("decision") == "later"]
    applied = [{"company": e.get("company"), "title": e.get("title"), "location": e.get("location"),
                "tracked": f"Applied {e.get('on')} (marked on the dashboard)"}
               for e in recent if e.get("decision") == "applied"]
    return saved, applied


def _split_curated(data: dict, today: dt.date | None) -> tuple[list[tuple[dict, dict]], list[tuple[dict, dict]]]:
    statuses = [(role, curated_status(role, data, today)) for role in data.get("apply", [])]
    apply = [(role, status) for role, status in statuses if status["state"] != "recheck"]
    recheck = [(role, status) for role, status in statuses if status["state"] == "recheck"]
    return apply, recheck


def _bottom_line(apply_count: int, recheck_count: int, skip_count: int, review_count: int,
                 apply_now_count: int = 0, tracked_count: int = 0) -> str:
    parts = []
    if apply_now_count:
        parts.append(f"Apply now to {apply_now_count} new checked role{'s' if apply_now_count != 1 else ''}")
    parts.append(f"{'apply' if parts else 'Apply'} to {apply_count} curated")
    if recheck_count:
        parts.append(f"recheck {recheck_count}")
    if tracked_count:
        parts.append(f"{tracked_count} already applied or in process")
    parts.append(f"skip {skip_count}")
    if review_count:
        parts.append(f"validate {review_count} fresh raw matches")
    return ", ".join(parts)


# The buttons post to scripts/decision_helper.py: same origin when the helper serves the page (laptop or
# cloud), else the laptop helper. Without a helper the click says it was not saved; nothing is lost.
DECISION_SCRIPT = """<script>
(() => {
  const TOKEN = "__TOKEN__";
  const BASE = location.protocol.startsWith("http") ? "" : "http://127.0.0.1:__PORT__";
  const SAID = {applied: "Applied", later: "Saved for later", skip: "Not for me"};
  const mark = (box, decision) => {
    box.querySelector(".decide-msg").textContent = "Saved: " + (SAID[decision] || decision) + ". It moves at the next refresh.";
    box.closest("section").classList.add("decided");
  };
  fetch(BASE + "/decisions").then(r => r.ok ? r.json() : {}).then(known => {
    document.querySelectorAll(".decide").forEach(box => { if (known[box.dataset.key]) mark(box, known[box.dataset.key]); });
  }).catch(() => {});
  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".decide button");
    if (!button) return;
    const box = button.closest(".decide");
    const msg = box.querySelector(".decide-msg");
    const body = {token: TOKEN, decision: button.dataset.d, key: box.dataset.key, company: box.dataset.company,
                  title: box.dataset.title, job_url: box.dataset.url, location: box.dataset.location};
    msg.textContent = "Saving...";
    try {
      const response = await fetch(BASE + "/decide", {method: "POST", headers: {"Content-Type": "text/plain"}, body: JSON.stringify(body)});
      if (!response.ok) throw new Error("HTTP " + response.status);
      mark(box, button.dataset.d);
    } catch (err) {
      msg.textContent = "Not saved: the decision helper is not running.";
    }
  });
})();
</script>"""


def render_html(data: dict, review: list[dict], banner: dict | None = None, records: dict | None = None,
                today: dt.date | None = None, apply_now: list[dict] | None = None,
                tracked: list[dict] | None = None, saved: list[dict] | None = None, token: str = "") -> str:
    profile = {**DEFAULT_PROFILE, **data.get("profile", {})}
    apply, recheck = _split_curated(data, today)
    skip = data.get("skip", [])
    apply_now = apply_now or []
    tracked = tracked or []
    saved = saved or []
    saved_html = ""
    if saved:
        saved_html = ("<h2>⭐ Saved for later</h2><p class=\"muted\">Jobs you marked Later. They stay here until you click "
                      f"Applied or Not for me, or for {DECISION_SHOW_DAYS} days.</p>" + "".join(saved_card(e) for e in saved))
    script = DECISION_SCRIPT.replace("__TOKEN__", re.sub(r"[^0-9a-f]", "", token)).replace("__PORT__", str(HELPER_PORT))
    refreshed = data.get("refreshed") or today_label()
    validated = data.get("last_validated") or "not yet"
    bottom = _bottom_line(len(apply), len(recheck), len(skip), len(review), len(apply_now), len(tracked))
    apply_now_html = ""
    if apply_now:
        apply_now_html = (
            "<h2>🚀 Apply now: new roles checked today</h2>"
            "<p class=\"muted\">Found by the morning pull and validated automatically against the listing: still open, "
            "Raleigh-Durham or remote, pay at or above the floor or not published, no clearance or senior-level knockout. "
            "Fit was not judged; read the posting once and apply direct if it is in lane.</p>"
            + "".join(apply_now_card(r) for r in apply_now)
        )
    tracked_html = ""
    if tracked:
        tracked_html = (
            f'<details class="history"><summary>📌 Already applied or in process ({len(tracked)})</summary>'
            "<p class=\"muted\">Curated roles marked applied in the data file (the ✅ check mark: applied_on, plus an optional outcome) or whose pipeline.md row says applied, interviewing or rejected. They stay here so the Apply column only holds actions.</p>"
            + "".join(tracked_card(r) for r in tracked)
            + "</details>"
        )
    banner_html = ""
    if banner:
        details = "".join(f"<br>· {esc(line)}" for line in banner.get("details", []))
        banner_html = f'<div class="pipeline {esc(banner["state"])}"><b>{esc(banner["headline"])}</b>{details}</div>'
    recheck_html = ""
    if recheck:
        recheck_html = (
            "<h2>⚠ Recheck before applying</h2>"
            "<p class=\"muted\">The automated liveness check found these postings closed or redirected. "
            "Confirm on the employer site, then clear <code>needs_recheck</code> or set a new <code>verified_on</code> in the data file, "
            "or move them to Skip. Out-of-area roles (not Raleigh-Durham or remote) belong in Skip.</p>"
            + "".join(apply_card(role, status, records) for role, status in recheck)
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Dashboard — {esc(profile['name'])}</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1d2430;--muted:#657080;--line:#e3e7ee;--go:#1f8a4c;--go-bg:#eaf7ef;--warn:#9a6b00;--warn-bg:#fff6e5;--bad:#8a1f1f;--bad-bg:#fdeaea;--skip:#737b88;--review:#2457c5}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.5}} .wrap{{max-width:860px;margin:0 auto;padding:34px 20px 70px}}
h1{{font-size:30px;margin:0 0 3px}} .sub,.muted,footer{{color:var(--muted)}} .sub{{margin:0 0 16px}} .chips{{display:flex;flex-wrap:wrap;gap:8px;margin:15px 0}} .chips span{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:13px}}
.pipeline{{border-radius:12px;padding:12px 15px;margin:14px 0;font-size:14px}} .pipeline.ok{{background:var(--go-bg);border:1px solid #bfe5cd;color:var(--go)}} .pipeline.warn{{background:var(--warn-bg);border:1px solid #f1d28a;color:var(--warn)}} .pipeline.bad{{background:var(--bad-bg);border:1px solid #f0b4b4;color:var(--bad)}}
.summary{{background:var(--go-bg);border:1px solid #bfe5cd;border-radius:14px;padding:15px 17px;margin:22px 0;font-size:16px}} .summary b{{color:var(--go)}} h2{{font-size:14px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:32px 0 12px}}
.card,.skip-card,.review-card{{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:18px;margin:0 0 14px;box-shadow:0 1px 2px rgba(20,30,50,.04)}} .apply{{border-left:5px solid var(--go)}} .apply.stale,.apply.recheck{{border-left-color:var(--warn)}} .apply.autonow{{border-left-color:var(--review)}} .badge.autonow{{background:#eaf0ff;color:var(--review);border-color:#c5d3f5}} .skip-card.tracked span{{background:var(--review)}} .review-card{{border-left:5px solid var(--review)}}
.card-top,.skip-top{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}} h3{{font-size:20px;margin:0}} .company{{color:var(--muted);margin:2px 0 0}} .badge{{background:var(--go-bg);color:var(--go);border:1px solid #bfe5cd;border-radius:999px;padding:4px 10px;font-weight:700;font-size:12px;white-space:nowrap;max-width:46%;white-space:normal;text-align:right}} .badge.stale,.badge.recheck{{background:var(--warn-bg);color:var(--warn);border-color:#f1d28a}}
.facts{{display:flex;flex-wrap:wrap;gap:7px;margin:13px 0}} .facts span{{font-size:13px;background:#f7f8fa;border:1px solid var(--line);border-radius:8px;padding:4px 9px}} .note{{background:var(--warn-bg);color:var(--warn);border-radius:9px;padding:9px 11px;font-size:13px}}
.linkcheck{{font-size:12px;font-weight:700}} .linkcheck.ok{{color:var(--go)}} .linkcheck.warn{{color:var(--warn)}} .linkcheck.muted{{color:var(--muted)}} .button{{display:inline-block;background:var(--go);color:#fff;text-decoration:none;font-weight:700;border-radius:10px;padding:11px 18px;margin-top:4px}} .recheck .button{{background:var(--warn)}}
.skip-card span{{background:var(--skip);color:#fff;border-radius:999px;padding:3px 9px;font-size:11px;font-weight:700}} .review-card span{{background:#eaf0ff;color:var(--review);border-radius:999px;padding:3px 9px;font-size:11px;font-weight:700}} footer{{border-top:1px solid var(--line);margin-top:36px;padding-top:18px;font-size:13px}}
.decide{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}} .decide button{{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;padding:7px 12px;font-weight:600;font-size:13px;cursor:pointer}} .decide button:hover{{border-color:var(--review)}} .decide-msg{{font-size:12px;color:var(--muted)}} section.decided{{opacity:.5}}
details.history summary{{font-size:14px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:32px 0 12px;font-weight:700;cursor:pointer}}
@media print{{body{{background:#fff}} .card,.skip-card,.review-card{{break-inside:avoid;box-shadow:none}} .button{{background:#fff;color:var(--go);border:1px solid var(--go)}} .decide{{display:none}} details.history:not([open])>*:not(summary){{display:block;content-visibility:visible}}}}
</style>
</head>
<body><main class="wrap">
<header>
<h1>Job Dashboard</h1>
<p class="sub">{esc(profile['name'])} · {esc(profile['search'])} · refreshed <b>{esc(refreshed)}</b> · validated <b>{esc(validated)}</b></p>
{banner_html}
<p>This is the single living job hub. The daily routine rewrites this same file instead of creating another dated PDF. In Claude, ask “show my jobs” and use <code>job-dashboard.md</code> for the same view in chat.</p>
<div class="chips"><span>{esc(profile['target'])}</span><span>{esc(profile['salary_floor'])}</span><span>{esc(profile['toolset'])}</span></div>
</header>
<div class="summary"><b>{esc(bottom)}.</b> “Apply now” roles were checked automatically this morning for open, location and pay. Curated roles were confirmed by hand; a stale badge means that was more than {STALE_AFTER_DAYS} days ago. Fresh raw matches stay in “Needs validation” until checked.</div>
{apply_now_html}
{saved_html}
<h2>✅ Apply to these (curated)</h2>
{''.join(apply_card(role, status, records) for role, status in apply) or '<p class="muted">No curated apply roles right now.</p>'}
<h2>🔎 New roles needing validation</h2>
{''.join(needs_validation_card(r) for r in review) or '<p class="muted">No new relevant raw matches outside the curated list.</p>'}
{recheck_html}
{tracked_html}
<details class="history"><summary>⛔ Skip these ({len(skip)})</summary>
{''.join(skip_card(r) for r in skip) or '<p class="muted">No skip decisions yet.</p>'}
</details>
<footer><p><b>Validation rule:</b> do not trust LinkedIn/job-board mirrors alone. Promote a role to Apply only after the company careers page or ATS confirms the role is live, the apply link resolves, and the salary/location fit is honest. Rows in the queue auto-expire after {QUEUE_MAX_DAYS_SHOWN} days; listings the auto-check finds closed are dropped.</p><p><b>Canonical files:</b> dashboard/job-dashboard-data.json → job-dashboard.html + job-dashboard.md. Old daily PDFs are not the hub.</p></footer>
</main>
{script}
</body></html>
"""


def render_markdown(data: dict, review: list[dict], banner: dict | None = None, records: dict | None = None,
                    today: dt.date | None = None, apply_now: list[dict] | None = None,
                    tracked: list[dict] | None = None, saved: list[dict] | None = None) -> str:
    profile = {**DEFAULT_PROFILE, **data.get("profile", {})}
    apply, recheck = _split_curated(data, today)
    skip = data.get("skip", [])
    apply_now = apply_now or []
    tracked = tracked or []
    saved = saved or []
    refreshed = data.get("refreshed") or today_label()
    validated = data.get("last_validated") or "not yet"
    lines = [
        "# Job Dashboard",
        "",
        f"**{profile['name']}** · {profile['search']} · refreshed **{refreshed}** · validated **{validated}**",
        "",
    ]
    if banner:
        lines.append(f"**Pipeline ({banner['state'].upper()}):** {banner['headline']}")
        lines.extend(f"- {line}" for line in banner.get("details", []))
        lines.append("")
    lines.extend([
        f"**Bottom line:** {_bottom_line(len(apply), len(recheck), len(skip), len(review), len(apply_now), len(tracked))}.",
        "",
    ])
    if apply_now:
        lines.append("## 🚀 Apply now: new roles checked today")
        for r in apply_now:
            chips = " · ".join(_fact_chips(r))
            lines.append(f"- **{r.get('company')} — {r.get('title')}** ({r.get('location')}{'; ' + chips if chips else ''}) — {r.get('check_label')}. Apply: {queue_link(r)}")
        lines.append("")
    if saved:
        lines.append("## ⭐ Saved for later")
        for e in saved:
            lines.append(f"- **{e.get('company')} — {e.get('title')}** ({e.get('location')}) — saved {e.get('on')}. Link: {e.get('job_url')}")
        lines.append("")
    lines.append("## ✅ Apply to these (curated)")
    if not apply:
        lines.append("- No validated apply roles yet.")

    def role_lines(r: dict, status: dict) -> list[str]:
        suffix = " ⏳ STALE, RE-VERIFY" if status["state"] == "stale" else (" ⚠ RECHECK" if status["state"] == "recheck" else "")
        _, check_text = check_line(r, records)
        out = [
            f"### {r.get('company')} — {r.get('title')}{suffix}",
            f"- **Status:** {status['badge']}",
            f"- **Location/work:** {r.get('location')} · {r.get('work_model')}",
            f"- **Salary:** {r.get('salary')}",
            f"- **Why:** {r.get('why')}",
            f"- **Company:** {r.get('company_blurb')}",
            f"- **Apply:** {r.get('apply_url')} ({r.get('source')}; {check_text})",
        ]
        if r.get("note"):
            out.append(f"- **Note:** {r.get('note')}")
        out.append("")
        return out

    for r, status in apply:
        lines.extend(role_lines(r, status))
    lines.extend(["", "## 🔎 New roles needing validation"])
    if not review:
        lines.append("- No new relevant raw matches outside the curated list.")
    for r in review:
        extra = "".join(f"; {chip}" for chip in (skills_chip(r), repost_chip(r)) if chip)
        lines.append(
            f"- **{r.get('company')} — {r.get('title')}** ({r.get('location')}; posted {r.get('date_posted') or 'n/a'}"
            f"{extra}) — "
            f"{r.get('reason')} Source: {queue_link(r)}"
        )
    lines.append("")
    if recheck:
        lines.extend(["## ⚠ Recheck before applying",
                      "- The automated liveness check found these closed or redirected. Confirm on the employer site before applying.", ""])
        for r, status in recheck:
            lines.extend(role_lines(r, status))
    if tracked:
        lines.append("## 📌 Already applied or in process")
        for r in tracked:
            lines.append(f"- {tracked_icon(r.get('tracked', ''))} **{r.get('company')} — {r.get('title')}** — {r.get('tracked')}")
        lines.append("")
    lines.append("## ⛔ Skip these")
    if not skip:
        lines.append("- No skip decisions yet.")
    for r in skip:
        lines.append(f"- **{r.get('company')} — {r.get('title')}** ({r.get('location')}) — **{r.get('tag', 'Skip')}**: {r.get('reason')}")
    lines.extend([
        "",
        "## Rules",
        "- This file is the in-chat source. Browser source is `job-dashboard.html`.",
        "- Do not promote a raw job to Apply until employer ATS/careers page confirms it is live and the apply link works.",
        f"- Queue rows auto-expire after {QUEUE_MAX_DAYS_SHOWN} days; a stale badge means validated more than {STALE_AFTER_DAYS} days ago.",
        "- Do not create dated PDF briefs as the hub; update the one dashboard instead.",
    ])
    return "\n".join(lines) + "\n"


def build_dashboard(
    root: pathlib.Path | None = None,
    check_links: bool = True,
    link_checker: Callable[[str], dict] = check_url,
    *,
    queue_checker: Callable[[dict], str] | None = None,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    limit: int = QUEUE_LIMIT,
    write_views: bool = True,
) -> dict:
    """Build the dashboard. With write_views=False (the chain's `queue-check` step, 2024-12-17) only the queue
    listing checks run and their verdicts land in the queue memory; the HTML and Markdown are not written and
    no curated link is fetched, so the later `render` step needs no network and cannot time out."""
    root = pathlib.Path(root) if root is not None else project_root()
    today = today or dt.date.today()
    now = now or dt.datetime.now().astimezone()
    data = read_json(root / DATA_PATH)
    curated_keys = {role_key(r) for r in data.get("apply", []) + data.get("skip", [])}
    pipeline_path = root / "pipeline.md"
    pipeline_text = pipeline_path.read_text(encoding="utf-8", errors="replace") if pipeline_path.exists() else ""
    curated_keys |= pipeline_role_keys(pipeline_text)
    # The queue is ranked from the day's raw CSV (newest posting first). The score-ranked queue from the
    # job-agent database went with that database's retirement on 2024-12-17.
    raw_path = latest_raw_csv(root)
    raw_rows = [row for path in render_raw_csvs(root, today) for row in read_raw_jobs(path)]
    candidates = rank_raw_candidates(raw_rows, curated_keys, today=today)

    seen_path = root / SEEN_PATH
    seen = read_json_file(seen_path) or {}
    # Fresh feed rows first, then validated roles the memory still holds for their seven-day window.
    candidates = candidates + carried_candidates(seen, today, curated_keys, {seen_key(c) for c in candidates})
    # A job the owner already clicked on stays off the queue, even when it comes back under a new link.
    decisions = read_decisions(root)
    candidates = [c for c in candidates if seen_key(c) not in decisions]
    extras = feed_extras(root, today)
    for cand in candidates:  # the validator flags hourly agency jobs and reads an Indeed row's employer posting
        for field, value in extras.get(cand.get("job_url", ""), {}).items():
            cand.setdefault(field, value)
    selection = select_queue(candidates, seen, today, limit=limit, checker=queue_checker)
    history = repost_history(root, today)
    for cand in selection["queue"]:
        info = repost_info(cand, history)
        if not info:
            continue
        cand["repost"] = info
        if info["reposts"] >= CHRONIC_REPOSTS and cand.get("tier") == "apply_now":
            cand["tier"] = "review"  # the owner's rule: a job that keeps coming back may be a ghost job
            cand["check_label"] = (f"{cand.get('check_label', '')} (reposted {info['reposts']} times since "
                                   f"{info['since']}: may be a ghost job, so not Apply now)")
    # Best matches first: most of the owner's key tools mentioned, then the feed's order (newest first).
    apply_now = sorted((c for c in selection["queue"] if c.get("tier") == "apply_now"), key=skills_found, reverse=True)
    review = sorted((c for c in selection["queue"] if c.get("tier") != "apply_now"), key=skills_found, reverse=True)
    atomic_write_text(seen_path, json.dumps(prune_seen(seen, today), indent=1, ensure_ascii=False, sort_keys=True))
    if not write_views:
        return {
            "html_path": "", "md_path": "", "raw_path": str(raw_path) if raw_path else "",
            "review_count": len(review), "apply_now_count": len(apply_now), "tracked_count": 0,
            "queue_dead_dropped": selection["dead"], "queue_skipped": selection.get("skipped", 0),
            "queue_expired": selection["expired"], "queue_checked": selection["checked"], "queue_unfinished": selection["unfinished"], "pipeline_state": "",
        }

    # Curated roles with the applied_on check mark leave the Apply column (pipeline.md is narrative since 2024-12-19).
    remaining, tracked = reconcile_with_pipeline(data)
    # The owner's clicks on curated roles: Applied joins the tracked list, Not for me moves to Skip.
    kept, passed = [], []
    for role in remaining:
        choice = decisions.get(seen_key(role)) or {}
        if choice.get("decision") == "applied":
            tracked.append({**role, "tracked": f"Applied {choice.get('on')} (marked on the dashboard)"})
        elif choice.get("decision") == "skip":
            passed.append({"title": role.get("title"), "company": role.get("company"), "location": role.get("location"),
                           "tag": "Not for me", "reason": f"You marked it Not for me on {choice.get('on')}."})
        else:
            kept.append(role)
    data = {**data, "apply": kept, "skip": passed + list(data.get("skip", []))}
    saved, applied_here = apply_decisions(decisions, today)
    curated_flat = {seen_key(r) for r in tracked}
    tracked += [r for r in applied_here if seen_key(r) not in curated_flat]

    for role in data.get("apply", []):
        if check_links:
            role["link_check"] = link_checker(role.get("apply_url", ""))
    records = read_json_file(root / LIVENESS_PATH) or {}
    status = read_json_file(root / STATUS_PATH)
    queue_stats = {
        "shown": len(review), "dead": selection["dead"], "expired": selection["expired"],
        "checked": selection["checked"], "apply_now": len(apply_now), "skipped": selection.get("skipped", 0),
    }
    banner = pipeline_banner(status, now, queue_stats)

    html_text = render_html(data, review, banner=banner, records=records, today=today, apply_now=apply_now, tracked=tracked,
                            saved=saved, token=decision_token(root))
    md_text = render_markdown(data, review, banner=banner, records=records, today=today, apply_now=apply_now, tracked=tracked,
                              saved=saved)
    html_path = root / HTML_PATH
    md_path = root / MD_PATH
    atomic_write_text(html_path, html_text)
    atomic_write_text(md_path, md_text)
    return {
        "html_path": str(html_path),
        "md_path": str(md_path),
        "raw_path": str(raw_path) if raw_path else "",
        "review_count": len(review),
        "apply_now_count": len(apply_now),
        "tracked_count": len(tracked),
        "queue_dead_dropped": selection["dead"],
        "queue_skipped": selection.get("skipped", 0),
        "queue_expired": selection["expired"],
        "queue_checked": selection["checked"], "queue_unfinished": selection["unfinished"],
        "pipeline_state": banner["state"],
    }


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the one living job dashboard")
    parser.add_argument("--root", default=None, help="Job Search root folder (default: JOBSEARCH_ROOT)")
    parser.add_argument("--no-link-check", action="store_true", help="Skip HTTP checks for curated apply links")
    parser.add_argument("--no-queue-check", action="store_true",
                        help="Skip the listing validation on the needs-validation queue (reuses today's verdicts)")
    parser.add_argument("--liveness-only", action="store_true",
                        help="Only check that listings are still up (no pay/location/knockout validation)")
    parser.add_argument("--queue-check-only", action="store_true",
                        help="Run the queue listing checks into the queue memory and stop: no HTML, no Markdown, no link checks")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root) if args.root else project_root()
    rate_control.set_root(root)  # provider cooldowns and budgets live under this root
    checker = None if args.no_queue_check else (check_listing if args.liveness_only else validate_candidate)
    result = build_dashboard(
        root,
        check_links=not args.no_link_check and not args.queue_check_only,
        queue_checker=checker,
        write_views=not args.queue_check_only,
    )
    print(json.dumps(result, indent=2))
    providers = rate_control.finish_shared_runs()
    if providers:
        print(rate_control.status_line(providers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
