"""The LinkedIn lane since 2024-12-18: one Bright Data job-listings batch a night instead of the guest endpoint.

    scripts\\automation\\venv\\Scripts\\python.exe scripts\\linkedin_feed.py            # the 01:00 trickle task runs this
    scripts\\automation\\venv\\Scripts\\python.exe scripts\\linkedin_feed.py --dry-run  # print the inputs, trigger nothing

Why: the guest search endpoint answered with 429s on every night since 10/17 and gave no posting text
(10/21: 16 of 124 rows with text). Bright Data's pre-built "LinkedIn job listings" scraper
(dataset gd_lpfll7v5hcqtkxl6l, discover_by_keyword) returns the same query groups (21) as structured
records with the full text, pay when published and the posted date, in about two minutes, billed per
record. The sample of 2024-12-18 (five local terms, 25 records) had text on every record.

How the spend is held:
- rate_control.BRIGHTDATA_FEED.daily_budget records a night (300, the owner's cap): limit_per_input is that
  cap divided by the inputs (21: 14 each, 294 at most), and the gate refuses a
  second run the same day.
- jobs_to_not_include carries every LinkedIn job id already in the last EXCLUDE_DAYS of daily CSVs,
  so a posting is never bought twice; time_range is "Past 24 hours" once the feed has run before.
- Every run appends one JSON line to automation-logs\\linkedin-feed-YYYY-MM.jsonl (counts, seconds,
  estimated dollars at BRIGHTDATA_FEED_PRICE_PER_1K, default 1.5) so the cost is a number, not a guess.

Exit codes: 0 when records were merged or the day's budget was already spent; 1 when the feed could
not run (no token, trigger refused, collection failed or timed out), which makes pull-jobs.ps1 fall
back to the guest-endpoint trickle (linkedin_pull.py --trickle) for that night.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from scripts import linkedin_detail, linkedin_pull, rate_control
    from scripts.sweep_common import CSV_COLUMNS, clean_description, merge_rows, pay_interval, project_root, remote_location, yearly_factor
except ModuleNotFoundError:  # run as a script from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import linkedin_detail
    import linkedin_pull
    import rate_control
    from sweep_common import CSV_COLUMNS, clean_description, merge_rows, pay_interval, project_root, remote_location, yearly_factor

DATASET_ID = "gd_lpfll7v5hcqtkxl6l"   # Bright Data: "Linkedin job listings information"
API = "https://api.brightdata.com/datasets/v3"
KEY = "brightdata_feed"
POLL_SECONDS = 15
WAIT_SECONDS = 12 * 60               # the sample took 2 min; the step's cap is 15 min
EXCLUDE_DAYS = 8                     # job ids already in these CSVs are never bought again (the widest window asked for is a week)
MAX_EXCLUDE_IDS = 1500               # newest first; the dry run of 2024-12-18 found 7,230 ids in 21 days, too big a payload for 16 inputs
PRICE_PER_1K_DEFAULT = 1.5           # dollars; override with BRIGHTDATA_FEED_PRICE_PER_1K once the dashboard price is known
FEED_LOG = "linkedin-feed-{month}.jsonl"
JOB_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^/?#]*?-)?(\d{6,})")


def inputs(time_range: str, exclude_ids: list[str]) -> list[dict]:
    """The query groups of linkedin_pull as discover_by_keyword inputs (local terms for Raleigh and Minneapolis, every term remote)."""
    out = []
    for location, remote in linkedin_pull.LOCATIONS:
        for term in (linkedin_pull.TERMS if remote else linkedin_pull.LOCAL_TERMS):
            out.append({"location": location, "keyword": term, "country": "US", "time_range": time_range,
                        "remote": "Remote" if remote else "", "jobs_to_not_include": list(exclude_ids)})
    return out


def known_job_ids(root: Path, today: dt.date, days: int = EXCLUDE_DAYS, limit: int = MAX_EXCLUDE_IDS) -> list[str]:
    """LinkedIn job ids in the last `days` of daily CSVs, newest file first, at most `limit`."""
    ids: list[str] = []
    seen: set[str] = set()
    for path in sorted((root / "weekly-sweeps").glob("linkedin-raw-????-??-??.csv"), reverse=True):
        try:
            day = dt.date.fromisoformat(path.stem[len("linkedin-raw-"):])
        except ValueError:
            continue
        if not 0 <= (today - day).days < days:   # today and the days-1 files before it (2024-12-20: was one file too many)
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                match = JOB_ID.search(row.get("job_url") or "")
                if match and match.group(1) not in seen:
                    seen.add(match.group(1))
                    ids.append(match.group(1))
        if len(ids) >= limit:
            break
    return ids[:limit]


def time_range_for(entry: dict, now: dt.datetime) -> str:
    """Past 24 hours once the feed ran within two days; Past week for the first night or after a gap."""
    last = rate_control.parse_time(entry.get("last_success"))
    return "Past 24 hours" if last and (now - last) <= dt.timedelta(days=2) else "Past week"


def record_to_row(rec: dict) -> dict:
    location = str(rec.get("job_location") or "").strip()
    discovery = rec.get("discovery_input") or {}
    remote = str(discovery.get("remote") or "").lower() == "remote" or "remote" in location.lower()
    if remote:
        location = remote_location(location)
    pay = rec.get("base_salary") if isinstance(rec.get("base_salary"), dict) else {}
    interval = pay_interval(pay.get("payment_period"))
    factor = yearly_factor(interval) if interval else None
    amounts = []
    for field in ("min_amount", "max_amount"):
        value = pay.get(field)
        amounts.append(str(round(value * factor)) if factor and isinstance(value, (int, float)) else "")
    text = clean_description(rec.get("job_summary") or "") or clean_description(rec.get("job_description_formatted") or "")
    url = str(rec.get("url") or "").split("?")[0]
    # the id form every other lane writes (2024-12-19); Bright Data sends the slug form
    url = linkedin_detail.view_url(str(rec.get("job_posting_id") or "") or url) or url
    row = {column: "" for column in CSV_COLUMNS}
    row.update({
        "title": str(rec.get("job_title") or "").strip(), "company": str(rec.get("company_name") or "").strip(),
        "location": location, "date_posted": str(rec.get("job_posted_date") or "")[:10],
        "min_amount": amounts[0], "max_amount": amounts[1], "is_remote": str(remote).lower(),
        "job_url": url, "site": "linkedin", "pay_interval": interval if any(amounts) else "", "description": text,
    })
    return row


def http(method: str, url: str, token: str, body: dict | None = None, timeout: int = 60) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")[:300]
    try:
        return status, json.loads(raw) if raw.strip() else {}
    except ValueError:
        return status, raw[:300]


def feed_log(record: dict, root: Path) -> None:
    path = root / "automation-logs" / FEED_LOG.format(month=record["ts"][:7])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def status(state: str, reason: str = "") -> str:
    return rate_control.status_line({KEY: {"state": state, "reason": reason, "consecutive_degraded_runs": 0}})


def main(argv: list[str] | None = None, *, http_call=None, gate=None, root: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    root = Path(root) if root else project_root()
    rate_control.set_root(root)
    gate = gate or rate_control.get_gate(rate_control.BRIGHTDATA_FEED)
    runtime = gate.runtime
    now = runtime.now()
    today = now.date()
    exclude = known_job_ids(root, today)
    entry = gate.store.get(KEY)
    time_range = time_range_for(entry, now)
    groups = inputs(time_range, exclude)
    cap = rate_control.BRIGHTDATA_FEED.daily_budget or 0
    per_input = max(1, cap // len(groups))
    requested = per_input * len(groups)
    price = float(os.environ.get("BRIGHTDATA_FEED_PRICE_PER_1K") or PRICE_PER_1K_DEFAULT)
    print(f"LinkedIn feed {today}: {len(groups)} inputs, {per_input} records each ({requested} at most, about "
          f"${requested * price / 1000:.2f}), {time_range}, {len(exclude)} known job ids excluded")
    if dry_run:
        print(json.dumps(groups[0], indent=1)[:600])
        return 0
    token = os.environ.get("BRIGHTDATA_API_TOKEN", "")
    if not token:
        print("LinkedIn feed: BRIGHTDATA_API_TOKEN is not set; nothing fetched (the guest lane runs instead)")
        print(status(rate_control.SKIPPED, "api_token_missing"))
        return 1
    call = http_call or (lambda method, url, body=None: http(method, url, token, body))
    admitted = gate.admit(KEY, units=requested)
    if not admitted.ok:
        print(f"LinkedIn feed: skipped ({admitted.reason}): the night's {cap} records are already spent")
        print(status(rate_control.SKIPPED, admitted.reason))
        return 0
    started = runtime.monotonic()
    trigger_url = (f"{API}/trigger?dataset_id={DATASET_ID}&type=discover_new&discover_by=keyword"
                   f"&limit_per_input={per_input}&include_errors=true&format=json")
    code, body = call("POST", trigger_url, {"input": groups, "limit_per_input": per_input})
    snapshot = body.get("snapshot_id") if isinstance(body, dict) else None
    if code >= 300 or not snapshot:
        reason = "auth_failed" if code in (401, 403) else ("quota_exceeded" if code == 402 else f"trigger_http_{code}")
        gate.refund(KEY, requested)  # nothing was bought; the catch-up chain may try again
        gate.failure(KEY, reason, rate_control.sanitize(body), status=rate_control.FAILED)
        print(f"LinkedIn feed: trigger refused (HTTP {code}): {rate_control.sanitize(body, 120)}")
        print(status(rate_control.FAILED, reason))
        return 1
    print(f"LinkedIn feed: snapshot {snapshot} collecting")
    state = ""
    while runtime.monotonic() - started < WAIT_SECONDS:
        code, body = call("GET", f"{API}/progress/{snapshot}")
        state = body.get("status", "") if isinstance(body, dict) else ""
        if state in ("ready", "failed", "canceled"):
            break
        runtime.sleep(POLL_SECONDS)
    if state != "ready":
        reason = "collection_timeout" if state in ("", "running") else "collection_failed"
        gate.failure(KEY, reason, f"snapshot {snapshot} {state or 'not ready'}", status=rate_control.FAILED)
        print(f"LinkedIn feed: snapshot {snapshot} {state or 'not ready'} after {runtime.monotonic() - started:.0f} s")
        print(status(rate_control.FAILED, reason))
        return 1
    code, body = call("GET", f"{API}/snapshot/{snapshot}?format=json")
    records = body if isinstance(body, list) else (body.get("data") if isinstance(body, dict) else None) or []
    errors = [r for r in records if isinstance(r, dict) and r.get("error")]
    good = [r for r in records if isinstance(r, dict) and not r.get("error") and r.get("job_title")]
    seen: set[str] = set()
    rows = []
    for rec in good:
        match = JOB_ID.search(str(rec.get("url") or ""))
        job_id = str(rec.get("job_posting_id") or (match.group(1) if match else ""))
        if job_id and job_id in seen:
            continue
        seen.add(job_id)
        rows.append(record_to_row(rec))
    rows = linkedin_pull.dedupe_rows(rows)
    merged = merge_rows(rows, root) if rows else {"added": 0, "path": "", "total": 0}
    with_text = sum(1 for r in rows if r.get("description"))
    seconds = runtime.monotonic() - started
    record = {"ts": now.isoformat(timespec="seconds"), "inputs": len(groups), "per_input": per_input, "requested": requested,
              "records": len(records), "record_errors": len(errors), "unique": len(rows), "added": merged["added"],
              "with_text": with_text, "time_range": time_range, "excluded": len(exclude), "seconds": round(seconds),
              "est_dollars": round(len(records) * price / 1000, 3), "snapshot": snapshot}
    feed_log(record, root)
    print(f"LinkedIn feed: {len(records)} records ({len(errors)} with errors), {len(rows)} unique postings, "
          f"{merged['added']} new in the CSV, {with_text} with text, {seconds:.0f} s, about ${record['est_dollars']:.2f}")
    if rows:
        gate.success(KEY)
        print(status(rate_control.OK))
    else:
        gate.failure(KEY, "no_records", status=rate_control.DEGRADED)
        print(status(rate_control.DEGRADED, "no_records"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
