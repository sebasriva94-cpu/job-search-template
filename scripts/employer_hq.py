"""North Carolina-headquarters check per employer.

The rule: an employer qualifies with a North Carolina headquarters
OR any North Carolina brick-and-mortar (a branch: an out-of-state headquarters with a local office qualifies).
A role physically in Raleigh-Durham is its own branch evidence and never spends a lookup here;
only remote roles ask for the headquarters state, and a known out-of-state HQ is a review flag
for the owner's branch confirmation, not a knockout.

Lookup order, cached per employer in dashboard/employer-hq.json:
  1. The cache, including hand-set entries ("source": "manual").
  2. The employer's own name when it places it (UNC Chapel Hill, Wake County).
  3. The employer's LinkedIn company page through Apify (harvestapi/linkedin-company), which flags
     the headquarters office. Needs APIFY_TOKEN or APIFY_API_TOKEN; tested 2024-12-09: 9 of 10 resolved in 12 s.
  4. One Google search through the Bright Data CLI ('"<employer>" headquarters'), reading the
     knowledge panel, Google's AI overview and the top snippets that name the employer. Each
     source votes for the state named right after "headquartered"/"headquarters"; most votes win.
A lookup that finds nothing is cached as unknown for UNKNOWN_RETRY_DAYS. A lookup that fails
for good reasons of its own is not cached; one that fails because the provider is throttled or down
(Apify 429 after its retries, a 5xx outage, bad key) is cached for TEMP_FAILURE_HOURS so the same
employer does not cost another paid call during the outage. Unknown never counts as out of state:
the validator shows those roles for review.

Apify runs under shared rate control (scripts/rate_control.py): one request at a time, 2 s between
starts, 30 new employers per run and 60 a day; a 429 gets 3 attempts with 1-2 s then 2-4 s jittered
backoff and then a 30-minute cooldown, a 5xx or network error up to 2 retries after 5-10 s then 15-30 s,
a 401/403 no retry and a 24-hour cooldown. The cooldown is persisted in dashboard/provider-state.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request

try:
    from scripts import rate_control
    from scripts.sweep_common import HOME_REGION_PLACES, is_home_region, normalize_company, project_root
except ModuleNotFoundError:
    import rate_control
    from sweep_common import HOME_REGION_PLACES, is_home_region, normalize_company, project_root

APIFY_ACTOR = "harvestapi~linkedin-company"
NODE = os.environ.get("BDATA_NODE", os.path.expanduser(r"~\.claude\node.exe"))
CLI_JS = os.environ.get("BDATA_CLI_JS", os.path.expanduser(r"~\.claude\node_modules\@brightdata\cli\dist\index.js"))
ZONE = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "web_unlocker")
UNKNOWN_RETRY_DAYS = 30
TEMP_FAILURE_HOURS = 6
APIFY_KEY = "apify"
APIFY_429_BACKOFF = ((1, 2), (2, 4))          # waits between the 3 attempts a 429 gets
APIFY_TRANSIENT_BACKOFF = ((5, 10), (15, 30))  # waits before the (at most 2) retries of a 5xx / network error
APIFY_429_COOLDOWN = 30 * 60
APIFY_LONG_COOLDOWN = 24 * 3600

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "washington, d.c.": "DC",
    "washington, dc": "DC", "washington dc": "DC",
}
_CODES = set(STATES.values())
# North Carolina cities that name the state on their own when a snippet skips it ("headquartered in Durham").
# Only names that are not also common elsewhere.
HOME_STATE_PLACES = HOME_REGION_PLACES + (
    "charlotte", "greensboro", "winston-salem", "asheville", "high point", "gastonia", "huntersville",
    "mooresville", "kannapolis", "statesville", "goldsboro", "rocky mount", "new bern", "kinston",
)
# Big cities elsewhere that name their state on their own ("headquartered in Chicago").
# Names shared by several states (Portland, Columbus, Kansas City, Richmond) are left out.
OTHER_CITIES = {
    "los angeles": "CA", "san francisco": "CA", "san jose": "CA", "san diego": "CA", "palo alto": "CA",
    "mountain view": "CA", "seattle": "WA", "chicago": "IL", "houston": "TX", "dallas": "TX", "austin": "TX",
    "san antonio": "TX", "atlanta": "GA", "boston": "MA", "cambridge, ma": "MA", "denver": "CO", "miami": "FL", "tampa": "FL", "orlando": "FL",
    "phoenix": "AZ", "philadelphia": "PA", "pittsburgh": "PA", "detroit": "MI", "minneapolis": "MN",
    "nashville": "TN", "salt lake city": "UT", "st. louis": "MO",
    "baltimore": "MD", "cleveland": "OH", "cincinnati": "OH", "indianapolis": "IN", "milwaukee": "WI",
    "las vegas": "NV", "new orleans": "LA", "omaha": "NE", "new york city": "NY", "manhattan": "NY",
    "brooklyn": "NY",
}
_STATE_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(STATES, key=len, reverse=True)) + r")\b", re.I)
_STATE_CODE_RE = re.compile(r",\s*([A-Z]{2})\b")
_HOME_STATE_PLACE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in HOME_STATE_PLACES) + r")\b", re.I)
_OTHER_CITY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in sorted(OTHER_CITIES, key=len, reverse=True)) + r")\b", re.I)
_HQ_CUE = re.compile(r"\bhead\s?quarter(?:s|ed)?\b|\bhead office\b|\bcorporate office\b", re.I)
_CLAUSE_END = re.compile(r"(?<=[a-z0-9)])\.\s|;|\|")
_GENERIC = {"the", "inc", "llc", "corp", "corporation", "company", "group", "university", "college", "of", "and",
            "services", "solutions", "systems", "technologies", "technology", "holdings", "international", "health"}

_lock = threading.Lock()
_cache: dict | None = None
_cache_file: Path | None = None


def cache_path(root: Path | str | None = None) -> Path:
    return Path(root or project_root()) / "dashboard" / "employer-hq.json"


def employer_key(employer: object) -> str:
    return normalize_company(employer)


def state_in(text: str) -> str | None:
    """The first US state named in `text`: a state name, a ", XX" code, a Efland or a big city elsewhere."""
    hits = []
    for m in _STATE_NAME_RE.finditer(text or ""):
        hits.append((m.start(), STATES[m.group(1).lower()]))
    for m in _STATE_CODE_RE.finditer(text or ""):
        if m.group(1) in _CODES:
            hits.append((m.start(), m.group(1)))
    for m in _HOME_STATE_PLACE_RE.finditer(text or ""):
        hits.append((m.start(), "NC"))
    for m in _OTHER_CITY_RE.finditer(text or ""):
        hits.append((m.start(), OTHER_CITIES[m.group(1).lower()]))
    return min(hits)[1] if hits else None


def state_after_cue(text: str) -> str | None:
    """The state in the clause right after a headquarters cue ("headquartered in Durham, North Carolina")."""
    for cue in _HQ_CUE.finditer(text or ""):
        clause = _CLAUSE_END.split(text[cue.end(): cue.end() + 140])[0]
        state = state_in(clause)
        if state:
            return state
    return None


def _panel_texts(obj):
    """Texts of knowledge-panel facts labelled Headquarters, whatever the nesting."""
    if isinstance(obj, dict):
        label = " ".join(str(obj.get(k, "")) for k in ("key", "title", "name", "label"))
        if "headquarter" in label.lower():
            yield json.dumps(obj, ensure_ascii=False)
            return
        for key, value in obj.items():
            if "headquarter" in str(key).lower() and isinstance(value, str):
                yield value
            elif isinstance(value, (dict, list)):
                yield from _panel_texts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _panel_texts(item)


def hq_from_serp(data: dict, employer: str) -> dict:
    """{"state", "evidence", "source"} from one Google result (Bright Data brd_json shape)."""
    sources: list[tuple[str, str, bool]] = []  # (source, text, needs_cue)
    for text in _panel_texts(data.get("knowledge") or {}):
        sources.append(("google knowledge panel", text, False))
    for item in (data.get("ai_overview") or {}).get("texts") or []:
        if isinstance(item, dict) and item.get("snippet"):
            sources.append(("google AI overview", str(item["snippet"]), True))
    words = [w for w in re.findall(r"[a-z0-9]+", employer_key(employer)) if len(w) >= 3 and w not in _GENERIC]
    for result in (data.get("organic") or [])[:6]:
        text = f"{result.get('title') or ''} | {result.get('description') or ''}"
        if words and any(w in text.lower() for w in words):
            host = urllib.parse.urlsplit(result.get("link") or "").netloc
            sources.append((f"google result {host}", text, True))
    votes: dict[str, list[tuple[str, str]]] = {}
    for source, text, needs_cue in sources:
        state = state_after_cue(text) if needs_cue else state_in(text)
        if state:
            votes.setdefault(state, []).append((source, text))
    if not votes:
        return {"state": None, "evidence": "", "source": "google: no headquarters found"}
    order = list(votes)  # first state seen ranks first on ties (sources are in priority order)
    best = max(order, key=lambda s: (len(votes[s]), -order.index(s)))
    source, text = votes[best][0]
    return {"state": best, "evidence": re.sub(r"\s+", " ", text)[:200], "source": source}


def _us_state(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) == 2 and text.upper() in _CODES:
        return text.upper()
    return STATES.get(text.lower())


def _same_company(found_name: str, employer: str) -> bool:
    words = lambda name: {w for w in re.findall(r"[a-z0-9]+", employer_key(name)) if len(w) >= 3 and w not in _GENERIC}
    a, b = words(found_name), words(employer)
    return bool(a & b) or employer_key(found_name) == employer_key(employer)


def hq_from_linkedin(items: list, employer: str) -> dict:
    """{"state", "evidence", "source"} from harvestapi/linkedin-company results for one search.

    A US headquarters gives its state; a headquarters abroad gives "non-US:<country>", which is
    simply not North Carolina. A result that names a different company counts as not found.
    """
    source = "linkedin company page (Apify)"
    item = next((i for i in items or [] if _same_company(str(i.get("name") or ""), employer)), None)
    if not item:
        return {"state": None, "evidence": "", "source": f"{source}: company not found"}
    hq = next((loc for loc in item.get("locations") or [] if loc.get("headquarter")), None)
    if not hq:
        return {"state": None, "evidence": str(item.get("name") or ""), "source": f"{source}: no headquarters listed"}
    parsed = hq.get("parsed") or {}
    country = str(hq.get("country") or parsed.get("countryCode") or "").upper()
    where = ", ".join(str(v) for v in (hq.get("city"), hq.get("geographicArea") or parsed.get("state"), country) if v)
    if country and country != "US":
        state = f"non-US:{country}"
    else:
        state = _us_state(hq.get("geographicArea")) or _us_state(parsed.get("state")) or _us_state(parsed.get("regionCode"))
    return {"state": state, "evidence": f"{item.get('name')}: headquarters {where}", "source": source}


def apify_token() -> str:
    """The Apify key: APIFY_TOKEN, or APIFY_API_TOKEN (the name the owner saved it under, 2024-12-09)."""
    return (os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "").strip()


class LookupFailed(RuntimeError):
    """Every lookup source failed or was held back. `cache` is True when a paid call really failed."""

    def __init__(self, message: str, cache: bool = False):
        super().__init__(message)
        self.cache = cache


def apify_lookup(employer: str, timeout: int = 120) -> dict:
    """The employer's LinkedIn company page through Apify. Needs an Apify key. Raises on failure."""
    token = apify_token()
    if not token:
        raise RuntimeError("APIFY_TOKEN not set")
    gate = rate_control.get_gate(rate_control.APIFY)
    admitted = gate.admit(APIFY_KEY)  # one unit = one new employer, whatever the retries
    if not admitted.ok:
        raise rate_control.ProviderUnavailable(admitted.reason, admitted.cooldown_until)
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?timeout={timeout}"
    body = json.dumps({"searches": [str(employer)]}).encode()

    def request() -> list:
        req = urllib.request.Request(url, data=body, method="POST", headers={  # key in a header, never the URL
            "Content-Type": "application/json", "Authorization": "Bearer " + token})
        with gate.slot(), urllib.request.urlopen(req, timeout=timeout + 30) as response:
            return json.load(response)

    try:
        items = rate_control.retry_call(request, gate.runtime, {
            "rate_limited": APIFY_429_BACKOFF, "transient": APIFY_TRANSIENT_BACKOFF})
    except rate_control.CallFailed as exc:
        failure = exc.failure
        if failure.kind == "rate_limited":
            gate.cooldown(APIFY_KEY, seconds=APIFY_429_COOLDOWN, reason="http_429")
        elif failure.kind in ("auth", "quota"):
            gate.cooldown(APIFY_KEY, seconds=APIFY_LONG_COOLDOWN, reason="auth_failed" if failure.kind == "auth" else "quota_exceeded",
                          status=rate_control.FAILED)
        else:
            gate.failure(APIFY_KEY, failure.reason)
        raise
    gate.success(APIFY_KEY)
    return hq_from_linkedin(items, str(employer))


def best_lookup(employer: str) -> dict:
    """Apify first when a token is set, then Google through Bright Data. Raises only if both fail.

    A source held back by its cooldown or budget, or one that failed for real, leaves a result without a
    state inconclusive: LookupFailed (cache=True after a real failure) instead of a cached "unknown".
    """
    result, errors, held, failed = None, [], False, False
    for lookup in ((apify_lookup, google_lookup) if apify_token() else (google_lookup,)):
        try:
            result = lookup(employer)
        except Exception as exc:  # try the next source
            errors.append(f"{lookup.__name__}: {type(exc).__name__}")
            held |= isinstance(exc, rate_control.ProviderUnavailable)
            failed |= isinstance(exc, rate_control.CallFailed)
            continue
        if result.get("state"):
            return result
    if result is None or (held or failed):
        raise LookupFailed("; ".join(errors), cache=failed)
    return result


def google_lookup(employer: str, timeout: int = 90) -> dict:
    """One Google search through the Bright Data CLI (existing unlocker zone). Raises on failure."""
    query = urllib.parse.quote_plus(f'"{employer_key(employer)}" headquarters')
    url = f"https://www.google.com/search?q={query}&hl=en&gl=us&brd_json=1"
    gate = rate_control.brightdata_gate()  # the same limits as every other Bright Data request
    admitted = gate.admit(rate_control.BRIGHTDATA_KEY)
    if not admitted.ok:
        raise rate_control.ProviderUnavailable(admitted.reason, admitted.cooldown_until)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "serp.json"
        try:
            with gate.slot():
                subprocess.run([NODE, CLI_JS, "scrape", url, "--zone", ZONE, "--country", "us", "--format", "html",
                                "-o", str(out)], check=True, capture_output=True, timeout=timeout)
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:
            text = exc.stderr.decode("utf-8", "ignore") if isinstance(getattr(exc, "stderr", None), bytes) else ""
            code = re.search(r"\b(401|402|403|429)\b", text)
            failure = rate_control.classify_status(int(code.group(1)) if code else None)
            if not rate_control.apply_brightdata_failure(gate, failure, text):
                gate.failure(rate_control.BRIGHTDATA_KEY, failure.reason if code else f"cli_{type(exc).__name__}", text)
            raise
    gate.success(rate_control.BRIGHTDATA_KEY)
    return hq_from_serp(data, employer)


def load_cache(path: Path | str | None = None) -> dict:
    global _cache, _cache_file
    target = Path(path) if path else cache_path()
    with _lock:
        if _cache is None or _cache_file != target:
            try:
                _cache = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _cache = {}
            _cache_file = target
        return _cache


def _save(cache: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def hq_state(employer: object, *, lookup=best_lookup, today: dt.date | None = None,
             path: Path | str | None = None, now: dt.datetime | None = None) -> str | None:
    """Two-letter HQ state for an employer, or None when unknown. Looks up and caches on a miss."""
    key = employer_key(employer)
    if not key:
        return None
    target = Path(path) if path else cache_path()
    cache = load_cache(target)
    today = today or dt.date.today()
    now = now or dt.datetime.now().astimezone()
    entry = cache.get(key)
    if entry:
        if entry.get("state"):
            return entry["state"]
        retry_at = rate_control.parse_time(entry.get("retry_not_before"))
        if retry_at:  # a provider outage was cached: no new paid call until the short backoff is over
            if now < retry_at:
                return None
        else:
            try:
                checked = dt.date.fromisoformat(str(entry.get("checked_on", ""))[:10])
                if (today - checked).days < UNKNOWN_RETRY_DAYS:
                    return None
            except ValueError:
                pass
    if "north carolina" in key or is_home_region(employer):
        # The employer's own name places it (UNC Chapel Hill, Wake County, North Carolina State University).
        found = {"state": "NC", "evidence": str(employer), "source": "employer name"}
    else:
        try:
            found = lookup(str(employer))
        except LookupFailed as exc:  # a failed lookup is not evidence; try again next run
            if exc.cache:  # ... but not while the provider is down: back off instead of paying again
                with _lock:
                    cache[key] = {"name": str(employer), "state": None, "evidence": "",
                                  "source": "provider temporarily unavailable",
                                  "retry_not_before": rate_control.iso(now + dt.timedelta(hours=TEMP_FAILURE_HOURS))}
                    _save(cache, target)
            return None
        except Exception:
            return None
    with _lock:
        cache[key] = {"name": str(employer), "state": found.get("state"), "evidence": found.get("evidence", ""),
                      "source": found.get("source", ""), "checked_on": today.isoformat()}
        _save(cache, target)
    return found.get("state")


def known_outside_home_state(employer: object, path: Path | str | None = None) -> bool:
    """Cache only, no network: True when the employer is known to be headquartered outside North Carolina.
    Since 2024-12-23 this is NOT an exclusion (a North Carolina branch still qualifies); it only marks
    remote roles for the branch check."""
    entry = load_cache(path).get(employer_key(employer)) or {}
    return bool(entry.get("state")) and entry.get("state") != "NC"
