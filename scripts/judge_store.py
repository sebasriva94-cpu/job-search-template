"""verdicts.db: the judge's verdicts, the gold set, the keyword-vs-judge disagreement log and sentinel runs.

One small SQLite file owned by the Python chain (dashboard\\verdicts.db, git-ignored), decision 5 of the
2024-12-13 plan: nothing here touches the job-agent Prisma database. Rows are keyed on the posting's
role key (normalized company | normalized title, scripts\\sweep_common.role_key), which is how the
curated dashboard data, pipeline.md and the daily CSV can all be joined without a URL.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import datetime as dt
import math
import re
import sqlite3
from pathlib import Path

try:
    from scripts.sweep_common import in_area, project_root, relevant, role_key, yearly_factor
except ModuleNotFoundError:
    from sweep_common import in_area, project_root, relevant, role_key, yearly_factor

DB_NAME = "verdicts.db"
LABELS = ("Apply", "Maybe", "Skip")
PAY_FLOOR = 70_000                      # yearly; the 2024-12-09 decision
MIN_TEXT_CHARS = 300                    # under this a description is "thin": not enough to judge

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdict (
  job_key TEXT NOT NULL, run_date TEXT NOT NULL, profile_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL, model_id TEXT NOT NULL, label TEXT NOT NULL, score INTEGER, confidence TEXT,
  summary_line TEXT, dimensions TEXT, provenance TEXT, usage TEXT, title TEXT, company TEXT, url TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (job_key, profile_version, rubric_version, prompt_version, model_id)
);
CREATE TABLE IF NOT EXISTS gold (
  job_key TEXT PRIMARY KEY, label TEXT NOT NULL, reason TEXT, source TEXT NOT NULL,
  title TEXT, company TEXT, url TEXT, labelled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disagreement (
  job_key TEXT NOT NULL, run_date TEXT NOT NULL, keyword_band TEXT, judge_label TEXT,
  sampled_for_label INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (job_key, run_date)
);
CREATE TABLE IF NOT EXISTS sentinel_run (
  run_date TEXT PRIMARY KEY, kappa REAL, apply_recall REAL, n INTEGER, note TEXT
);
"""


def job_key(row: dict) -> str:
    return "|".join(role_key(row))


def db_path(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else project_root()
    return base / "dashboard" / DB_NAME


def connect(root: Path | str | None = None) -> sqlite3.Connection:
    """An open connection with the schema in place; prefer `open_db`, which also closes it."""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def open_db(root: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """`with open_db(root) as conn:` commits on success and always closes (sqlite's own `with` never closes,
    which on Windows keeps the file locked)."""
    conn = connect(root)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _number(value: object) -> float | None:
    text = "" if value is None else str(value).strip().replace(",", "").replace("$", "")
    if not text or text.lower() == "nan":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if math.isnan(number) else number


def yearly_maximum(row: dict) -> float | None:
    """The posting's top yearly pay when it listed one, else None (unknown pay never fails the gate)."""
    high = _number(row.get("max_amount"))
    low = _number(row.get("min_amount"))
    amount = high if high is not None else low
    if amount is None:
        return None
    # The sweeps already write yearly-equivalent amounts and keep the original unit in pay_interval
    # ("$30/hr" arrives as 62400 hourly; 2024-12-13 first night: multiplying again let it pass the floor).
    # A small amount with a non-yearly unit is still a raw rate, so convert only then.
    factor = yearly_factor(row.get("pay_interval")) or 1
    return amount * factor if amount < 1000 else amount


_MONEY = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k)?\s*(?:(?:-|to|–|and)\s*\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k)?)?"
    r"\s*(?:/\s*|per\s+|an?\s+)?(hour|hr|year|yr|annum|annually|month|mo|week|wk)?", re.I)


def pay_from_text(text: str) -> float | None:
    """The highest yearly-equivalent pay figure written in a posting ("$20 to $30/hr", "$55,000 - $62,000",
    "$65k"), or None when the text names no pay. Used only when the CSV carries no pay fields."""
    best = None
    for match in _MONEY.finditer(text or ""):
        low, low_k, high, high_k, unit = match.groups()
        for raw, k in ((low, low_k), (high, high_k)):
            if not raw:
                continue
            value = float(raw.replace(",", "")) * (1000 if k else 1)
            if unit:
                value *= yearly_factor(unit) or 1
            elif value < 1000:         # a bare small number (a bonus, a stipend) is not a salary; hourly needs its unit
                continue
            if 15_000 <= value <= 400_000 and (best is None or value > best):
                best = value
    return best


def gate(row: dict) -> dict[str, bool]:
    """The code checks that run before any model call: title vocabulary, place, pay floor, text."""
    top = yearly_maximum(row)
    if top is None:  # 2024-12-13 pilot: "$20 to $30/hr" in the text with blank pay fields reached the model as Apply
        top = pay_from_text(str(row.get("description") or ""))
    return {
        "title": relevant(row.get("title")),
        "place": in_area(row),
        "pay": top is None or top >= PAY_FLOOR,
        "text": len(str(row.get("description") or "").strip()) >= MIN_TEXT_CHARS,
    }


def passes_gate(checks: dict[str, bool]) -> bool:
    return checks["title"] and checks["place"] and checks["pay"]


def near_miss(checks: dict[str, bool]) -> bool:
    """Exactly one of title, place, pay failed: the rows a judge must see labelled, not just obvious skips."""
    return sum(not checks[name] for name in ("title", "place", "pay")) == 1


def upsert_gold(conn: sqlite3.Connection, rows: list[dict], source: str, now: dt.datetime | None = None) -> int:
    """Store gold labels. A label from the form always wins; a seed never overwrites a form label."""
    stamp = (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    written = 0
    for row in rows:
        label = str(row.get("label") or "").strip().capitalize()
        if label not in LABELS:
            raise ValueError(f"gold label must be one of {LABELS}: {row.get('label')!r} for {row.get('title')!r}")
        key = row.get("job_key") or job_key(row)
        if not key.strip("|"):
            continue
        cursor = conn.execute(
            """INSERT INTO gold (job_key, label, reason, source, title, company, url, labelled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_key) DO UPDATE SET
                 label = excluded.label, reason = excluded.reason, source = excluded.source,
                 title = excluded.title, company = excluded.company, url = excluded.url,
                 labelled_at = excluded.labelled_at
               WHERE gold.source != 'form' OR excluded.source = 'form'""",
            (key, label, str(row.get("reason") or "").strip(), source, row.get("title") or "",
             row.get("company") or "", row.get("url") or row.get("job_url") or "", stamp),
        )
        written += cursor.rowcount
    conn.commit()
    return written


def gold_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM gold ORDER BY labelled_at, job_key")]


def gold_keys(conn: sqlite3.Connection) -> set[str]:
    return {row["job_key"] for row in conn.execute("SELECT job_key FROM gold")}
