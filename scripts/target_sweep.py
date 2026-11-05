"""Target-company sweep: fetch public boards, print matches, merge canonical rows.

The watchlist below is an EXAMPLE configuration: North Carolina-headquartered employers, jobs in Raleigh-Durham
or fully remote. Each employer is read from its own public job
board (Greenhouse, Workday, UltiPro, Workable, Breezy, SmartRecruiters, iCIMS), and only IT-titled
jobs in Raleigh-Durham or remote are kept. Employers were chosen by checking
which employers in the daily pulls run a readable board.

Every request goes through scripts/rate_control.py, keyed by provider family and hostname, so a throttled
Workday tenant never stops Greenhouse or another employer's host. Greenhouse, Ashby, SmartRecruiters,
Workable and Breezy: 4 requests at once, one per host, 250 ms between starts. Workday, UltiPro and iCIMS:
2 at once, one per host, 1 s between starts (HTML-parsed iCIMS 1.5 s). A 429 pauses that host for its
Retry-After (15 minutes without one); a 401/403 blocks it for 6 hours; a timeout or 5xx is retried once
after 5-15 s; a 404/410 is not retried; three transient failures of one host within 10 minutes open a
30-minute circuit. One degraded host leaves the sweep degraded but usable; it fails only when no board
could be read. Cooldowns persist in dashboard/provider-state.json between runs.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.request

try:
    from scripts import rate_control
    from scripts.sweep_common import clean_description, in_area, merge_rows, project_root, relevant, remote_location
except ModuleNotFoundError:
    import rate_control
    from sweep_common import clean_description, in_area, merge_rows, project_root, relevant, remote_location

# EXAMPLE configuration: employers read straight from their own public job boards. The template names none;
# add your own, one entry per employer, in the shape shown in each comment.
GREENHOUSE: dict[str, str] = {}  # {"Example Co": "exampleco"} -> boards-api.greenhouse.io/v1/boards/exampleco
ASHBY: dict[str, str] = {}  # {"Example Co": "exampleco"}
WORKDAY: dict[str, tuple[str, str, str]] = {}  # company: (host, tenant, career site), e.g.
# {"Example University": ("exampleuniversity.wd1.myworkdayjobs.com", "exampleuniversity", "Careers")}
# A Workday board only answers searches, so a few IT searches stand in for "all jobs".
WORKDAY_SEARCHES = ("IT support", "help desk", "desktop", "systems administrator", "technician",
                    "network", "endpoint", "information technology")
ULTIPRO: dict[str, tuple[str, str]] = {}  # company: (tenant, board id)
WORKABLE: dict[str, str] = {}  # {"Example IT Services": "example-it-services"}
BREEZY: dict[str, str] = {}  # {"Example Benefits": "example-benefits"}
SMARTRECRUITERS: dict[str, str] = {}  # {"Example Systems": "ExampleSystems"}
ICIMS: dict[str, tuple[str, str]] = {}  # company: (portal prefix of <prefix>.icims.com, place used when a listing names none)
ICIMS_MAX_PAGES = 15  # 20 jobs a page
# Workday boards name buildings, not towns, for some sites. Known addresses:
WORKDAY_PLACES = {
    "Example Headquarters Building": "Apex, NC",
}


FAST_FAMILIES = {"greenhouse.io": "greenhouse", "ashbyhq.com": "ashby", "smartrecruiters.com": "smartrecruiters",
                 "workable.com": "workable", "breezy.hr": "breezy"}
SLOW_FAMILIES = {"myworkdayjobs.com": "workday", "ultipro.com": "ultipro", "icims.com": "icims"}
ICIMS_HTML_INTERVAL = 1.5
RATE_LIMIT_COOLDOWN = 15 * 60       # a 429 with no Retry-After
BLOCKED_COOLDOWN = 6 * 3600         # a 401/403: stop asking that host
TRANSIENT_RETRY_WAIT = (5, 15)


def provider_of(url: str) -> tuple[str, str, rate_control.Policy]:
    """(provider family, hostname, policy) for a board URL."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for table, policy in ((SLOW_FAMILIES, rate_control.ATS_SLOW), (FAST_FAMILIES, rate_control.ATS_FAST)):
        for suffix, family in table.items():
            if host == suffix or host.endswith("." + suffix):
                return family, host, policy
    return "other", host, rate_control.ATS_FAST


def controlled(url: str, send, interval: float | None = None):
    """send() under the host-aware limits. Raises ProviderUnavailable (host paused), or the failure itself."""
    family, host, policy = provider_of(url)
    gate, key = rate_control.get_gate(policy), f"{family}:{host}"
    for attempt in (1, 2):
        paused = gate.blocked(key)
        if paused:
            raise rate_control.ProviderUnavailable(paused.reason, paused.cooldown_until)
        try:
            with gate.slot(host, interval):
                result = send()
        except Exception as exc:  # noqa: BLE001 -- classified; the original is re-raised unless a control applies
            failure = rate_control.classify_exception(exc, gate.runtime.now())
            if failure.kind == "rate_limited":
                seconds = failure.retry_after if failure.retry_after is not None else RATE_LIMIT_COOLDOWN
                gate.cooldown(key, seconds=seconds, reason="http_429", error=str(exc))
            elif failure.kind == "auth":
                gate.cooldown(key, seconds=BLOCKED_COOLDOWN, reason=f"blocked_{failure.status}", error=str(exc))
            elif failure.kind == "transient":
                if gate.transient(key, failure.reason, str(exc)):
                    raise  # circuit open: no retry against this host
                if attempt == 1:
                    gate.runtime.sleep(gate.runtime.uniform(*TRANSIENT_RETRY_WAIT))
                    continue
            elif failure.kind != "not_found":
                gate.failure(key, failure.reason, str(exc))
            raise
        gate.success(key)
        return result
    raise AssertionError("unreachable")


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    def send():
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    return controlled(url, send)


def post_json(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"})

    def send():
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    return controlled(url, send)


def get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"})

    def send():
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", "ignore")
    return controlled(url, send, interval=ICIMS_HTML_INTERVAL)


# iCIMS portals have no public JSON. The listing page repeats one block per job: a header with
# "Posting City/State" (some boards) and a "Posted Date" span (full timestamp in its title), the title
# anchor, then a field list that may carry "Job Locations" ("US-NC-Durham") and "Location : Name".
# Boards differ in which of these they show. Whitespace is collapsed before matching.
_ICIMS_ANCHOR = re.compile(
    r'<a href="(https://[^"]+/jobs/(\d+)/[^"?]+/job)[^"]*" class="iCIMS_Anchor"[^>]*>.*?<h3\s*>(.*?)</h3>', re.S)
_ICIMS_POSTED = re.compile(r'Posted Date</span><span title="([^"]+)"')
_ICIMS_CITY = re.compile(r'Posting City/State</span><span\s*>([^<]*)</span>')


def _icims_field(chunk: str, label: str) -> str:
    found = re.search(re.escape(label) + r'</span></dt><dd class="iCIMS_JobHeaderData"><span\s*>([^<]*)</span>', chunk)
    return html.unescape(found.group(1)).strip() if found else ""


def icims_jobs(page: str) -> list[dict]:
    """The jobs on one iCIMS listing page: id, url, title, posted (raw), location, place."""
    text = re.sub(r"\s+", " ", re.sub(r">\s+<", "><", page))
    anchors = list(_ICIMS_ANCHOR.finditer(text))
    jobs = []
    for index, match in enumerate(anchors):
        before = text[anchors[index - 1].end() if index else 0:match.start()]
        after = text[match.end():anchors[index + 1].start() if index + 1 < len(anchors) else len(text)]
        posted = _ICIMS_POSTED.findall(before)
        city = _ICIMS_CITY.findall(before)
        jobs.append({"id": match.group(2), "url": match.group(1), "title": html.unescape(match.group(3)).strip(),
                     "posted": posted[-1] if posted else "", "city_state": html.unescape(city[-1]).strip() if city else "",
                     "location": _icims_field(after, "Job Locations"), "place": _icims_field(after, "Location : Name")})
    return jobs


def fetch_icims(portal: str) -> list[dict]:
    found: dict[str, dict] = {}
    for page in range(ICIMS_MAX_PAGES):
        jobs = icims_jobs(get_text(f"https://{portal}.icims.com/jobs/search?ss=1&pr={page}&in_iframe=1"))
        new = [job for job in jobs if job["id"] not in found]
        if not new:  # past the last page iCIMS repeats it, or returns none
            break
        for job in new:
            found[job["id"]] = job
    for job in found.values():
        # The framed job view (?in_iframe=1, the same host and pacing as the listing pages) carries the posting text,
        # which the listing lacks (2024-12-20). IT titles only, about ten a night across the three boards.
        if not relevant(job.get("title")):
            continue
        try:
            job["_description"] = icims_description(get_text(job["url"] + "?in_iframe=1"))
        except Exception:  # keep the row without text
            continue
    return list(found.values())


def icims_description(page: str) -> str:
    """The posting text of an iCIMS job view: everything from the job-content block on, tags and scripts removed."""
    marker = re.search(r'<[a-z]+[^>]*class="[^"]*iCIMS_(?:JobContent|InfoMsg_Job|Expandable_Container)', page, re.I)
    body = page[marker.start():] if marker else page
    return clean_description(re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", body))


def icims_posted(text: str) -> str:
    for pattern in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def icims_location(job: dict, home: str = "") -> str:
    """'Posting City/State' as is; else 'US-NC-Chapel Hill' -> 'Chapel Hill, NC', a bare 'US-NC' plus the
    location name; else the board's home place."""
    if job.get("city_state"):
        return str(job["city_state"])
    parts = [p.strip() for p in str(job.get("location") or "").split("-")]
    state = parts[1] if len(parts) > 1 else ""
    city = "-".join(parts[2:]) if len(parts) > 2 else (job.get("place") or "")
    return ", ".join(x for x in (city, state) if x) or str(job.get("location") or "") or home


def icims_rows(company: str, portal: str, jobs: list[dict], home: str = "") -> list[dict]:
    return [_row(job["title"], company, icims_location(job, home), icims_posted(job.get("posted", "")), False,
                 job["url"], f"icims:{portal}", description=str(job.get("_description") or ""))
            for job in jobs if relevant(job.get("title"))]


def _row(title: str, company: str, location: str, posted: str, remote: bool, url: str, site: str,
         description: str = "") -> dict:
    if remote:
        location = remote_location(location)  # every lane says remote in the location (2024-12-19)
    return {"title": title, "company": company, "location": location, "date_posted": posted,
            "min_amount": "", "max_amount": "", "is_remote": str(bool(remote) or "remote" in location.lower()).lower(),
            "job_url": url, "site": site, "description": description}


def workday_posted(text: object, today: dt.date) -> str:
    """Workday lists say "Posted Today", "Posted Yesterday", "Posted 11 Days Ago" or "Posted 30+ Days Ago"."""
    t = str(text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*days?", t)
        if not m:
            return ""
        days = int(m.group(1)) + (1 if "+" in t else 0)  # "30+" is older than 30
    return (today - dt.timedelta(days=days)).isoformat()


def workday_rows(company: str, host: str, site: str, jobs: list[dict], today: dt.date) -> list[dict]:
    return [_row(j.get("title", ""), company, WORKDAY_PLACES.get(j.get("locationsText", ""), j.get("locationsText", "")),
                 workday_posted(j.get("postedOn"), today),
                 False, f"https://{host}/en-US/{site}{j.get('externalPath', '')}", f"workday:{host.split('.')[0]}",
                 description=str(j.get("_description") or ""))
            for j in jobs if relevant(j.get("title"))]


def fetch_workday(host: str, tenant: str, site: str) -> list[dict]:
    found: dict[str, dict] = {}
    for term in WORKDAY_SEARCHES:
        try:
            out = post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                            {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term})
        except Exception:
            if found:  # throttled or failing part-way: keep what was read (the host's state records why)
                break
            raise
        for job in out.get("jobPostings") or []:
            found.setdefault(job.get("externalPath") or job.get("title"), job)
    for job in found.values():
        # One detail request per IT-titled job (2024-12-20): the posting text, which the listing lacks and which the
        # judge could otherwise only buy through Bright Data, where Workday pages never render (46 no-text verdicts,
        # 150 of 150 daily units spent on 10/23), and the places behind "7 Locations". Free, same host, about 20 a night.
        if not (relevant(job.get("title")) and job.get("externalPath")):
            continue
        try:
            info = get_json(f"https://{host}/wday/cxs/{tenant}/{site}{job['externalPath']}").get("jobPostingInfo") or {}
        except Exception:  # keep the row without text; the judge's host rule leaves it Unscored without a paid fetch
            continue
        job["_description"] = clean_description(str(info.get("jobDescription") or ""))
        if re.fullmatch(r"\d+ Locations", str(job.get("locationsText") or "")):
            places = [info.get("location"), *(info.get("additionalLocations") or [])]
            job["locationsText"] = "; ".join(dict.fromkeys(p for p in places if p)) or job["locationsText"]
    return list(found.values())


def ultipro_rows(company: str, tenant: str, board: str, jobs: list[dict]) -> list[dict]:
    rows = []
    for j in jobs:
        if not relevant(j.get("Title")):
            continue
        address = ((j.get("Locations") or [{}])[0].get("Address") or {})
        place = ", ".join(x for x in (address.get("City"), (address.get("State") or {}).get("Code")) if x)
        rows.append(_row(j.get("Title", ""), company, place, str(j.get("PostedDate") or "")[:10], False,
                         f"https://recruiting.ultipro.com/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={j.get('Id', '')}",
                         f"ultipro:{tenant}"))
    return rows


def fetch_ultipro(tenant: str, board: str) -> list[dict]:
    body = {"opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "", "OrderBy": [
        {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}], "Filters": [
        {"t": "TermsSearchFilterDto", "fieldName": n, "extra": None, "values": []} for n in (4, 5, 6)]},
        "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [],
                          "hasNoLicenses": False, "SkippedSkills": []}}
    return post_json(f"https://recruiting.ultipro.com/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults",
                     body).get("opportunities") or []


def workable_rows(company: str, slug: str, jobs: list[dict]) -> list[dict]:
    return [_row(j.get("title", ""), company, ", ".join(x for x in (j.get("city"), j.get("state")) if x),
                 str(j.get("published_on") or "")[:10], bool(j.get("telecommuting")), j.get("url", ""), f"workable:{slug}",
                 description=clean_description(" ".join(str(j.get(k) or "") for k in ("description", "requirements", "benefits"))))
            for j in jobs if relevant(j.get("title"))]


def breezy_rows(company: str, slug: str, jobs: list[dict]) -> list[dict]:
    return [_row(j.get("name", ""), company, (j.get("location") or {}).get("name", ""),
                 str(j.get("published_date") or "")[:10], bool((j.get("location") or {}).get("is_remote")),
                 j.get("url", ""), f"breezy:{slug}", description=clean_description(str(j.get("description") or "")))
            for j in jobs if relevant(j.get("name"))]


def fetch_smartrecruiters(ident: str) -> list[dict]:
    """The company's postings, each IT-titled one with its text from the posting detail (2024-12-20): the jobAd
    sections jobDescription, qualifications and additionalInformation. One request per IT posting, free."""
    postings = get_json(f"https://api.smartrecruiters.com/v1/companies/{ident}/postings?limit=100").get("content") or []
    for posting in postings:
        if not (relevant(posting.get("name")) and posting.get("id")):
            continue
        try:
            detail = get_json(f"https://api.smartrecruiters.com/v1/companies/{ident}/postings/{posting['id']}")
        except Exception:  # keep the row without text
            continue
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        posting["_description"] = clean_description(" ".join(
            str((sections.get(k) or {}).get("text") or "") for k in ("jobDescription", "qualifications", "additionalInformation")))
    return postings


def smartrecruiters_rows(company: str, ident: str, jobs: list[dict]) -> list[dict]:
    rows = []
    for j in jobs:
        if not relevant(j.get("name")):
            continue
        loc = j.get("location") or {}
        rows.append(_row(j.get("name", ""), company, ", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
                         str(j.get("releasedDate") or "")[:10], bool(loc.get("remote")),
                         f"https://jobs.smartrecruiters.com/{ident}/{j.get('id', '')}", f"smartrecruiters:{ident}",
                         description=str(j.get("_description") or "")))
    return rows


def greenhouse_rows(company: str, token: str, jobs: list[dict]) -> list[dict]:
    rows = []
    for job in jobs:
        if not relevant(job.get("title")):
            continue
        location = (job.get("location") or {}).get("name", "")
        rows.append({
            "title": job.get("title", ""), "company": company, "location": location,
            "date_posted": str(job.get("updated_at") or "")[:10],
            "min_amount": "", "max_amount": "",
            "is_remote": str("remote" in location.lower()).lower(),
            "job_url": job.get("absolute_url", ""), "site": f"greenhouse:{token}",
            "description": job.get("content", "") or "",   # ?content=true; HTML, cleaned on merge
        })
    return rows


def ashby_rows(company: str, token: str, jobs: list[dict]) -> list[dict]:
    rows = []
    for job in jobs:
        if not relevant(job.get("title")):
            continue
        location = str(job.get("location") or "")
        remote = job.get("isRemote")
        if remote is None:
            remote = "remote" in location.lower()
        rows.append({
            "title": job.get("title", ""), "company": company, "location": location,
            "date_posted": "", "min_amount": "", "max_amount": "",
            "is_remote": str(bool(remote)).lower(), "job_url": job.get("jobUrl", ""),
            "site": f"ashby:{token}",
            "description": job.get("descriptionPlain") or job.get("descriptionHtml") or "",
        })
    return rows


def atlassian_rows(postings: list[dict]) -> list[dict]:
    rows = []
    for post in postings:
        if not relevant(post.get("title")):
            continue
        location = ", ".join(post.get("locations") or [])
        rows.append({
            "title": post.get("title", ""), "company": "Atlassian", "location": location,
            "date_posted": "", "min_amount": "", "max_amount": "",
            "is_remote": str("remote" in location.lower()).lower(),
            "job_url": post.get("applyUrl", ""), "site": "atlassian",
        })
    return rows


def boards(today: dt.date) -> list[tuple[str, object, object]]:
    """(company, fetch(), to_rows(jobs)) for every watchlist employer."""
    out: list[tuple[str, object, object]] = []
    for company, token in GREENHOUSE.items():
        out.append((company, lambda t=token: get_json(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true").get("jobs", []),
                    lambda jobs, c=company, t=token: greenhouse_rows(c, t, jobs)))
    for company, token in ASHBY.items():
        out.append((company, lambda t=token: get_json(f"https://api.ashbyhq.com/posting-api/job-board/{t}").get("jobs", []),
                    lambda jobs, c=company, t=token: ashby_rows(c, t, jobs)))
    for company, (host, tenant, site) in WORKDAY.items():
        out.append((company, lambda h=host, t=tenant, s=site: fetch_workday(h, t, s),
                    lambda jobs, c=company, h=host, s=site: workday_rows(c, h, s, jobs, today)))
    for company, (tenant, board) in ULTIPRO.items():
        out.append((company, lambda t=tenant, b=board: fetch_ultipro(t, b),
                    lambda jobs, c=company, t=tenant, b=board: ultipro_rows(c, t, b, jobs)))
    for company, slug in WORKABLE.items():
        out.append((company, lambda s=slug: get_json(f"https://apply.workable.com/api/v1/widget/accounts/{s}?details=true").get("jobs", []),
                    lambda jobs, c=company, s=slug: workable_rows(c, s, jobs)))
    for company, slug in BREEZY.items():
        out.append((company, lambda s=slug: get_json(f"https://{s}.breezy.hr/json?verbose=true"),
                    lambda jobs, c=company, s=slug: breezy_rows(c, s, jobs)))
    for company, ident in SMARTRECRUITERS.items():
        out.append((company, lambda i=ident: fetch_smartrecruiters(i),
                    lambda jobs, c=company, i=ident: smartrecruiters_rows(c, i, jobs)))
    for company, (portal, home) in ICIMS.items():
        out.append((company, lambda p=portal: fetch_icims(p),
                    lambda jobs, c=company, p=portal, h=home: icims_rows(c, p, jobs, h)))
    return out


def provider_summaries(summaries: dict) -> dict:
    """Per-host gate summaries folded into one entry per provider family (ok, degraded or worst state)."""
    families: dict[str, list[dict]] = {}
    for key, summary in summaries.items():
        families.setdefault(key.split(":", 1)[0], []).append(summary)
    out = {}
    for family, items in families.items():
        bad = [item for item in items if item["state"] != rate_control.OK]
        entry = {"state": rate_control.OK, "reason": "",
                 "consecutive_degraded_runs": max(item["consecutive_degraded_runs"] for item in items)}
        if bad:
            entry["state"] = bad[0]["state"] if len(bad) == len(items) else rate_control.DEGRADED
            entry["reason"] = bad[0]["reason"] or ""
            until = [item["cooldown_until"] for item in bad if item.get("cooldown_until")]
            if until:
                entry["cooldown_until"] = max(until)
            if len(items) > 1:
                entry["reason"] = f"{len(bad)} of {len(items)} hosts: {entry['reason']}"[:160]
        out[family] = entry
    return out


def main() -> int:
    rows: list[dict] = []
    attempted = succeeded = 0
    targets = boards(dt.date.today())
    if not targets:
        print("No target employers configured: the watchlist in scripts/target_sweep.py is empty.")
        return 0
    for company, fetch, to_rows in targets:
        attempted += 1
        try:
            jobs = fetch()
            relevant_rows = to_rows(jobs)
            hits = list({row["job_url"]: row for row in relevant_rows if in_area(row)}.values())  # one job, one line
            succeeded += 1
            rows.extend(hits)
            print(f"\n=== {company} ({len(jobs)} roles read, {len(relevant_rows)} IT-relevant, "
                  f"{len(hits)} in Raleigh-Durham or remote) ===")
            for row in hits:
                print(f"  {row['title']} | {row['location'] or '?'} | posted {row['date_posted'] or '?'} | {row['job_url']}")
        except rate_control.ProviderUnavailable as exc:
            print(f"\n=== {company}: SKIPPED — host paused ({rate_control.sanitize(exc.reason)}"
                  f"{' until ' + exc.cooldown_until if exc.cooldown_until else ''}) ===")
        except Exception as exc:
            print(f"\n=== {company}: FETCH FAILED — {type(exc).__name__}: {rate_control.sanitize(exc)} ===")

    if rows:
        merged = merge_rows(rows, project_root())
        print(f"\nMerged {merged['added']} new rows ({merged['total']} total) -> {merged['path']}")
    print(f"Sections successful: {succeeded}/{attempted}")
    providers = provider_summaries(rate_control.finish_shared_runs())
    if any(p["state"] != rate_control.OK for p in providers.values()):
        print("Target sweep degraded: some boards were paused, blocked or failing; the rest were read.")
    print(rate_control.status_line(providers))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
