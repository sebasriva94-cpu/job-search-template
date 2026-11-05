"""Posting text from LinkedIn's public job page, fetched directly from the home IP (2024-12-17).

A logged-out visitor gets the full description on https://www.linkedin.com/jobs/view/{id}; JobSpy's
search results do not carry it, so 212 LinkedIn rows on 10/19 had no text and 80 ended Unscored
after Bright Data timed out fetching them. The trickle lane (linkedin_pull.py --trickle) calls
fetch_page for the rows that pass the title, place and pay gates, one every DETAIL_GAP seconds,
inside the same pacing and probe log as the searches. A 429 or a sign-in wall stops the detail
fetches for the rest of the run; the judge's Bright Data fallback remains for those rows.

Pure functions apart from fetch_page, so the parser is unit-tested without network.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

try:
    from scripts.sweep_common import clean_description
except ModuleNotFoundError:
    from sweep_common import clean_description

DETAIL_GAP = (8, 15)   # seconds between two detail fetches (jittered)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*?-)?(\d{6,})|[?&]currentJobId=(\d{6,})")
_LD_JSON = re.compile(r'(?is)<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>')
_MARKUP = re.compile(r'(?is)<div[^>]+class="[^"]*(?:show-more-less-html__markup|description__text)[^"]*"[^>]*>(.*?)</div>\s*(?:</div>|<button)')


def job_id(url: str) -> str:
    """The numeric posting id in a LinkedIn job URL, or ''."""
    match = _JOB_ID.search(str(url or ""))
    return (match.group(1) or match.group(2)) if match else ""


def view_url(url_or_id: str) -> str:
    ident = url_or_id if str(url_or_id).isdigit() else job_id(url_or_id)
    return f"https://www.linkedin.com/jobs/view/{ident}" if ident else ""


def fetch_page(url: str, timeout: int = 25) -> dict:
    """{status, html, final_url, error}: one plain GET with a browser user agent, redirects followed."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            return {"status": response.status, "html": raw.decode("utf-8", "replace"), "final_url": response.geturl(), "error": None}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "html": "", "final_url": exc.geturl() or url, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 -- network trouble is an outcome, not a crash
        return {"status": None, "html": "", "final_url": url, "error": type(exc).__name__}


def description_from_html(html: str) -> str:
    """The posting text, one line, from the page's JSON-LD block or its description markup; '' when neither."""
    for block in _LD_JSON.findall(html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                text = clean_description(item["description"])
                if text:
                    return text
    match = _MARKUP.search(html or "")
    return clean_description(match.group(1)) if match else ""


def classify(page: dict) -> str:
    """'ok' | 'empty' | 'rate_limited' | 'auth' | 'transient' for one fetched page."""
    status, final_url = page.get("status"), str(page.get("final_url") or "").lower()
    if status == 429:
        return "rate_limited"
    if status in (401, 403, 999) or any(marker in final_url for marker in ("authwall", "/login", "/signup", "/checkpoint")):
        return "auth"
    if status is None or (isinstance(status, int) and status >= 500):
        return "transient"
    return "ok" if description_from_html(page.get("html") or "") else "empty"
