"""JSearch sweep: Google for Jobs listings around Raleigh through OpenWeb Ninja (added 2024-12-10).

Google's job listings reach boards the LinkedIn pull covers poorly or not at all (Indeed, ZipRecruiter,
Adzuna, BeBee, GovernmentJobs.com). The owner's free OpenWeb Ninja plan allows about 100 requests a month and
then stops, so the sweep runs three searches of one page (10 jobs) each, once a day: about 90 a month.
Rows pass the shared title filter and merge into the daily CSV, where merge_rows drops jobs the pull
already has (same company and title).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts import rate_control
    from scripts.sweep_common import merge_rows, pay_interval, project_root, relevant, remote_location, yearly_factor
except ModuleNotFoundError:
    import rate_control
    from sweep_common import merge_rows, pay_interval, project_root, relevant, remote_location, yearly_factor

KEY_NAME = "Jsearch_API_TOKEN"  # as the owner saved it; Windows environment names ignore case
URL = "https://api.openwebninja.com/jsearch/search-v2"
QUERIES = (
    "systems administrator jobs in Raleigh, NC",
    "IT support jobs in Raleigh, NC",
    "help desk jobs in Raleigh, NC",
)
# 80 km is the pull's 50 miles. Three days matches the pull's 72-hour window and covers mornings
# when the laptop was closed.
PARAMS = {"country": "us", "date_posted": "3days", "num_pages": "1", "radius": "80"}
LINKEDIN_JOB = re.compile(r"^/jobs/view/(?:[^/]*-)?(\d+)/?$")  # ".../jobs/view/it-support-at-acme-4478573929"


def get_key():
    key = os.environ.get(KEY_NAME)
    if key:
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return winreg.QueryValueEx(handle, KEY_NAME)[0]
    except OSError:
        return None


def search(key: str, query: str) -> list[dict]:
    url = URL + "?" + urllib.parse.urlencode({"query": query, **PARAMS})
    request = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "job-search-sweep/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["data"]["jobs"]


def clean_link(url: object) -> str:
    """Drop Google's utm_* tracking but keep what a board needs (Indeed's jk, ZipRecruiter's jid)."""
    parts = urllib.parse.urlsplit(str(url or ""))
    match = LINKEDIN_JOB.match(parts.path) if parts.netloc.endswith("linkedin.com") else None
    if match:  # the pull's form, so one LinkedIn job keeps one URL in the job database (url is unique there)
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def job_to_row(job: dict) -> dict:
    company = str(job.get("employer_name") or "").strip()
    title = str(job.get("job_title") or "").strip()
    suffix = f" - {company}".lower()
    if company and title.lower().endswith(suffix):  # "System Administrator - Catholic Health Services"
        title = title[: -len(suffix)].strip()
    location = str(job.get("job_location") or "").strip()
    remote = bool(job.get("job_is_remote"))
    if remote:
        location = remote_location(location)
    interval = pay_interval(job.get("job_salary_period"))
    factor = yearly_factor(interval) if interval else None
    pay = []
    for field in ("job_min_salary", "job_max_salary"):  # yearly amounts, as in every other source
        amount = job.get(field)
        pay.append(str(round(amount * factor)) if factor and isinstance(amount, (int, float)) else "")
    direct = next((o.get("apply_link") for o in job.get("apply_options") or [] if o.get("is_direct")), "")
    return {
        "title": title,
        "company": company,
        "location": location,
        "date_posted": str(job.get("job_posted_at_datetime_utc") or "")[:10],
        "min_amount": pay[0],
        "max_amount": pay[1],
        "is_remote": str(remote).lower(),
        "description": str(job.get("job_description") or ""),
        "job_url": clean_link(job.get("job_apply_link") or job.get("job_google_link")),
        "site": "jsearch",
        "pay_interval": interval if any(pay) else "",
        "job_url_direct": clean_link(direct) if direct else "",
    }


def ran_today(marker, today: dt.date) -> bool:
    """One run a day keeps the free plan's monthly cap: a rerun of the morning task would spend 3 more."""
    return marker.exists() and marker.read_text(encoding="utf-8").strip() == today.isoformat()


AUTH_CODES = (401, 403)  # a rejected token needs a human: the step goes red


def classify(exc: BaseException) -> tuple[str, str] | None:
    """How a search failure is reported on the PROVIDER_STATUS line (2024-12-30).

    JSearch is one of six discovery sources, so an external failure (quota or rate limit, a 5xx, a
    network error, a timeout, a body that is not JSON) makes the step degraded, not red: the night and
    the GitHub run stay green and the dashboard banner says why. A 401 or 403 is a failed provider: the
    token was rejected and only the owner can fix that. None means "not an external failure" (a schema change,
    a code bug): the caller re-raises and the traceback plus exit 1 are the right signal.
    """
    if isinstance(exc, urllib.error.HTTPError):  # before URLError: HTTPError is its subclass
        state = rate_control.FAILED if exc.code in AUTH_CODES else rate_control.DEGRADED
        return state, f"http_{exc.code}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return rate_control.DEGRADED, "network_error"
    if isinstance(exc, json.JSONDecodeError):
        return rate_control.DEGRADED, "bad_json"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    key = get_key()
    if not key:
        print(f"{KEY_NAME} env var not set")
        return 1
    root = project_root()
    marker, today = root / "automation-logs" / "jsearch-last-run.txt", dt.date.today()
    if "--force" not in argv and ran_today(marker, today):
        print("JSearch already ran today; skipped to save the free monthly requests (use --force to rerun)")
        return 0
    attempted = succeeded = 0
    rows: list[dict] = []
    failures: list[tuple[str, str]] = []  # (provider state, short reason) per failed search
    for query in QUERIES:
        attempted += 1
        try:
            jobs = search(key, query)
        except Exception as exc:
            kind = classify(exc)
            if kind is None:
                raise  # a schema change or a code bug: the traceback and exit 1 are the right signal
            failures.append(kind)
            print(f"\n=== {query}: FETCH FAILED - {type(exc).__name__}: {exc} ===")
            continue
        succeeded += 1
        mapped = [row for row in map(job_to_row, jobs) if relevant(row["title"])]
        rows.extend(mapped)
        print(f"\n=== {query}: {len(mapped)} kept (of {len(jobs)}) ===")
        for row in mapped:
            print(f"  {row['title']} | {row['company']} | {row['location']} | posted {row['date_posted']}")
            print(f"    {row['job_url']}")
    if succeeded:  # a run where every search failed can be retried the same day
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(today.isoformat(), encoding="utf-8")
    if rows:
        merged = merge_rows(rows, root)
        print(f"\nMerged {merged['added']} new rows ({merged['total']} total) -> {merged['path']}")
    print(f"Searches successful: {succeeded}/{attempted}")
    auth_failed = any(state == rate_control.FAILED for state, _ in failures)
    if failures:
        # The reason names the failure that set the state: a rejected token when there is one, even if a
        # 429 came first; otherwise the first failure.
        reason = next(r for s, r in failures if s == rate_control.FAILED) if auth_failed else failures[0][1]
        entry = {"state": rate_control.FAILED if auth_failed else rate_control.DEGRADED,
                 "reason": f"{len(failures)} of {attempted} searches: {reason}"}
    else:
        entry = {"state": rate_control.OK, "reason": ""}
    print(rate_control.status_line({"jsearch": entry}))
    return 1 if auth_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
