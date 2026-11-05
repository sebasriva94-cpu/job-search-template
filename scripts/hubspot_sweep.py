"""HubSpot careers sweep using one local Playwright scan of the public all-openings page.

The prior Bright Data Browser API path was slow and intermittently timed out on the first
navigation. HubSpot's public `/careers/jobs/all` page renders all openings in one local browser
page, so discovery does not require a paid remote browser or a multi-page loop.
"""
from __future__ import annotations

import re
import sys

try:
    from scripts.sweep_common import merge_rows, project_root, relevant
except ModuleNotFoundError:
    from sweep_common import merge_rows, project_root, relevant

CAREERS_URL = "https://www.hubspot.com/careers/jobs/all"
REQUEST_TIMEOUT_MS = 45_000


def redact_error(value: object, secrets=()) -> str:
    text = str(value)
    text = re.sub(r"\b(?:wss?|https?)://\S+", "[redacted-url]", text, flags=re.I)
    text = re.sub(r"(?i)\b(?:token|password|authorization)\s*[=:]\s*\S+", "[redacted-secret]", text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    return text


def extract_cards(raw_cards: list[dict]) -> list[dict]:
    """Keep only relevant, unique HubSpot career links and their display context."""
    seen: set[str] = set()
    cards: list[dict] = []
    for raw in raw_cards:
        href = str(raw.get("href") or "")
        title = str(raw.get("title") or "").strip()
        if not href or href in seen or not relevant(title):
            continue
        seen.add(href)
        cards.append({
            "title": title,
            "href": href,
            "ctx": [title, str(raw.get("department") or ""), str(raw.get("location") or "")],
        })
    return cards


def cards_to_rows(cards: list[dict]) -> list[dict]:
    rows = []
    for card in cards:
        context = [str(line) for line in card.get("ctx") or []]
        department_hit = any("information technology" in line.lower() for line in context[1:3])
        if not relevant(card.get("title")) and not department_hit:
            continue
        location = next((line for line in context[1:] if line and line != card.get("title")
                         and "information technology" not in line.lower()), "")
        rows.append({
            "title": card.get("title", ""), "company": "HubSpot", "location": location,
            "date_posted": "", "min_amount": "", "max_amount": "",
            "is_remote": str("remote" in location.lower()).lower(),
            "job_url": card.get("href", ""), "site": "hubspot",
        })
    return rows


def validate_scan(card_count: int, first_page_timed_out: bool = False) -> None:
    if first_page_timed_out:
        raise RuntimeError("all-openings selector timed out")
    if card_count == 0:
        raise RuntimeError("zero cards scanned")


def fetch_cards() -> list[dict]:
    """Use local Chromium. This is read-only and opens no application form."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,mp4}", lambda route: route.abort())
            page.goto(CAREERS_URL, timeout=REQUEST_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_selector('a[href*="/careers/jobs/"]', timeout=15_000)
            raw_cards = page.eval_on_selector_all(
                'a[href*="/careers/jobs/"]',
                """els => els.map(e => {
                    const card = e.closest("li, article, [class*=job], [class*=card]") || e.parentElement?.parentElement;
                    const lines = (card ? card.innerText : "").split("\\n").map(s => s.trim()).filter(Boolean);
                    const title = lines.find(l => l.toLowerCase() !== "apply" && l.length > 3) || "";
                    const department = lines.find(l => /^(Business Technology|Customer Success|Engineering|Operations|Product|Security)$/i.test(l)) || "";
                    const location = lines.find(l => /^(Remote|Office|Flex)\\s*-/.test(l) || /,\\s*(USA|US|Canada|Ireland|United Kingdom)$/i.test(l)) || "";
                    return {href: e.href, title, department, location};
                })""",
            )
            browser.close()
    except Exception as exc:
        raise RuntimeError(redact_error(exc)) from None

    # A zero *raw* card count means the board did not load. Zero relevant roles is valid:
    # HubSpot may simply have no IT-support/infrastructure opening that day.
    validate_scan(len(raw_cards))
    return extract_cards(raw_cards)


def main() -> int:
    # The night task's stdout is cp1252; a character it lacks (Playwright's box drawing, an emoji in a
    # title) must not crash a print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        cards = fetch_cards()
        rows = cards_to_rows(cards)
        print(f"\n=== HubSpot ({len(cards)} relevant roles scanned locally, {len(rows)} IT-relevant) ===")
        for row in rows:
            print(f"  {row['title']} | {row['location'] or '?'} | {row['job_url']}")
        if rows:
            merged = merge_rows(rows, project_root())
            print(f"Merged {merged['added']} new rows ({merged['total']} total) -> {merged['path']}")
        return 0
    except Exception as exc:
        print(f"\n=== HubSpot: FETCH FAILED - {type(exc).__name__}: {redact_error(exc)} ===")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
