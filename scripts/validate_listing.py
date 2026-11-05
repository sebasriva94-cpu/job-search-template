"""Listing validation for the needs-validation queue: is it open, where is it, what does it pay,
and is there a knockout? Turns a raw feed row into an "apply now", "review", "skip" or "dead" verdict.

Fetch chain (first that works):
  1. ATS public APIs for Greenhouse, Ashby and Lever URLs (structured, free).
  2. Bright Data Web Unlocker (zone in BRIGHTDATA_UNLOCKER_ZONE, token in BRIGHTDATA_API_TOKEN) for LinkedIn, Indeed
     and employer sites that block plain fetches. Verified 2024-10-29: returns the full LinkedIn
     guest page including the salary block and the "No longer accepting applications" banner.
  3. Plain GET (employer sites that do not block).
  4. Scrapling direct GET (LinkedIn guest pages only, and only while Bright Data is unavailable;
     source "scrapling-direct", LINKEDIN_DIRECT budget: one fetch per queue row, 4 s apart, paused
     an hour on any refusal). A per-site Bright Data decline is never routed around, and Indeed is
     never fetched directly (it answers 401).

Bright Data runs under shared rate control (scripts/rate_control.py): at most 2 requests at once and
one new request every 1.5 s, 60 requests per render and 150 a day, a 15-minute cooldown on a 429 (or its
Retry-After), 24 hours on a quota (402) or auth failure, one 20-40 s retry for a 5xx or network error.
While Bright Data is cooling down or out of budget a LinkedIn or Indeed page is never fetched directly
(that is what gets an address blocked): the row stays "could not check" and lands in review, never in
Apply now. An employer's own page keeps its single direct fallback.

An Indeed row is checked on the employer's own posting instead (the feed's job_url_direct): Indeed
pages time out through the Unlocker (2024-12-09). That page counts only when it shows the job.

Nothing here judges fit; that stays with a human. It only proves the facts a human would
otherwise have to open the listing to see.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts import rate_control
    from scripts.sweep_common import (SECOND_METRO_IN_TEXT, PER_YEAR, HOME_REGION_IN_TEXT, is_local, is_staffing_agency,
                                      yearly_factor)
except ModuleNotFoundError:
    import rate_control
    from sweep_common import (SECOND_METRO_IN_TEXT, PER_YEAR, HOME_REGION_IN_TEXT, is_local, is_staffing_agency,
                              yearly_factor)

UNLOCKER_URL = "https://api.brightdata.com/request"
UNLOCKER_ZONE = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "web_unlocker")
FLOOR = 70_000          # example pay floor; pay that tops out below this is flagged
REVIEW_BAND = 65_000    # between this and the floor: show, but flag the pay; below it: skip
MAX_YEARS = 8           # a posting asking for this many years or more is out of level
HOURS_PER_YEAR = PER_YEAR["hourly"]   # the one pay table lives in sweep_common (2024-12-19)
BD_KEY = rate_control.BRIGHTDATA_KEY
BD_RETRY_WAIT = (20, 40)             # seconds, jittered: the one retry of a 5xx / network error
RETRY_PAUSE = 5         # seconds before the one retry after a timeout, a DNS or connection failure, or a server error

# The home region is Raleigh-Durham (sweep_common.is_home_region). ATS APIs sometimes
# report only a work model ("Hybrid") while the posting body says "Available Locations: Hybrid -
# Rougemont, NC". A Raleigh-Durham place followed by NC or North Carolina is the cue (HOME_REGION_IN_TEXT); a
# Greater Minneapolis place followed by MN or Minnesota counts too (SECOND_METRO_IN_TEXT).
CLOSED_MARKERS = (
    "no longer accepting applications", "this job is no longer available", "job has expired",
    "position has been filled", "this job is closed", "job not found", "posting is closed",
    "this job has been closed", "no longer available",
)
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|lead|principal|staff|director|manager|head of|architect|vp|vice president)\b", re.I)
JUNIOR_TITLE = re.compile(
    r"\b(junior|jr\.?|entry[- ]level|intern(ship)?|trainee|new grad(uate)?|apprentice|level ?1|l1|tier ?1|t1)\b"
    r"|\b(technician|tech|specialist|analyst|administrator|engineer|support)\s+i\b",
    re.I,
)
# The owner's level band (2024-12-09): Middle only (II / Tier 2, or no level in the title). Level III, Tier 3
# and up are out, like senior titles; "Level II to III" still counts as in band.
UPPER_TITLE = re.compile(
    r"\b(level|tier)\s*(3|4|iii|iv)\b|\b(l3|l4|t3)\b"
    r"|\b(technician|tech|specialist|analyst|administrator|engineer|support)\s+(iii|iv|3|4)\b",
    re.I,
)
CLEARANCE = re.compile(
    r"(?:active|current)\s+(?:\w+\s+){0,3}clearance|secret clearance|top secret|ts/sci|"
    r"security clearance\s+(?:is\s+)?required|must (?:hold|possess)\s+(?:an?\s+)?(?:active\s+)?(?:\w+\s+)?clearance",
    re.I,
)
# A years figure only counts when it is a requirement: "N years of experience", "N+ years' experience",
# "N years of hands-on experience", "N years in IT support", "N years working in". Company blurbs such as
# "Best Places to Work since 2017 (that's 9 years!)" used to be read as an experience requirement
# (one posting was auto-skipped as "asks for 9+ years" when the posting asked for 2+).
YEARS = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:years|yrs)['’]?\s+(?:of\s+)?(?:[\w-]+\s+){0,3}?(?:experience|exp\b|working\b|supporting\b|in\b(?!\s+a\s+row))",
    re.I,
)
REMOTE_WORDS = re.compile(r"\b(remote|work from home|telecommute|wfh)\b", re.I)
ONSITE_WORDS = re.compile(r"\b(on-?site|in-?office|in person|hybrid)\b", re.I)
PAY_RANGE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?)\s*(k)?\s*(?:/\s*(yr|year|hr|hour))?\s*(?:-|–|—|to)\s*\$?\s?"
    r"(\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?)\s*(k)?\s*(?:/\s*(yr|year|hr|hour))?",
    re.I,
)
# One hourly figure with no range ("$25/hr", "$30 per hour", "$22 an hour"); marks the pay as hourly only.
# Two or three digits, so a "$2.00/hour shift differential" does not count.
HOURLY_PAY = re.compile(r"\$\s?\d{2,3}(?:\.\d{1,2})?\s*(?:/\s*(?:hr|hour)\b|per\s+hour\b|an\s+hour\b|hourly\b)", re.I)
# The owner's rule (2024-12-09): staffing agencies are shown like any employer, flagged only when the pay is hourly.
AGENCY_HOURLY = "staffing agency with hourly pay, likely a contract"
# The owner's key tools: the checker reports which of these a posting mentions and the dashboard ranks by the
# count. One file owns the tools and their spellings.
KEY_TOOLS = ("Intune", "Jamf", "Okta", "Entra", "Active Directory", "ServiceNow", "PowerShell")
# Other names postings use for the key tools. "Active Directory" inside "Azure Active Directory" is
# Entra, not the on-premises directory.
SKILL_ALIASES = {
    "entra": (r"entra", r"azure ad", r"azure active directory"),
    "active directory": (r"(?<!azure )active directory",),
}
APPLICANTS = re.compile(r"(over\s+)?(\d{1,4})\s*\+?\s*applicants", re.I)
POSTED = re.compile(r"\b(\d{1,2}|an?)\s+(minute|hour|day|week|month)s?\s+ago\b", re.I)
MAX_POSTING_AGE_DAYS = 21  # the owner's rule (2024-12-09): only listings posted less than 3 weeks ago
_AGO_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}


def posting_age_days(posted: object, today: dt.date | None = None) -> int | None:
    """Days since posting, from an ISO date or "3 weeks ago" text; None when unknown."""
    text = str(posted or "").strip()
    try:
        return ((today or dt.date.today()) - dt.date.fromisoformat(text[:10])).days
    except ValueError:
        pass
    m = POSTED.search(text)
    if not m:
        return None
    count = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
    return count * _AGO_DAYS[m.group(2).lower()]


# --- fetching -----------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "final_url": r.url, "html": r.read(600_000).decode("utf-8", "ignore"), "error": None}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "final_url": url, "html": "", "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "final_url": url, "html": "", "error": f"{type(e).__name__}: {e}"}


def _api_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def unlocker_get(url: str, timeout: int = 60, token: str | None = None) -> dict:
    """Bright Data Web Unlocker: returns the rendered page for sites that block plain fetches."""
    token = token or os.environ.get("BRIGHTDATA_API_TOKEN")
    if not token:
        return {"status": None, "final_url": url, "html": "", "error": "BRIGHTDATA_API_TOKEN missing"}
    body = json.dumps({"zone": UNLOCKER_ZONE, "url": url, "format": "raw"}).encode()
    req = urllib.request.Request(
        UNLOCKER_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            page = {"status": r.status, "final_url": url, "html": r.read(800_000).decode("utf-8", "ignore"), "error": None}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "final_url": url, "html": "", "error": f"unlocker HTTP {e.code}",
                "retry_after": e.headers.get("Retry-After") if getattr(e, "headers", None) else None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "final_url": url, "html": "", "error": f"{type(e).__name__}: {rate_control.sanitize(e)}"}
    # Bright Data declines some sites with a 200 notice ("Residential Failed (bad_endpoint) ... robots.txt",
    # a Workday tenant on 2024-12-09) that used to be read as an open listing; the caller falls back to a plain GET.
    if page["html"].lstrip().startswith("Residential Failed"):
        return {**page, "html": "", "error": "Bright Data declined the site"}
    return page


def _bd_unavailable(url: str, reason: str) -> dict:
    return {"status": None, "final_url": url, "html": "", "error": rate_control.sanitize(reason), "bd_unavailable": True}


LINKEDIN_DIRECT_KEY = "linkedin_direct"
LINKEDIN_DIRECT_COOLDOWN = 3600  # a refusal (401/403/429) pauses the fallback for an hour, per the compliance rule


def scrapling_get(url: str, timeout: int = 30) -> dict:
    """One direct guest-page fetch through scrapling's HTTP tier (a browser fingerprint, no proxy, no
    login). 2024-12-23 spike: LinkedIn guest pages answer 200 with the full listing in under a second;
    Indeed answers 401, which is why Indeed stays never-direct."""
    try:
        from scrapling import Fetcher
    except ImportError:
        return {"status": None, "final_url": url, "html": "", "error": "scrapling not installed"}
    try:
        page = Fetcher.get(url, timeout=timeout)
        raw = page.body if isinstance(page.body, str) else page.body.decode("utf-8", "ignore")
        return {"status": page.status, "final_url": url, "html": raw, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "final_url": url, "html": "", "error": f"{type(e).__name__}: {e}"}


def controlled_scrapling_get(url: str, timeout: int = 30) -> dict:
    """scrapling_get under the shared LinkedIn-direct limits (LINKEDIN_DIRECT): capped at one fetch per
    queue row per run, paced, and paused for an hour on any refusal. The fallback exists only for a
    Bright Data outage, so a paused gate reads like bd_unavailable: nothing is fetched and the row
    stays 'could not check', never guessed."""
    gate = rate_control.get_gate(rate_control.LINKEDIN_DIRECT)
    admitted = gate.admit(LINKEDIN_DIRECT_KEY)
    if not admitted.ok:
        where = f" until {admitted.cooldown_until}" if admitted.cooldown_until else ""
        return _bd_unavailable(url, f"LinkedIn direct fetch paused ({admitted.reason}{where})")
    with gate.slot("www.linkedin.com"):
        page = scrapling_get(url, timeout=timeout)
    status = page.get("status")
    if status in (401, 403, 429):
        gate.cooldown(LINKEDIN_DIRECT_KEY, seconds=LINKEDIN_DIRECT_COOLDOWN, reason=f"http_{status}",
                      error=page.get("error") or "")
    elif isinstance(status, int) and (status < 400 or status in (404, 410)):
        gate.success(LINKEDIN_DIRECT_KEY)
    else:
        gate.failure(LINKEDIN_DIRECT_KEY, f"http_{status}" if status else "network_error", page.get("error") or "")
    return page


def controlled_unlocker_get(url: str, timeout: int = 60, retry_transient: bool = True) -> dict:
    """unlocker_get under the shared Bright Data limits (see the module docstring).

    retry_transient=False skips the one retry of a timeout or network error: the judge's text fetch
    uses it (2024-12-18), because a page that gave no answer in 25 s rarely answers on the second try
    and the retry turned every slow row into two minutes.

    The page carries "bd_unavailable": True when nothing could be read because Bright Data is cooling
    down, out of budget or failing, which tells fetch_listing not to fall back to a direct fetch of a
    LinkedIn or Indeed page.
    """
    gate = rate_control.brightdata_gate()
    for attempt in (1, 2):
        admitted = gate.admit(BD_KEY)
        if not admitted.ok:
            where = f" until {admitted.cooldown_until}" if admitted.cooldown_until else ""
            return _bd_unavailable(url, f"Bright Data paused ({admitted.reason}{where})")
        with gate.slot():
            page = unlocker_get(url, timeout=timeout)
        status = page.get("status")
        if status in (404, 410) or (status is None and not page.get("error")) or (isinstance(status, int) and status < 400):
            gate.success(BD_KEY)  # includes "Bright Data declined the site": a per-site answer, not a provider fault
            return page
        failure = (rate_control.classify_status(status, page.get("retry_after"), gate.runtime.now())
                   if status else rate_control.Failure("transient", "network_error"))
        error = page.get("error") or ""
        if not rate_control.apply_brightdata_failure(gate, failure, error):
            if failure.kind == "transient" and attempt == 1 and retry_transient:
                gate.runtime.sleep(gate.runtime.uniform(*BD_RETRY_WAIT))
                continue
            gate.failure(BD_KEY, failure.reason, error)
        return {**page, "bd_unavailable": True, "error": rate_control.sanitize(page.get("error") or failure.reason)}
    return _bd_unavailable(url, "Bright Data unavailable")


def ats_fetch(url: str, timeout: int = 25) -> dict | None:
    """Structured fetch through a public ATS API; None when the URL is not an ATS we know."""
    parsed = urllib.parse.urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path
    try:
        if "greenhouse.io" in host:
            m = re.search(r"/([^/]+)/jobs/(\d+)", path)
            if not m:
                return None
            slug, job_id = m.group(1), m.group(2)
            try:
                j = _api_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?content=true", timeout)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"source": "greenhouse", "closed": True, "text": "", "title": "", "location": "", "posted": "", "html": ""}
                raise
            text = _strip_html(html_lib.unescape(j.get("content", "")))
            return {"source": "greenhouse", "closed": False, "text": text, "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""), "posted": str(j.get("updated_at", ""))[:10], "html": ""}
        if "ashbyhq.com" in host:
            m = re.search(r"/([^/]+)/([0-9a-f-]{20,})", path)
            if not m:
                return None
            slug, job_id = m.group(1), m.group(2)
            jobs = _api_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout).get("jobs", [])
            hit = next((j for j in jobs if j.get("id") == job_id or job_id in (j.get("jobUrl") or "")), None)
            if not hit:
                return {"source": "ashby", "closed": True, "text": "", "title": "", "location": "", "posted": "", "html": ""}
            comp = ((hit.get("compensation") or {}).get("compensationTierSummary") or "")
            text = (hit.get("descriptionPlain") or _strip_html(hit.get("descriptionHtml") or "")) + ("\n" + comp if comp else "")
            return {"source": "ashby", "closed": False, "text": text, "title": hit.get("title", ""),
                    "location": hit.get("location", "") + (" (remote)" if hit.get("isRemote") else ""),
                    "posted": str(hit.get("publishedAt", ""))[:10], "html": ""}
        if "lever.co" in host:
            m = re.search(r"/([^/]+)/([0-9a-f-]{20,})", path)
            if not m:
                return None
            slug, job_id = m.group(1), m.group(2)
            try:
                j = _api_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}", timeout)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"source": "lever", "closed": True, "text": "", "title": "", "location": "", "posted": "", "html": ""}
                raise
            cats = j.get("categories") or {}
            return {"source": "lever", "closed": False, "text": j.get("descriptionPlain", "") or _strip_html(j.get("description", "")),
                    "title": j.get("text", ""), "location": f"{cats.get('location', '')} {cats.get('commitment', '')}",
                    "posted": "", "html": ""}
    except Exception as e:  # noqa: BLE001 -- an ATS hiccup falls through to the page fetch
        return {"source": "ats-error", "error": f"{type(e).__name__}: {e}"}
    return None


def _transient(page: dict) -> bool:
    """A timeout, a DNS or connection failure (no answer at all), or a server error (5xx): worth one more try."""
    status = page.get("status")
    return (status is None and bool(page.get("error"))) or (isinstance(status, int) and status >= 500)


def fetch_listing(url: str, timeout: int = 25) -> dict:
    """Best available view of a listing: {source, closed, text, html, title, location, posted, status, error}."""
    if not url:
        return {"source": "none", "closed": False, "text": "", "html": "", "title": "", "location": "", "posted": "", "status": None, "error": "missing URL"}
    ats = ats_fetch(url, timeout)
    if ats and ats.get("source") != "ats-error":
        ats.setdefault("status", 200)
        ats.setdefault("error", None)
        return ats
    use_bd = bool(os.environ.get("BRIGHTDATA_API_TOKEN"))
    source = "unlocker" if use_bd else "direct"
    if use_bd:
        page = controlled_unlocker_get(url, timeout=max(timeout, 60))  # retries its own 5xx / network errors
        if page.get("error") or not page.get("html"):
            if _is_linkedin(url):
                if page.get("bd_unavailable"):  # a provider outage only; a per-site decline is never routed around
                    page = controlled_scrapling_get(url)
                    source = "scrapling-direct"
            elif not _is_indeed(url):  # Indeed is never fetched directly (401); employer sites keep the one plain fallback
                page = _http_get(url, timeout)
                source = "direct"
    else:
        for attempt in range(2):  # a hiccup used to mean "could not check" until the next morning (2024-12-09)
            page = _http_get(url, timeout)
            if attempt or not _transient(page):
                break
            time.sleep(RETRY_PAUSE)
    raw = page.get("html", "")
    listing = {
        "source": source,
        "closed": False, "html": raw, "text": _strip_html(raw), "title": "", "location": "", "posted": "",
        "status": page.get("status"), "error": page.get("error"),
    }
    if listing["status"] in (404, 410):  # the posting is gone
        return {**listing, "closed": True, "html": "", "text": "", "error": None}
    blocked = unreadable_reason(url, listing["status"], page.get("final_url") or url, listing["text"], raw)
    if blocked:  # nothing was read, so nothing may be decided from it (validate() says "could not check")
        return {**listing, "html": "", "text": "", "error": listing["error"] or blocked}
    return listing


def unreadable_reason(url: str, status, final_url: str, text: str, raw: str = "") -> str | None:
    """Why a fetched page is not the listing (a refusal, a sign-in page or an empty page), else None.

    2024-12-09: with the Bright Data token expired, LinkedIn answered 429 or a sign-in page and
    Indeed 401. Those empty pages were judged open from the title alone and shown as "Apply now".
    """
    if status in (401, 403, 429):
        return f"blocked (HTTP {status})"
    if isinstance(status, int) and status >= 500:  # the site's own server failed; nothing refused us
        return f"server error (HTTP {status})"
    low = text.lower()
    if "linkedin.com" in urllib.parse.urlsplit(final_url or url).netloc.lower():
        if "/authwall" in final_url or "/login" in final_url or not (
                "seniority level" in low or "employment type" in low or any(m in low for m in CLOSED_MARKERS)):
            return "LinkedIn sign-in page instead of the listing"
    if not low.strip() and not job_posting_ld(raw):
        return "empty page"
    return None


# --- parsing ------------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _is_hourly(value: str, k_flag: str | None, unit: str | None) -> bool:
    """A "/hr" or "/hour" figure, or a bare one under 300 ("$35 - $45 an hour")."""
    unit = (unit or "").lower()
    if unit in ("hr", "hour"):
        return True
    try:
        return not unit and not k_flag and float(value.replace(",", "")) < 300
    except ValueError:
        return False


def _to_annual(value: str, k_flag: str | None, unit: str | None) -> int | None:
    try:
        num = float(value.replace(",", ""))
    except ValueError:
        return None
    if k_flag:
        num *= 1000
    if _is_hourly(value, k_flag, unit):
        num *= HOURS_PER_YEAR
    if num < 10_000 or num > 1_000_000:
        return None
    return int(round(num))


def job_posting_ld(raw_html: str) -> dict | None:
    """The JSON-LD JobPosting block LinkedIn (and many ATSs) embed in the page, if any."""
    for m in re.finditer(r'(?is)<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', raw_html or ""):
        try:
            data = json.loads(html_lib.unescape(m.group(1)))
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def extract_facts(listing: dict, candidate: dict | None = None) -> dict:
    """Pull the decision facts out of a fetched listing (plus the feed row's own fields)."""
    candidate = candidate or {}
    text = listing.get("text") or ""
    low = text.lower()
    facts: dict = {
        "closed": bool(listing.get("closed")) or any(marker in low[:60_000] for marker in CLOSED_MARKERS),
        "pay_min": None, "pay_max": None, "pay_text": "", "pay_hourly": False,
        "remote": False, "onsite": False, "hybrid": False,
        "location": listing.get("location") or candidate.get("location") or "",
        "posted": listing.get("posted") or candidate.get("date_posted") or "",
        "applicants": None, "years": None, "clearance": False,
        "title": listing.get("title") or candidate.get("title") or "",
        "source": listing.get("source", ""),
    }
    ld = job_posting_ld(listing.get("html") or "")
    if ld:
        facts["title"] = ld.get("title") or facts["title"]
        if str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE":
            facts["remote"] = True
        if ld.get("datePosted"):
            facts["posted"] = str(ld["datePosted"])[:10]
        salary = ld.get("baseSalary") or {}
        value = salary.get("value") if isinstance(salary, dict) else None
        if isinstance(value, dict):
            unit = str(value.get("unitText", "")).upper()
            lo, hi = value.get("minValue"), value.get("maxValue")
            mult = yearly_factor(unit) or 1
            try:
                if lo is not None:
                    facts["pay_min"] = int(round(float(lo) * mult))
                if hi is not None:
                    facts["pay_max"] = int(round(float(hi) * mult))
            except (TypeError, ValueError):
                pass
            facts["pay_hourly"] = unit == "HOUR" and (lo is not None or hi is not None)
        loc = ld.get("jobLocation")
        loc = loc[0] if isinstance(loc, list) and loc else loc
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                city = addr.get("addressLocality") or ""
                region = addr.get("addressRegion") or ""
                if city or region:
                    facts["location"] = ", ".join(p for p in (city, region) if p)
        desc = _strip_html(html_lib.unescape(str(ld.get("description", ""))))
        if desc:
            text = text + " " + desc
            low = text.lower()
    if facts["pay_max"] is None:
        m = PAY_RANGE.search(text)
        if m:
            lo = _to_annual(m.group(1), m.group(2), m.group(3) or m.group(6))
            hi = _to_annual(m.group(4), m.group(5), m.group(6) or m.group(3))
            if lo and hi and hi >= lo:
                facts["pay_min"], facts["pay_max"], facts["pay_text"] = lo, hi, m.group(0)
                facts["pay_hourly"] = _is_hourly(m.group(4), m.group(5), m.group(6) or m.group(3))
    if facts["pay_max"] is None and HOURLY_PAY.search(text):  # no range found; a single hourly rate
        facts["pay_hourly"] = True
    if facts["pay_max"] and not facts["pay_text"]:
        facts["pay_text"] = f"${facts['pay_min']:,} to ${facts['pay_max']:,}" if facts["pay_min"] else f"up to ${facts['pay_max']:,}"
    head = text[:4000]
    if not is_local(facts["location"]):
        m = HOME_REGION_IN_TEXT.search(head) or SECOND_METRO_IN_TEXT.search(head)
        if m:
            facts["location"] = f"{facts['location']} ({m.group(0)})" if facts["location"] else m.group(0)
    if REMOTE_WORDS.search(facts["title"]) or REMOTE_WORDS.search(head) or "remote" in facts["location"].lower():
        facts["remote"] = True
    if ONSITE_WORDS.search(head) or ONSITE_WORDS.search(facts["title"]):
        facts["onsite"] = True
        if re.search(r"\bhybrid\b", head, re.I):
            facts["hybrid"] = True
    if re.search(r"(?i)\b(100%|fully|entirely)\s+(on-?site|in-?office)\b|\bon-?site\s+(role|position)\b|physically onsite", head):
        facts["onsite"] = True
        facts["remote"] = False if not re.search(r"(?i)remote (?:global )?support", head) else facts["remote"]
    m = APPLICANTS.search(text)
    if m:
        facts["applicants"] = int(m.group(2)) if not m.group(1) else int(m.group(2))
    years = [int(y) for y in YEARS.findall(text[:20_000]) if 0 < int(y) < 30]
    if years:
        facts["years"] = max(years)
    facts["clearance"] = bool(CLEARANCE.search(text))
    if not facts["posted"]:
        m = POSTED.search(text[:3000])
        if m:
            facts["posted"] = m.group(0)
    return facts


def mentioned_skills(listing: dict, skills) -> list[str]:
    """Which of the owner's key tools the posting mentions, in the owner's order (a signal for ranking, never a filter).

    Reads the posting's own description (its JobPosting block) when the page has one, so the
    "similar jobs" panel on a LinkedIn page does not count; otherwise the fetched text.
    """
    ld = job_posting_ld(listing.get("html") or "")
    text = _strip_html(html_lib.unescape(str(ld.get("description", "")))) if ld else ""
    text = text or listing.get("text") or ""
    found = []
    for skill in skills:
        names = SKILL_ALIASES.get(skill.lower(), (re.escape(skill.lower()),))
        if re.search(r"\b(?:" + "|".join(names) + r")\b", text, re.I):
            found.append(skill)
    return found


def agency_hourly(candidate: dict, facts: dict) -> bool:
    """A staffing agency paying by the hour, per the listing page or the feed's pay_interval."""
    hourly = facts.get("pay_hourly") or str(candidate.get("pay_interval") or "").lower() == "hourly"
    return bool(hourly) and is_staffing_agency(candidate.get("company", ""))


def decide(candidate: dict, facts: dict, floor: int = FLOOR) -> dict:
    """'apply_now' | 'review' | 'skip' | 'dead' with reasons. Never judges fit."""
    reasons: list[str] = []
    title = facts.get("title") or candidate.get("title") or ""
    if facts.get("closed"):
        return {"verdict": "dead", "reasons": ["listing closed"]}
    age = posting_age_days(facts.get("posted"))
    if age is not None and age >= MAX_POSTING_AGE_DAYS:
        return {"verdict": "skip", "reasons": [f"posted {age} days ago (3 weeks or more)"]}
    if facts.get("clearance"):
        return {"verdict": "skip", "reasons": ["security clearance required"]}
    if SENIOR_TITLE.search(title):
        return {"verdict": "skip", "reasons": [f"title is above level: {title}"]}
    if JUNIOR_TITLE.search(title):
        return {"verdict": "skip", "reasons": [f"junior title: {title}"]}
    if UPPER_TITLE.search(title) and not re.search(r"\b(ii|2)\s+to\s+(iii|3)\b", title, re.I):
        return {"verdict": "skip", "reasons": [f"title is above the level II band: {title}"]}
    years = facts.get("years")
    if years and years >= MAX_YEARS:
        return {"verdict": "skip", "reasons": [f"asks for {years}+ years"]}
    pay_max = facts.get("pay_max")
    if pay_max and pay_max < REVIEW_BAND:
        return {"verdict": "skip", "reasons": [f"pay tops out at ${pay_max:,}"]}
    in_area = is_local(facts.get("location", "")) or is_local(candidate.get("location", ""))
    remote = bool(facts.get("remote"))
    if not in_area and not remote:
        if facts.get("onsite") or facts.get("location"):
            return {"verdict": "skip", "reasons": [f"onsite outside Raleigh-Durham and Greater Minneapolis: {facts.get('location') or 'location unknown'}"]}
        reasons.append("location unconfirmed")
    if "hq_state" in facts:  # set by validate() for remote roles under the North Carolina-presence rule (2024-12-23)
        hq = facts.get("hq_state")
        if hq and hq != "NC":
            # Widened from North Carolina-HQ-only: any North Carolina brick-and-mortar qualifies, and a headquarters
            # elsewhere cannot disprove a North Carolina branch, so this is a flag for the owner, not a knockout.
            reasons.append(f"employer headquartered outside North Carolina ({hq}); counts only if it has a North Carolina branch")
        if not hq:
            reasons.append("North Carolina headquarters not confirmed")
    if pay_max and pay_max < floor:
        reasons.append(f"pay tops out at ${pay_max:,}, under the ${floor:,} floor")
    if not pay_max:
        reasons.append("pay not published")
    if facts.get("applicants") and facts["applicants"] >= 200:
        reasons.append(f"{facts['applicants']}+ applicants already")
    if agency_hourly(candidate, facts):  # a flag, not a filter
        reasons.append(AGENCY_HOURLY)
    hard = [r for r in reasons if r.startswith("location") or "under the" in r
            or r.startswith(("North Carolina headquarters", "employer headquartered"))]
    return {"verdict": "review" if hard else "apply_now", "reasons": reasons}


def _is_linkedin(url: object) -> bool:
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _is_indeed(url: object) -> bool:
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    return host == "indeed.com" or host.endswith(".indeed.com")


def employer_link(candidate: dict) -> str:
    """The employer's own posting an Indeed row links to (the feed's job_url_direct), else "".

    Live sample 2024-12-09: 108 of 115 Indeed jobs linked to an employer site; the other 7 to
    Indeed's own page (jobs that are applied for on Indeed), which keep the Indeed check.
    """
    direct = str(candidate.get("job_url_direct") or "").strip()
    if not _is_indeed(candidate.get("job_url")) or not direct.startswith(("http://", "https://")) or _is_indeed(direct):
        return ""
    return direct


def shows_listing(listing: dict, title: str) -> bool:
    """True when a fetched employer page is the posting: a JobPosting block, a closed notice, or most
    of the job title's words in the text. UltiPro, BambooHR, ADP and Dayforce send an empty app shell
    that a browser fills in later (live check 2024-12-09); nothing may be decided from that."""
    if listing.get("closed") or listing.get("source") not in ("unlocker", "direct", "scrapling-direct"):  # a gone page, or an ATS API answer
        return True
    text = (listing.get("text") or "").lower()
    if job_posting_ld(listing.get("html") or "") or any(marker in text for marker in CLOSED_MARKERS):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2]
    found = set(re.findall(r"[a-z0-9]+", text))
    return bool(words) and sum(w in found for w in words) >= 0.75 * len(words)


def physical_in_area(candidate: dict, facts: dict) -> bool:
    """The posting itself places a workplace in the owner's area: onsite or hybrid there, or located there and not
    flagged remote. Since 2024-12-23 a Raleigh-Durham role is its own North Carolina brick-and-mortar evidence, and
    a Greater Minneapolis role needs no North Carolina presence at all, so the headquarters check does
    not apply to either (and no lookup is spent)."""
    in_area = is_local(facts.get("location", "")) or is_local(candidate.get("location", ""))
    return bool(in_area) and bool(facts.get("onsite") or facts.get("hybrid") or not facts.get("remote"))


def validate(candidate: dict, fetcher=fetch_listing, hq_lookup=None, skills=()) -> dict:
    """One call per queue row: fetch, extract, decide. Safe to run in a thread pool.

    hq_lookup(employer) -> two-letter state or None (employer_hq.hq_state) applies the
    North Carolina-presence rule to remote roles; a role physically in Raleigh-Durham is its own
    branch evidence and never spends a lookup. skills (the owner's key tools) adds which ones the
    posting mentions to the facts; it never changes the verdict.
    """
    employer = employer_link(candidate)
    url = employer or candidate.get("job_url") or ""
    try:
        listing = fetcher(url)
    except Exception as e:  # noqa: BLE001
        listing = {"source": "error", "closed": False, "text": "", "html": "", "title": "", "location": "", "posted": "", "status": None, "error": f"{type(e).__name__}: {e}"}
    if employer and not listing.get("error") and not shows_listing(listing, candidate.get("title", "")):
        listing = {**listing, "html": "", "text": "", "error": "the employer's page did not show the job"}
    if listing.get("error") and not listing.get("text"):
        # The page could not be read, but the title alone can still rule a row out
        # (senior, junior, clearance words). Anything else stays indeterminate.
        where = candidate.get("location", "")
        title_only = decide(candidate, {"closed": False, "title": candidate.get("title", ""), "location": where,
                                        "remote": "remote" in str(where).lower()})
        if title_only["verdict"] == "skip":
            title_only["facts"] = {}
            return title_only
        reasons = [f"could not check the listing ({listing['error']})"]
        if agency_hourly(candidate, {}):  # the feed still says hourly
            reasons.append(AGENCY_HOURLY)
        return {"verdict": "indeterminate", "reasons": reasons, "facts": {}}
    facts = extract_facts(listing, candidate)
    facts["checked_url"] = url  # the dashboard's apply link: the page that was actually read
    # "Page not found" can be a site down for maintenance; the dashboard confirms it on a second morning.
    facts["not_found"] = listing.get("status") in (404, 410)
    if skills:  # the dashboard shows "4 of 7 key tools" and lists the best matches first (2024-12-09)
        facts["skills"], facts["skills_of"] = mentioned_skills(listing, skills), len(skills)
    verdict = decide(candidate, facts)
    if hq_lookup is not None and verdict["verdict"] in ("apply_now", "review") and not physical_in_area(candidate, facts):
        facts["hq_state"] = hq_lookup(candidate.get("company", ""))
        verdict = decide(candidate, facts)
    verdict["facts"] = {k: facts.get(k) for k in ("pay_min", "pay_max", "pay_text", "pay_hourly", "remote", "onsite", "hybrid", "location", "posted", "applicants", "years", "source", "hq_state", "checked_url", "skills", "skills_of", "not_found")}
    return verdict


if __name__ == "__main__":  # quick manual check: python validate_listing.py <url>
    import sys
    result = validate({"job_url": sys.argv[1], "title": "", "location": ""})
    print(json.dumps(result, indent=2))
