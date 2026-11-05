#!/usr/bin/env python3
"""check_apply_liveness.py -- daily liveness check for CURATED Apply roles.

Reads dashboard/job-dashboard-data.json, checks each Apply role's apply_url against
its ATS over plain HTTP, and flags any role whose posting has closed or redirected
away with a "NEEDS RECHECK" note -- instead of letting a dead role keep claiming it
is open (the loophole that left closed roles showing as validated-open).

It only FLAGS; it never deletes a role -- a human clears the flag. Exits 1 when any
role is flagged dead so the daily pipeline (pull-jobs.ps1) raises its one-a-day alert.
Every verdict (live / dead / indeterminate) is also written to
dashboard/apply-liveness.json so the dashboard can show "auto-check <date>: live on
<host>" instead of a plain HTTP status that LinkedIn answers 200 even for removed jobs.

Signals (empirically validated 2024-10-16, see the commit message):
  - HTTP 404/410 ................................. dead
  - any other HTTP error (403/429/999 bot blocks, 5xx) ... indeterminate
  - linkedin.com: final URL no longer under /jobs/view/ ... dead (redirected to search);
        a login wall (authwall) is indeterminate, not dead
  - ashbyhq.com: none of the role's distinctive title tokens appear in the page ... dead
  - greenhouse / workable / other: role title tokens present in <title> ... live;
        an explicit dead-marker phrase in the body ..................... dead
  - network error / timeout / anything ambiguous ... indeterminate (left untouched)

The fetch and the decision are split so the classifier is unit-testable without network
(see tests/test_check_apply_liveness.py). job_dashboard.py reuses fetch()+classify() to
check the needs-validation queue the same way.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

RECHECK_PREFIX = "\u26a0 NEEDS RECHECK"
LIVENESS_FILE = "apply-liveness.json"  # under dashboard/, read by job_dashboard.py
DEAD_MARKERS = (
    "no longer accepting",
    "no longer available",
    "position has been filled",
    "this job is closed",
    "job not found",
    "posting is closed",
)
GENERIC_TOKENS = {
    "the", "and", "for", "iii", "ii", "inc", "llc", "ltd", "remote", "onsite",
    "hybrid", "senior", "junior", "client", "role", "job",
}


def role_tokens(title: str) -> list[str]:
    """Distinctive lowercased words from a role title (len >= 4, non-generic)."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return [w for w in words if len(w) >= 4 and w not in GENERIC_TOKENS]


def page_title(body: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    return (m.group(1) if m else "").strip().lower()


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").lower()


def classify(role: dict, fetched: dict) -> str:
    """Return 'live' | 'dead' | 'indeterminate' from a fetched result.

    `fetched` = {status:int|None, final_url:str, body:str, error:str|None}.
    Conservative: only returns 'dead' on a strong signal; ambiguity -> 'indeterminate'.
    """
    if fetched.get("error"):
        return "indeterminate"
    status = fetched.get("status")
    if status in (404, 410):
        return "dead"
    if status is None or status >= 400:
        # 403/429/999 bot blocks and 5xx say nothing about the posting itself.
        return "indeterminate"

    final_url = (fetched.get("final_url") or role.get("apply_url") or "")
    body = fetched.get("body") or ""
    low = body.lower()
    host = host_of(role.get("apply_url") or "")
    tokens = role_tokens(role.get("title", ""))
    title = page_title(body)
    haystack = f"{title}\n{low[:20000]}"
    tokens_hit = sum(1 for t in tokens if t in haystack)

    if any(marker in low for marker in DEAD_MARKERS):
        return "dead"

    # Greenhouse answers a removed job id with a redirect to the board carrying ?error=true
    # (such ids also 404 on the boards API).
    if "greenhouse.io" in host_of(final_url) and "error=true" in final_url.lower():
        return "dead"

    if host.endswith("linkedin.com"):
        low_final = final_url.lower()
        if "authwall" in low_final or "%2fjobs%2fview%2f" in low_final:
            return "indeterminate"  # login wall carrying the job URL: blocked, not removed
        # A live posting stays under /jobs/view/; a removed one 302s to a search page.
        return "live" if "/jobs/view/" in low_final else "dead"

    if host.endswith("ashbyhq.com"):
        # SPA shell: a live posting server-renders the role into og:title/description,
        # so its distinctive tokens appear; a dead one degrades to the generic board.
        return "live" if tokens and tokens_hit >= 1 else "dead"

    # greenhouse / workable / employer sites: SSR the role name into <title>.
    if tokens and sum(1 for t in tokens if t in title) >= 1:
        return "live"
    return "indeterminate"


def fetch(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return {
            "status": r.status,
            "final_url": r.url,
            "body": r.read(60000).decode("utf-8", "ignore"),
            "error": None,
        }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "final_url": url, "body": "", "error": None}
    except Exception as e:  # noqa: BLE001 -- network/timeout -> indeterminate, never crash
        return {"status": None, "final_url": url, "body": "", "error": f"{type(e).__name__}: {e}"}


def flag_role(role: dict, day: str) -> bool:
    """Mark a role needs-recheck (idempotent). Returns True if it changed."""
    if role.get("needs_recheck"):
        return False
    role["needs_recheck"] = True
    old = role.get("verified", "")
    role["verified"] = (
        f"{RECHECK_PREFIX} \u2014 auto-liveness check found this posting closed or "
        f"redirected away ({day}). Confirm before applying. Prior note: {old}"
    )
    return True


def clear_flag(role: dict) -> bool:
    """A role that came back live drops any stale recheck flag. Returns True if changed."""
    if not role.get("needs_recheck"):
        return False
    role.pop("needs_recheck", None)
    role["verified"] = re.sub(
        rf"^{re.escape(RECHECK_PREFIX)}.*?Prior note:\s*", "", role.get("verified", "")
    )
    return True


def apply_actions(data: dict, root: pathlib.Path) -> list[dict]:
    """Curated Apply roles that are still apply actions, as the dashboard defines them.

    Roles carrying the `applied_on` check mark already leave the Apply column
    (job_dashboard.reconcile_with_pipeline; pipeline.md stopped being parsed for status on 2024-12-19),
    so a posting that closes after an application was sent must not fail the run
    every morning. Returns the original dicts so a flag written to one still lands in the data file.
    """
    try:
        from job_dashboard import reconcile_with_pipeline  # local import: job_dashboard imports this module
    except ImportError:
        from scripts.job_dashboard import reconcile_with_pipeline
    remaining, _tracked = reconcile_with_pipeline(data)
    return remaining


def run(root: pathlib.Path, day: str, fetcher=fetch) -> dict:
    data_path = root / "dashboard" / "job-dashboard-data.json"
    liveness_path = root / "dashboard" / LIVENESS_FILE
    data = json.loads(data_path.read_text(encoding="utf-8"))
    apply = apply_actions(data, root)
    summary = {"live": 0, "dead": 0, "indeterminate": 0, "flagged": [], "changed": False, "records": 0}
    records: dict[str, dict] = {}
    for role in apply:
        url = role.get("apply_url")
        if not url:
            summary["indeterminate"] += 1
            continue
        fetched = fetcher(url)
        verdict = classify(role, fetched)
        records[url] = {
            "checked_on": day,
            "verdict": verdict,
            "status": fetched.get("status"),
            "final_url": fetched.get("final_url") or "",
            "company": role.get("company", ""),
            "title": role.get("title", ""),
        }
        summary[verdict] += 1
        if verdict == "dead":
            summary["flagged"].append(role.get("company", "?"))
            if flag_role(role, day):
                summary["changed"] = True
        elif verdict == "live":
            if clear_flag(role):
                summary["changed"] = True
    if summary["changed"]:
        write_json(data_path, data)
    write_json(liveness_path, records)
    summary["records"] = len(records)
    return summary


def write_json(path: pathlib.Path, value) -> None:
    """Whole file to a temp name in the same folder, then one replace: the render and the show-my-jobs
    skill read both files while this step may be writing them (2024-12-19 review)."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    root = pathlib.Path(
        argv[0] if argv and not argv[0].startswith("--") else str(pathlib.Path(__file__).resolve().parents[1])
    )
    # The date is passed in (never call today() in the testable core); the CLI defaults to today.
    day = dt.date.today().isoformat()
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            day = argv[i + 1]
        elif a == "--root" and i + 1 < len(argv):
            root = pathlib.Path(argv[i + 1])
    s = run(root, day, fetcher=fetch)  # looked up at call time so tests can patch it
    print(json.dumps({k: s[k] for k in ("live", "dead", "indeterminate", "flagged", "records")}))
    print(status_line(s))
    if s["dead"]:
        print(
            f"APPLY-CHECK: {s['dead']} curated Apply role(s) flagged closed: "
            f"{', '.join(s['flagged'])}",
            file=sys.stderr,
        )
    return 0


def status_line(summary: dict) -> str:
    """The PROVIDER_STATUS line pull-jobs.ps1 folds into pipeline-status.json (same shape as
    scripts/rate_control.status_line). Closed curated roles make the step degraded, not failed
    (2024-12-17): they are already shown under "Recheck before applying", and three stale curated
    roles had made every night read "1 step failed" since 2024-12-14. The step fails only when the
    check itself cannot run (an exception, which exits non-zero on its own)."""
    dead, flagged = summary.get("dead", 0), summary.get("flagged", [])
    state = "degraded" if dead else "ok"
    reason = f"{dead} closed: {', '.join(flagged)}" if dead else ""
    providers = {"curated_apply": {"state": state, "reason": reason, "consecutive_degraded_runs": 0}}
    return "PROVIDER_STATUS " + json.dumps({"state": state, "providers": providers}, sort_keys=True)


if __name__ == "__main__":
    # argv: [root?] [--date YYYY-MM-DD] [--root PATH]
    raise SystemExit(main(sys.argv[1:]))
