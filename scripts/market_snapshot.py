"""Raleigh-Durham job-market snapshot for the Monday edition of the cloud daily brief (added 2024-12-09).

Reads the last 7 nightly pulls (weekly-sweeps), the dashboard's check memory (dashboard/queue-seen.json)
and the owner's dashboard clicks (dashboard/decisions.json), and prints a few plain numbers: how many IT jobs
appeared in the owner's area, how many passed the owner's rules, how many were reposts and who reposts most, and what
the owner clicked. Read-only. Since 2024-12-10 scripts/cloud_sync.py writes this text to the brief repo as
inputs/market-snapshot.md every evening and the brief copies it on Mondays (the laptop weekly review
that used to run it is retired).

    python scripts/market_snapshot.py
"""
from __future__ import annotations

import collections
import datetime as dt
import pathlib

try:
    from scripts import employer_hq
    from scripts import job_dashboard as dash
    from scripts.sweep_common import is_second_metro, is_remote_row, is_home_region, is_staffing_agency, project_root, relevant
except ModuleNotFoundError:  # Direct execution: scripts/ is sys.path[0].
    import employer_hq
    import job_dashboard as dash
    from sweep_common import is_second_metro, is_remote_row, is_home_region, is_staffing_agency, project_root, relevant

WEEK_DAYS = 7


def source_group(site: object) -> str:
    s = str(site or "").lower()
    if s.startswith("linkedin"):
        return "LinkedIn"
    if s.startswith("indeed"):
        return "Indeed"
    if s.startswith("usajobs"):
        return "USAJOBS"
    return "company job boards"


def snapshot(root: pathlib.Path, today: dt.date, days: int = WEEK_DAYS) -> dict:
    start = today - dt.timedelta(days=days - 1)
    pulls = 0
    jobs: dict[tuple, dict] = {}   # one entry per job (employer + title + place), first row seen
    links: set[str] = set()
    for day, path in dash.pull_csvs(root):
        if not start <= day <= today:
            continue
        pulls += 1
        for row in dash.read_raw_jobs(path):
            if not (dash.is_target_location(row) and relevant(row.get("title", ""))):
                continue
            links.add(row.get("job_url", ""))
            jobs.setdefault(dash.repost_key(row), row)

    rows = list(jobs.values())
    # Since 2024-12-23 the rule counts any North Carolina brick-and-mortar, not just a North Carolina HQ, so nothing is
    # removed for headquarters alone. What remains worth counting: remote roles at an employer known to be
    # headquartered out of state, which the dashboard flags for the owner's North Carolina-branch confirmation.
    branch_check = [r for r in rows if is_remote_row(r) and employer_hq.known_outside_home_state(r.get("company", ""))]
    history = dash.repost_history(root, today)
    reposts = {key: dash.repost_info(row, history) for key, row in jobs.items()}
    reposted = {key: info for key, info in reposts.items() if info}
    by_employer = collections.Counter()
    for key, info in reposted.items():
        by_employer[jobs[key].get("company", "")] += info["reposts"]

    week = {(start + dt.timedelta(days=n)).isoformat() for n in range(days)}
    verdicts = collections.Counter()
    for entry in (dash.read_json_file(root / dash.SEEN_PATH) or {}).values():
        if entry.get("checked_on") in week and entry.get("verdict"):
            verdicts[entry["verdict"]] += 1
    clicks = collections.Counter(e.get("decision") for e in dash.read_decisions(root).values() if e.get("on") in week)

    return {
        "start": start.isoformat(), "end": today.isoformat(), "pulls": pulls,
        "links": len(links - {""}), "jobs": len(rows),
        # A remote row can name a Raleigh-Durham city ("Remote (Raleigh, NC)"), so remote is decided first.
        "home_region": sum(1 for r in rows if not is_remote_row(r) and not is_second_metro(r.get("location"))),
        "second_metro": sum(1 for r in rows if not is_remote_row(r) and is_second_metro(r.get("location"))),
        "remote": sum(1 for r in rows if is_remote_row(r)),
        "by_source": dict(collections.Counter(source_group(r.get("site")) for r in rows).most_common()),
        "home_state_branch_check": len(branch_check),
        "agencies": sum(1 for r in rows if is_staffing_agency(r.get("company", ""))),
        "agencies_hourly": sum(1 for r in rows if is_staffing_agency(r.get("company", "")) and r.get("pay_interval") == "hourly"),
        "verdicts": dict(verdicts),
        "reposted": len(reposted),
        "chronic": sum(1 for info in reposted.values() if info["reposts"] >= dash.CHRONIC_REPOSTS),
        "top_reposters": by_employer.most_common(5),
        "clicks": {d: clicks.get(d, 0) for d in ("applied", "later", "skip")},
    }


def render(s: dict) -> str:
    pct = lambda part, whole: f"{round(100 * part / whole)}%" if whole else "0%"
    v = s["verdicts"]
    lines = [
        f"Raleigh-Durham market snapshot, {s['start']} to {s['end']} ({s['pulls']} morning pulls)",
        f"- IT jobs in your area: {s['jobs']} (from {s['links']} links): {s['home_region']} in Raleigh-Durham, {s['second_metro']} in Greater Minneapolis, {s['remote']} remote",
        "- Where they came from: " + (", ".join(f"{name} {n}" for name, n in s["by_source"].items()) or "none"),
        f"- Remote roles at employers headquartered out of state (a North Carolina branch would still qualify): {s['home_state_branch_check']}",
        f"- Staffing agencies: {s['agencies']} ({s['agencies_hourly']} with hourly pay)",
        f"- Checked by the dashboard this week: Apply now {v.get('apply_now', 0)}, review {v.get('review', 0)}, "
        f"skipped by your rules {v.get('skip', 0)}, closed {v.get('dead', 0)}, could not check {v.get('indeterminate', 0)}",
        f"- Came back under a new link (reposts): {s['reposted']} of {s['jobs']} jobs ({pct(s['reposted'], s['jobs'])}); "
        f"3 or more times: {s['chronic']}",
        "- Employers reposting most: " + (", ".join(f"{name} ({n})" for name, n in s["top_reposters"]) or "none"),
        f"- Your clicks this week: Applied {s['clicks']['applied']}, Later {s['clicks']['later']}, Not for me {s['clicks']['skip']}",
    ]
    return "\n".join(lines)


def main() -> int:
    print(render(snapshot(project_root(), dt.date.today())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
