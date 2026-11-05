"""USAJOBS 2210 sweep with canonical row mapping and truthful section exits."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

try:
    from scripts.sweep_common import FEDERAL_2210_EXTRA, merge_rows, pay_interval, project_root, relevant, yearly_factor
except ModuleNotFoundError:
    from sweep_common import FEDERAL_2210_EXTRA, merge_rows, pay_interval, project_root, relevant, yearly_factor

GRADE_LO, GRADE_HI = 9, 12
HOME_REGION = {"LocationName": "Raleigh, North Carolina", "Radius": "50"}  # same area as the daily LinkedIn/Indeed pull
SECOND_METRO = {"LocationName": "Minneapolis, Minnesota", "Radius": "30"}  # second example search area: about I-494/I-694
# The title vocabulary lives in sweep_common (one list for every stage). The federal extras
# only apply here because every query below is restricted to the 2210 (IT) series.


def support_titled(title: object) -> bool:
    return relevant(title, extra=FEDERAL_2210_EXTRA)


def get_key():
    key = os.environ.get("USAJOBS_API_KEY")
    if key:
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return winreg.QueryValueEx(handle, "USAJOBS_API_KEY")[0]
    except OSError:
        return None


def search(key: str, **params):
    query = {"JobCategoryCode": "2210", "ResultsPerPage": "100", **params}
    url = "https://data.usajobs.gov/api/search?" + urllib.parse.urlencode(query)
    headers = {
        "Host": "data.usajobs.gov", "User-Agent": os.environ.get("USAJOBS_EMAIL", "you@example.com"),
        "Authorization-Key": key,
    }
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)["SearchResult"]["SearchResultItems"]


def grade_ok(descriptor: dict) -> bool:
    details = descriptor.get("UserArea", {}).get("Details", {})
    try:
        low, high = int(details.get("LowGrade")), int(details.get("HighGrade"))
    except (TypeError, ValueError):
        return True
    return low <= GRADE_HI and high >= GRADE_LO


def is_explicitly_remote(descriptor: dict) -> bool:
    """USAJobs keyword search can return onsite jobs whose description merely says 'remote'."""
    details = descriptor.get("UserArea", {}).get("Details", {})
    location = str(descriptor.get("PositionLocationDisplay") or "").lower()
    return bool(details.get("RemoteIndicator")) or "remote" in location or "anywhere in the u.s." in location


def remote_rows(items: list[dict]) -> list[dict]:
    """Map only support-titled postings that are explicitly remote by USAJobs metadata/location."""
    return [
        item_to_row(item)
        for item in items
        if is_explicitly_remote(item["MatchedObjectDescriptor"])
        and grade_ok(item["MatchedObjectDescriptor"])
        and support_titled(item["MatchedObjectDescriptor"].get("PositionTitle", ""))
    ]


def _yearly(amount: object, factor: int | None) -> str:
    """A USAJOBS range figure as the yearly amount every lane writes (hourly and other rates converted since
    2024-12-19; before that they were dropped)."""
    try:
        value = float(str(amount).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    return str(round(value * factor)) if factor else ""


def item_to_row(item: dict) -> dict:
    descriptor = item["MatchedObjectDescriptor"]
    details = descriptor.get("UserArea", {}).get("Details", {})
    location = str(descriptor.get("PositionLocationDisplay") or "")
    remote = bool(details.get("RemoteIndicator")) or "remote" in location.lower() or "anywhere in the u.s." in location.lower()
    pay = (descriptor.get("PositionRemuneration") or [{}])[0]
    interval = pay_interval(pay.get("RateIntervalCode"))
    factor = yearly_factor(interval) if interval else None
    return {
        "title": descriptor.get("PositionTitle", ""),
        "company": descriptor.get("OrganizationName", ""),
        "location": location,
        "date_posted": str(descriptor.get("PublicationStartDate") or "")[:10],
        "min_amount": _yearly(pay.get("MinimumRange"), factor),
        "max_amount": _yearly(pay.get("MaximumRange"), factor),
        "pay_interval": interval,
        "is_remote": str(remote).lower(),
        "job_url": descriptor.get("PositionURI", ""),
        "site": "usajobs",
        # The announcement text the search API already carries: duties, then the qualifications.
        "description": " ".join(
            part for part in (
                " ".join(str(d) for d in (details.get("MajorDuties") or [])),
                str(descriptor.get("QualificationSummary") or ""),
            ) if part
        ),
    }


def print_rows(rows: list[dict], label: str, total: int) -> None:
    print(f"\n=== {label}: {len(rows)} in-grade (of {total}) ===")
    for row in rows:
        salary = f"${row['min_amount']}-${row['max_amount']}" if row["min_amount"] else "salary interval not annual"
        print(f"  {row['title']} | {row['company']} | {row['location']} | {salary}")
        print(f"    {row['job_url']}")


def main() -> int:
    key = get_key()
    if not key:
        print("USAJOBS_API_KEY env var not set")
        return 1
    attempted = succeeded = 0
    rows: list[dict] = []
    sections = [  # Raleigh-Durham: 50 miles of Raleigh covers Durham, Wake and Orange
        ("Raleigh-Durham duty station", HOME_REGION, False),
        ("Greater Minneapolis duty station", SECOND_METRO, False),
        ("Remote-keyword, support-titled", {"Keyword": "remote"}, True),
    ]
    for label, params, filter_title in sections:
        attempted += 1
        try:
            items = search(key, **params)
            if filter_title:
                mapped = remote_rows(items)
            else:
                mapped = [item_to_row(item) for item in items if grade_ok(item["MatchedObjectDescriptor"])]
            succeeded += 1
            rows.extend(mapped)
            print_rows(mapped, label, len(items))
        except Exception as exc:
            print(f"\n=== {label}: FETCH FAILED — {type(exc).__name__}: {exc} ===")
    if rows:
        merged = merge_rows(rows, project_root())
        print(f"\nMerged {merged['added']} new rows ({merged['total']} total) -> {merged['path']}")
    print(f"Sections successful: {succeeded}/{attempted}")
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
