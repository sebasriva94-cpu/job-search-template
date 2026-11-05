"""Shared schema, relevance vocabulary, normalization, paths, and locked CSV merge."""
from __future__ import annotations

import csv
import datetime as dt
import functools
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Iterable

try:
    import msvcrt
except ImportError as exc:  # This automation is intentionally Windows-only.
    raise RuntimeError("sweep_common requires Windows msvcrt locking") from exc

DEFAULT_ROOT = Path(__file__).resolve().parents[1]  # the repo root
CSV_COLUMNS = [
    "title", "company", "location", "date_posted", "min_amount",
    "max_amount", "is_remote", "job_url", "site", "pay_interval", "job_url_direct", "description",
]
# Daily files written before pay_interval and job_url_direct were added (both 2024-12-09), and before
# description was added (2024-12-12). Pay amounts are always yearly; pay_interval is how the board
# published them ("hourly", "yearly", ...), blank when unknown. job_url_direct is the employer's own
# posting an Indeed row links to, blank when none. description is the posting text as the source
# returned it (Indeed, JSearch, USAJOBS, Greenhouse and Ashby include it for free; LinkedIn and the
# other boards leave it blank), reduced to one line of plain text and capped, so every later reader
# (the retired job-agent importer parsed the CSV line by line) and the judge see real requirements.
LEGACY_CSV_COLUMNS = CSV_COLUMNS[:-1]        # 2024-12-09 to 2024-12-12
LEGACY_CSV_COLUMNS_V1 = CSV_COLUMNS[:-3]     # before 2024-12-09
DESCRIPTION_MAX_CHARS = 6000
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr|table)\b[^>]*>", re.I)


def clean_description(value: object, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    """Posting text as one line of plain text: HTML unescaped and stripped (block tags become
    spaces so words do not run together), whitespace collapsed, newlines removed, capped."""
    import html as _html
    text = "" if value is None else str(value)
    if not text.strip() or text.lower() == "nan":
        return ""
    text = _html.unescape(text)
    if "<" in text and ">" in text:
        text = _BLOCK_TAG.sub(" ", text)
        text = _TAG.sub(" ", text)
        text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " ..."
    return text

# ONE title vocabulary for every stage (2024-10-29 consolidation). The target-company,
# USAJOBS and HubSpot sweeps keep a board's matching roles with it, the pull's API sources
# filter on it, and the dashboard's needs-validation queue surfaces on it. Terms match on
# word boundaries, case-insensitively ("it support" no longer matches "Credit Support").
TITLE_INCLUDE = (
    # core support / sysadmin / endpoint
    "it support", "it engineer", "it operations", "it specialist", "it technician", "it analyst",
    "it field", "information technology", "systems administrator", "system administrator",
    "sysadmin", "systems engineer", "systems analyst", "systems specialist", "systems support",
    "infrastructure", "endpoint", "help desk", "helpdesk", "service desk", "desktop support",
    "desktop engineer", "desktop technician", "technical support", "computer support",
    "support technician", "field support", "network administrator", "network technician",
    "workplace technology", "workplace engineer", "workplace support", "workplace specialist",
    "corporate engineering", "corporate technology", "end user computing", "end-user computing",
    "deskside", "endpoint management", "endpoint administrator", "it administrator",
    # identity / device management
    "identity", "iam", "device management", "mdm", "okta", "intune", "jamf",
    # widened after a funnel audit: rows that matched the target work but were worded
    # differently ("Tech Support Analyst", "Customer Support Engineer", "Microsoft 365 Administrator")
    "tech support", "support engineer", "systems administration",
    "collaboration platform", "microsoft 365", "m365", "office 365", "google workspace",
    "entra", "active directory", "sccm",
    # widened 2024-11-06 after a hand-picked LinkedIn batch beat the engine: "Technology Operations
    # Support Analyst", "Jr. Sailpoint Engineer", "Federal IT Enterprise Services
    # Administrator" all failed this check
    "sailpoint", "technology operations", "services administrator",
    # higher-ed vocabulary (universities title the same work differently)
    "user services", "user support", "academic technology", "instructional technology",
    "classroom technology", "learning technology",
    # healthcare-IT vocabulary
    "healthcare it", "health informatics", "clinical systems", "ehr", "epic analyst",
    # widened after the first gold round: two hand-labelled Apply roles never reached the judge
    # ("Support Analyst", "Senior Frontline Technician"), plus the O*NET reported titles
    # for 15-1232 / 15-1231 / 15-1244 the list lacked. The title gate is for recall; the description decides.
    "support analyst", "frontline technician", "frontline", "computer technician",
    "information systems technician", "network support", "it consultant", "client support",
)
# Titles that share a word with the list above but are not IT-support work or are far
# above the target level. Checked on the title only, never on the company name.
TITLE_EXCLUDE = (
    # long-standing dashboard excludes
    "telecom", "avaya", "data center", "night shift", "production support", "controls",
    "client support", "field service", "assistive technology", "hotel", "hospitality",
    # engineering and trades that only share "systems" / "technician"
    "power systems", "trading systems", "quality systems", "robotics", "maintenance technician",
    "manufacturing", "mechanical", "electrical technician", "hvac", "logistics", "warehouse",
    # non-IT functions that share "infrastructure" / "identity" / "support"
    "software engineer", "software developer", "machine learning", "data engineer",
    "product manager", "program manager", "project manager", "recruiter", "accounting",
    "accountant", "tax", "financing", "procurement", "sourcing", "sales", "marketing",
    "capital markets", "legal", "intern",
    # levels above target
    "director", "vice president", "vp", "head of", "chief", "staff", "principal", "architect",
    "site reliability",
    # adjacent functions that share a support word but are not the owner's lane
    "coordinator", "consultant", "sap", "dba", "database", "governance", "product platform",
    "supply chain", "maintenance technical support",
    # finance titles that "support analyst" (2024-12-19) would otherwise re-admit
    "credit", "loan", "mortgage", "trade support", "treasury",
)
# Exclusions that are only exclusions when nothing in the title says IT (2024-12-19): "consultant" and
# "client support" are common IT job titles ("IT Support Consultant", "IT Client Support") and O*NET
# lists "IT Consultant" under network support, but a bare "Management Consultant" or "Client Support
# Representative" is still out.
SOFT_EXCLUDE = frozenset({"consultant", "client support"})
_IT_MARKER = re.compile(r"\bit\b|information technology|\btechnical\b|\btech\b|\bhelp ?desk\b|\bservice desk\b", re.IGNORECASE)
# USAJOBS searches are already restricted to the 2210 (IT) series, so these federal
# title words mean IT work there and only there. Passed as `extra` by usajobs_sweep.
FEDERAL_2210_EXTRA = (
    "custspt", "customer support", "end user", "desktop", "infosec", "network",
    "operating system",
)
KEYWORDS = TITLE_INCLUDE  # backward-compatible alias


@functools.lru_cache(maxsize=8)
def _pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    escaped = "|".join(re.escape(term.strip()) for term in terms if term.strip())
    return re.compile(rf"\b(?:{escaped})\b", re.IGNORECASE)


def relevant(title: object, *, extra: tuple[str, ...] = ()) -> bool:
    """True when a job TITLE names IT-support work and none of the exclusions apply."""
    text = _text(title)
    if not text:
        return False
    included = _pattern(TITLE_INCLUDE).search(text) or (extra and _pattern(tuple(extra)).search(text))
    if not included:
        return False
    excluded = [m.group(0).lower() for m in _pattern(TITLE_EXCLUDE).finditer(text)]
    if any(term not in SOFT_EXCLUDE for term in excluded):
        return False
    return not excluded or bool(_IT_MARKER.search(text))


def project_root() -> Path:
    return Path(os.environ.get("JOBSEARCH_ROOT", DEFAULT_ROOT))


_COMPANY_SUFFIX_RE = re.compile(
    r"[\s,.]+(?:inc|incorporated|llc|l\.l\.c|ltd|limited|ag|gmbh|plc|corp|corporation|co|company|lp|l\.p|usa)\.?$",
    re.IGNORECASE,
)


def normalize_company(value: object) -> str:
    """Lowercase, collapse whitespace, and strip legal suffixes so "Kapsch TrafficCom AG"
    and "Kapsch TrafficCom USA" key to the same employer."""
    name = re.sub(r"\s+", " ", _text(value).lower())
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)  # "Example Staffing (client role)" -> "example staffing"
    name = re.sub(r"\.com$", "", name)
    for _ in range(3):
        stripped = _COMPANY_SUFFIX_RE.sub("", name).strip(" ,.")
        if not stripped or stripped == name:
            break
        name = stripped
    return name


def normalize_title(value: object) -> str:
    title = _text(value).lower()
    title = re.sub(r"\s*\([^)]*(remote|united states|us|usa|\bnc\b|north carolina)[^)]*\)\s*$", "", title)
    return re.sub(r"\s+", " ", title)


# Staffing agencies (the owner's rule, 2024-12-09): shown like any employer, flagged when the pay is hourly,
# which usually means a contract. The big IT agencies by name, plus agency words in the employer
# name. Checked against the 31k employer names in the job-agent DB; "personnel", "workforce" and
# "employment" are left out because they also name government bodies (Bureau of Naval Personnel,
# Texas Workforce Commission, Employment Development Department), and "Hays" names a Texas county.
STAFFING_AGENCIES = (
    "robert half", "kforce", "teksystems", "insight global", "apex systems", "addison group", "randstad",
    "adecco", "experis", "manpower", "manpowergroup", "akkodis", "modis", "aerotek", "actalent",
    "aston carter", "allegis", "collabera", "signature consultants", "beacon hill", "kelly services", "yoh",
    "cybercoders", "jobot", "vaco", "highspring", "judge group", "intersect group", "system one",
    "genesis10", "eliassen", "harvey nash", "matlen silver", "brooksource", "ettain", "lancesoft",
    "mindlance", "pyramid consulting", "diverse lynx", "compunnel", "net2source", "us tech solutions",
    "russell tobin", "rose international", "kavaliro", "ascendo", "spherion", "express employment",
    "staffmark", "peopleready", "appleone", "ajilon", "hirequest", "michael page", "pagegroup", "belcan",
    "nesco resource", "talentbridge", "sni companies", "stellar it",
    # agency words
    "staffing", "staff", "recruiting", "recruitment", "recruiters", "talent",
)


def is_staffing_agency(company: object) -> bool:
    """True when the employer name is a staffing or recruiting agency."""
    name = normalize_company(company)
    return bool(name) and bool(_pattern(STAFFING_AGENCIES).search(name))


# EXAMPLE configuration: the target areas. Edit these lists to your own search.
# Home region: the Research Triangle, North Carolina (Wake, Durham and Orange counties).
# One place list for every stage (pull, dashboard queue, validator). Whole-word matches, so
# "Durhamville, NY" is not Durham.
HOME_REGION_PLACES = (
    "raleigh-durham", "research triangle", "research triangle park", "rtp", "raleigh", "north raleigh",
    "brier creek", "durham", "durham county", "wake county", "wake forest", "knightdale", "rolesville",
    "fuquay-varina", "fuquay varina", "rougemont", "chapel hill", "carrboro", "efland", "mebane",
)
# Names shared with places in other states count only when followed by NC or North Carolina.
HOME_REGION_AMBIGUOUS = (
    "cary", "apex", "morrisville", "garner", "holly springs", "wendell", "zebulon", "hillsborough", "bahama",
    "new hill", "willow spring", "orange county",
)
_HOME_REGION_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in HOME_REGION_PLACES) + r")\b", re.I)
_HOME_REGION_AMBIGUOUS_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in HOME_REGION_AMBIGUOUS) + r"),?\s+(nc|north carolina)\b", re.I
)
# A home-region place followed by NC or North Carolina, for reading the area out of free text
# ("Available Locations: Hybrid - Rougemont, NC").
HOME_REGION_IN_TEXT = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in HOME_REGION_PLACES + HOME_REGION_AMBIGUOUS) + r"),?\s+(nc|north carolina)\b",
    re.I,
)


def is_home_region(location: object) -> bool:
    """True when a location string names a place in the home region (here Wake, Durham or Orange County, NC)."""
    loc = _text(location)
    return bool(_HOME_REGION_RE.search(loc) or _HOME_REGION_AMBIGUOUS_RE.search(loc))


# Second target metro (EXAMPLE: Greater Minneapolis; on site, hybrid or remote all count): Minneapolis, Saint Paul
# and the suburbs around the I-494/I-694 loop. Most suburb names also exist in other states, so those count only
# when followed by MN or Minnesota.
SECOND_METRO_PLACES = (
    "minneapolis", "saint paul", "st. paul", "st paul", "twin cities", "eden prairie", "edina", "minnetonka",
    "maple grove", "eagan", "burnsville", "lakeville", "shakopee", "chanhassen", "coon rapids",
)
SECOND_METRO_AMBIGUOUS = (
    "bloomington", "plymouth", "richfield", "woodbury", "roseville", "blaine", "andover", "fridley", "hopkins",
    "golden valley", "brooklyn park", "brooklyn center", "apple valley", "maplewood", "oakdale", "shoreview",
    "new brighton", "white bear lake", "inver grove heights", "cottage grove", "prior lake", "savage", "crystal",
    "new hope", "robbinsdale", "columbia heights", "wayzata", "chaska", "rosemount", "mendota heights",
)
_SECOND_METRO_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in SECOND_METRO_PLACES) + r")\b", re.I)
_SECOND_METRO_AMBIGUOUS_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in SECOND_METRO_AMBIGUOUS) + r"),?\s+(mn|minnesota)\b", re.I
)
# A second-metro place followed by MN or Minnesota, for reading the area out of free text.
SECOND_METRO_IN_TEXT = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in SECOND_METRO_PLACES + SECOND_METRO_AMBIGUOUS) + r"),?\s+(mn|minnesota)\b",
    re.I,
)


def is_second_metro(location: object) -> bool:
    """True when a location string names Minneapolis, Saint Paul or a listed suburb."""
    loc = _text(location)
    return bool(_SECOND_METRO_RE.search(loc) or _SECOND_METRO_AMBIGUOUS_RE.search(loc))


def is_local(location: object) -> bool:
    """True when a location is in one of the two target areas: the home region or the second metro."""
    return is_home_region(location) or is_second_metro(location)


# Remote and the area: one rule for every stage (2024-12-19; before it, four copies of different strictness).
# A board's remote flag is set for every row a "United States, remote" search returns, onsite jobs in Charlotte
# included, so the flag alone counts only when the location is nothing more specific than the country. Every
# lane that knows a row is remote says so in the location ("Remote (Birmingham, AL)").
_COUNTRY_ONLY = {"", "us", "usa", "united states", "us, united states", "anywhere", "anywhere in the u.s.", "worldwide",
                 "anywhere in the world", "global"}


def remote_location(location: object) -> str:
    """The location text for a row the board returned as remote: "Remote (Birmingham, AL)", or "Remote"."""
    where = _text(location)
    if "remote" in where.lower():
        return where
    return "Remote" if where.lower() in _COUNTRY_ONLY else f"Remote ({where})"


def is_remote_row(row: dict) -> bool:
    loc = _text(row.get("location")).lower()
    if "remote" in loc:
        return True
    flag = str(row.get("is_remote") or "").strip().lower() in {"true", "1", "yes"}
    return flag and (loc in _COUNTRY_ONLY or loc.startswith("united states"))


def in_area(row: dict) -> bool:
    """The owner's place rule: remote, or a location in Raleigh-Durham (Durham, Wake, Orange) or Greater Minneapolis."""
    return is_remote_row(row) or is_local(row.get("location"))


# One pay table (2024-12-19; before it, five copies). CSV amounts are always yearly; pay_interval keeps the
# interval the board published ("hourly" flags an agency rate). The factors are JobSpy's.
PER_YEAR = {"hourly": 2080, "daily": 260, "weekly": 52, "biweekly": 26, "monthly": 12, "yearly": 1}
PAY_INTERVAL_ALIASES = {
    # USAJOBS sends codes, not words: PA, PH, PD, PW, BW, PM (2024-12-20; only PA was known before)
    "hourly": "hourly", "hour": "hourly", "hr": "hourly", "perhour": "hourly", "ph": "hourly",
    "daily": "daily", "day": "daily", "perday": "daily", "pd": "daily",
    "weekly": "weekly", "week": "weekly", "wk": "weekly", "perweek": "weekly", "pw": "weekly",
    "biweekly": "biweekly", "bi-weekly": "biweekly", "bw": "biweekly",
    "monthly": "monthly", "month": "monthly", "mo": "monthly", "permonth": "monthly", "pm": "monthly",
    "yearly": "yearly", "year": "yearly", "yr": "yearly", "annum": "yearly", "annual": "yearly", "annually": "yearly",
    "peryear": "yearly", "pa": "yearly",
}


def pay_interval(label: object) -> str:
    """The canonical interval ("hourly", "yearly", ...) for a board's label ("hr", "Per Hour", "YEAR"), or ""."""
    key = re.sub(r"[\s_]+", "", _text(label).lower())
    return PAY_INTERVAL_ALIASES.get(key, "")


def yearly_factor(label: object) -> int | None:
    """What multiplies an amount in `label`'s interval up to a year: 1 for blank (CSV amounts are yearly), None
    for a label no board uses."""
    if not _text(label):
        return 1
    interval = pay_interval(label)
    return PER_YEAR.get(interval) if interval else None


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else re.sub(r"\s+", " ", text)


def role_key(row: dict) -> tuple[str, str]:
    return normalize_company(row.get("company")), normalize_title(row.get("title"))


def daily_csv(root: Path | str | None = None, day: dt.date | None = None) -> Path:
    base = Path(root) if root is not None else project_root()
    return base / "weekly-sweeps" / f"linkedin-raw-{day or dt.date.today()}.csv"


def lock_path(root: Path | str | None = None, day: dt.date | None = None) -> Path:
    return daily_csv(root, day).with_suffix(".csv.lock")


def _acquire_lock(handle, timeout: float, poll: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring CSV lock after {timeout:g}s")
            time.sleep(poll)


def _release_lock(handle) -> None:
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _clean_row(row: dict) -> dict[str, str]:
    cleaned = {}
    for column in CSV_COLUMNS:
        value = row.get(column, "")
        if isinstance(value, bool):
            value = str(value).lower()
        cleaned[column] = clean_description(value) if column == "description" else _text(value)
    return cleaned


BACKFILL_FIELDS = ("description", "job_url_direct")
PAY_FIELDS = ("min_amount", "max_amount", "pay_interval")


def _backfill(kept: dict, later: dict) -> None:
    """A later source for a known role fills what the first row lacked (2024-12-19): the text, the direct
    link, and the pay fields as a unit. The first row keeps its own link and site."""
    for field in BACKFILL_FIELDS:
        if not (kept.get(field) or "").strip() and (later.get(field) or "").strip():
            kept[field] = later[field]
    amounts = ("min_amount", "max_amount")   # a bare pay_interval is not pay: Indeed writes one with blank amounts (2024-12-20)
    if not any((kept.get(f) or "").strip() for f in amounts) and any((later.get(f) or "").strip() for f in amounts):
        for field in PAY_FIELDS:
            kept[field] = later.get(field, "")


def _merge_locked(rows: list[dict], csv_path: Path) -> dict:
    existing: list[dict] = []
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames not in (CSV_COLUMNS, LEGACY_CSV_COLUMNS, LEGACY_CSV_COLUMNS_V1):  # a legacy file gains the columns
                raise ValueError(
                    f"CSV header mismatch: expected {CSV_COLUMNS!r}, got {reader.fieldnames!r}"
                )
            existing = [dict(row) for row in reader]

    index = {role_key(row): row for row in existing}
    added = 0
    for raw in rows:
        row = _clean_row(raw)
        key = role_key(row)
        if not all(key):
            continue
        if key in index:
            _backfill(index[key], row)
            continue
        index[key] = row
        existing.append(row)
        added += 1

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{csv_path.name}.", suffix=".tmp", dir=csv_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(existing)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, csv_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"path": str(csv_path), "added": added, "total": len(existing)}


def merge_rows(
    rows: Iterable[dict],
    root: Path | str | None = None,
    *,
    timeout: float = 60,
    poll: float = 1,
) -> dict:
    csv_path = daily_csv(root)
    sidecar = lock_path(root)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sidecar.open("a+b") as handle:
        if sidecar.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        _acquire_lock(handle, timeout, poll)
        try:
            return _merge_locked(list(rows), csv_path)
        finally:
            _release_lock(handle)
