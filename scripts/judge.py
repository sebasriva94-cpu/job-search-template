"""The judge: label each new posting Apply / Maybe / Skip with quoted evidence, in verdicts.db.

Per row of the day's CSV (weekly-sweeps/linkedin-raw-YYYY-MM-DD.csv):
1. The code gate (judge_store.gate): title vocabulary, Raleigh-Durham or remote, pay floor, text. A row
   that fails title, place or pay gets a gate verdict (label Skip, model "gate") and is never sent to
   the model.
2. A gate-passer without posting text has its page fetched through Bright Data under the shared
   limits (scripts\\validate_listing.controlled_unlocker_get); still no text = Unscored.
3. One model call per posting: the rubric plus candidate profile as a cached system prefix, the posting
   as the user turn, a JSON schema on the output (scripts\\judge_schema.json). The model only labels
   each dimension strong / partial / weak with a copied phrase as evidence; it never decides.
4. Code verifies every evidence phrase against the posting (exact after normalisation, else fuzzy at or
   above 0.90). Unverified evidence caps that dimension at partial. Confidence is a rule: supported
   (all five verified), partial (three or four), unknown (fewer, or thin text); unknown never Applies.
5. Code computes the weighted score (experience 35, skills 25, archetype 15, culture 15, pay 10; strong
   = full, partial = half, weak = 0) and the label: Apply at or above 75 with experience and skills
   not weak and nothing blocking; Skip under 45 or blocked; otherwise Maybe. Blockers: a clearance or
   language red flag, four or more missing core requirements. An injection notice caps at Maybe.
6. Verdicts are keyed on (job_key, profile_version, rubric_version, prompt_version, model_id); a
   re-run for the same key makes no call. (Until 2024-12-17 the keyword band from the job-agent
   database was logged beside the label when the two disagreed; that database is retired and the
   disagreement table is kept only as an archive.)

Mode: real time (the night chain needs verdicts before the render and the 08:30 brief; the 1-hour
cached prefix covers the run). A Batch API mode at half price for re-scoring passes, where an hour's
latency does not matter, is planned. --pilot N judges N rows in real time and
prints token usage and rate-limit headers. --dry-run lists what would be sent. A missing
ANTHROPIC_API_KEY (process or Windows user environment) skips the step cleanly.

Usage: python scripts\\judge.py [--date YYYY-MM-DD] [--model claude-sonnet-5-5] [--pilot N] [--dry-run]
       python scripts\\judge.py --rescore   (re-derive stored labels under the current code rules, free)
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

try:
    from scripts import judge_store, rate_control
    from scripts.sweep_common import CSV_COLUMNS, clean_description, project_root
except ModuleNotFoundError:
    import judge_store
    import rate_control
    from sweep_common import CSV_COLUMNS, clean_description, project_root

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 2048
MAX_PER_NIGHT = 400
MAX_COST_PER_NIGHT = 5.0                 # dollars, estimated before sending
TIME_BUDGET_MINUTES = 25                 # the step's cap is 30 min; stop cleanly before it, the rest waits for the next run
TEXT_FETCH_TIMEOUT = 25                  # seconds for one Bright Data text fetch, no retry: a bad row costs half a minute, not two
WEIGHTS = {"experience": 35, "skills": 25, "archetype": 15, "culture_logistics": 15, "compensation": 10}
POINTS = {"strong": 1.0, "partial": 0.5, "weak": 0.0}
APPLY_AT, SKIP_UNDER = 75, 45
FUZZY_MIN = 0.90
PROVIDER = "anthropic"
# per million tokens, 2024-12-13 (platform.claude.com/pricing): input, cache write 1h, cache read, output
PRICES = {
    "claude-haiku-5-5": (0.10, 0.20, 0.01, 0.50),
    "claude-sonnet-5-5": (2.00, 4.00, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 8.00, 0.20, 20.00),
}
HERE = Path(__file__).resolve().parent


# -- versions and prompt ---------------------------------------------------------------------------

# The candidate's working languages (ISO 639-1). A posting in any other language is blocked. EXAMPLE: edit to
# your own, e.g. ("en", "es").
WORKING_LANGUAGES = ("en",)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def load_prompt(root: Path) -> dict:
    rubric = (HERE / "judge_prompt.md").read_text(encoding="utf-8")
    match = re.search(r"rubric_version:\s*(\S+)", rubric)
    profile = (root / "candidate-profile.md").read_text(encoding="utf-8")
    schema = json.loads((HERE / "judge_schema.json").read_text(encoding="utf-8"))
    system = rubric + "\n\n" + profile.strip() + "\n"
    return {
        "system": system,
        "schema": schema,
        "rubric_version": match.group(1) if match else "unknown",
        "prompt_version": _digest(rubric + json.dumps(schema, sort_keys=True)),
        "profile_version": _digest(profile),
    }


def posting_text(row: dict) -> str:
    pay = " to ".join(x for x in (str(row.get("min_amount") or ""), str(row.get("max_amount") or "")) if x and x != "nan")
    lines = [
        f"Title: {row.get('title', '')}",
        f"Company: {row.get('company', '')}",
        f"Location (from the aggregator, may be wrong): {row.get('location', '')}",
        f"Remote flag: {row.get('is_remote', '')}",
        f"Pay as listed: {pay} {row.get('pay_interval', '')}".rstrip(),
        f"Source: {row.get('site', '')}, posted {row.get('date_posted', '')}",
        "",
        "Posting text:",
        str(row.get("description") or ""),
    ]
    return "\n".join(lines)


def request_params(prompt: dict, row: dict, model: str) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": prompt["schema"]}},
        "system": [{"type": "text", "text": prompt["system"], "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": posting_text(row)}],
    }


# -- evidence verification and scoring --------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")
_QUOTES = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"'}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_DASHES).translate(_QUOTES).lower()
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(quote: str, posting: str) -> str:
    """'exact', 'fuzzy', 'missing' or 'none' (no quote given)."""
    needle, hay = normalize(quote), normalize(posting)
    if not needle:
        return "none"
    if needle in hay:
        return "exact"
    words, hay_words = needle.split(" "), hay.split(" ")
    size = len(words)
    best = 0.0
    for span in {max(1, size - 1), size, size + 1}:
        for start in range(0, max(1, len(hay_words) - span + 1)):
            window = " ".join(hay_words[start:start + span])
            if abs(len(window) - len(needle)) > max(12, len(needle) // 3):
                continue
            ratio = difflib.SequenceMatcher(None, needle, window).ratio()
            if ratio > best:
                best = ratio
                if best >= 0.995:
                    break
        if best >= FUZZY_MIN:
            break
    return "fuzzy" if best >= FUZZY_MIN else "missing"


BLOCKING_FLAG = re.compile(r"clearance|language", re.I)
CLEARANCE_ACTIVE = re.compile(r"\b(active|current|existing)\b|\bmust (hold|possess|have)\b|required to start", re.I)
CLEARANCE_OBTAIN = re.compile(r"\b(obtain\w*|eligib\w*|sponsor\w*)\b", re.I)


def flag_blocks(flag: dict, posting: str | None) -> bool:
    """A red flag forces a Skip only when it names a condition the rubric blocks on and, when the posting is at
    hand, only when its sentence is really there. 2024-12-20: the word clearance anywhere blocked 61 verdicts, at
    least four of them 'ability to obtain', which the rubric allows; red-flag quotes were never verified."""
    text = f"{flag.get('flag', '')} {flag.get('evidence', '')}"
    if not BLOCKING_FLAG.search(text):
        return False
    if re.search(r"clearance", text, re.I) and (CLEARANCE_OBTAIN.search(text) or not CLEARANCE_ACTIVE.search(text)):
        return False
    if posting is not None and verify_quote(str(flag.get("evidence", "")), posting) not in ("exact", "fuzzy"):
        return False
    return True
# The label rules below live in code, outside the verdict key (which hashes the rubric, the schema, the
# profile and the model). This version is stamped on every verdict and `--rescore` re-derives label and
# score from the stored form when it changes, with no model call (2024-12-19 review, ADR-004).
LABEL_RULE_VERSION = "2024-12-20a"
FACETS = {"level": ("entry", "target", "senior", "unstated"),
          "office_days": ("remote", "hybrid_3_or_fewer", "hybrid_4_plus", "onsite", "unstated")}


def derive_label(dims: dict, confidence: str, blocked: bool, missing: list, injection: str, facets: dict | None = None) -> tuple[str, int]:
    """(label, score) from verified dimension labels: the one place the Apply / Maybe / Skip rule lives.

    `dims` carries label and points per dimension (as stored in verdicts.db), so a stored verdict can be
    re-derived without the posting text."""
    score = int(round(sum(d["points"] for d in dims.values())))
    # First night (2024-12-13): a weak dimension cost only 10 to 15 points, so a Tier 1 seat, an agency
    # contract-to-hire and an under-floor hourly role all reached Apply at 80+. Apply now needs no weak
    # dimension at all; a weak one is a question for the owner (Maybe), never an automatic yes.
    any_weak = any(d["label"] == "weak" for d in dims.values())
    # 2024-12-19 (the owner's decision after the first gold round): a seat the posting itself calls entry level,
    # Level I or Tier 1 is a Skip, not a question, when the archetype is weak too. A Tier 2 role that
    # merely carries a junior-sounding title stays a Maybe: the level facet must be verified in the text.
    level = (facets or {}).get("level") or {}
    stated_entry = level.get("label") == "entry" and level.get("evidence_status") in ("exact", "fuzzy")
    if blocked or score < SKIP_UNDER or (dims["archetype"]["label"] == "weak" and stated_entry):
        label = "Skip"
    elif (score >= APPLY_AT and not any_weak and confidence != "unknown" and not injection
          and len(missing) <= 1):  # one missing core requirement is not a skip; two or three are a question (Maybe)
        label = "Apply"
    else:
        label = "Maybe"
    return label, score


def decide(form: dict, posting: str, thin: bool = False) -> dict:
    """Score and label a validated form in code; returns the verdict fields to store."""
    dims, provenance, verified = {}, {}, 0
    for name, weight in WEIGHTS.items():
        item = form["dimensions"][name]
        label = str(item.get("label", "weak")).lower()
        if label not in POINTS:
            label = "weak"
        status = verify_quote(item.get("evidence", ""), posting)
        provenance[name] = status
        if status in ("exact", "fuzzy") or (status == "none" and label != "strong"):
            verified += 1  # a partial or weak call on a posting that says nothing is supported by the silence
        elif label == "strong":
            label = "partial"  # a strong claim needs evidence the posting actually contains
        dims[name] = {"label": label, "points": round(weight * POINTS[label], 1),
                      "evidence": item.get("evidence", ""), "reason": item.get("reason", ""), "evidence_status": status}
    # Facets (2024-12-19): what the posting says about the seat's level and its office days, each with a
    # quote verified like the dimensions. They carry no points; derive_label reads the level facet.
    facets = {}
    for name, allowed in FACETS.items():
        item = form.get(name) if isinstance(form.get(name), dict) else {}
        label = str(item.get("label", "unstated")).lower()
        if label not in allowed:
            label = "unstated"
        status = verify_quote(item.get("evidence", ""), posting)
        facets[name] = {"label": label, "evidence": item.get("evidence", ""), "reason": item.get("reason", ""),
                        "evidence_status": status}
    confidence = "unknown" if thin else "supported" if verified == 5 else "partial" if verified >= 3 else "unknown"
    red_flags = [f for f in form.get("red_flags", []) if isinstance(f, dict)]
    blocked = any(flag_blocks(f, posting) for f in red_flags)
    language = str(form.get("language", "en")).lower()[:2]
    if language not in WORKING_LANGUAGES + ("",):
        blocked = True
    missing = [m for m in form.get("missing_core", []) if isinstance(m, str) and m.strip()]
    if len(missing) >= 4:
        blocked = True
    injection = str(form.get("injection_notice", ""))[:300]
    label, score = derive_label(dims, confidence, blocked, missing, injection, facets)
    return {"label": label, "score": score, "confidence": confidence, "dimensions": dims, "provenance": provenance,
            "summary_line": str(form.get("summary_line", ""))[:160], "missing_core": missing, "red_flags": red_flags,
            "injection_notice": injection, "blocked": blocked, "facets": facets,
            "label_rule_version": LABEL_RULE_VERSION, "form": form}


def rescore(conn) -> dict:
    """Re-derive label and score for every stored model verdict from its saved dimensions and form, under
    the current LABEL_RULE_VERSION, with no model call. Gate and Unscored rows are left alone. Returns the
    counts of rows seen, changed and skipped (no stored form) and the label moves."""
    seen = changed = skipped = 0
    moves: dict[str, int] = {}
    rows = conn.execute("SELECT job_key, profile_version, rubric_version, prompt_version, model_id, label, score, "
                        "confidence, dimensions, provenance FROM verdict WHERE model_id != 'gate' AND label != 'Unscored'").fetchall()
    for row in rows:
        seen += 1
        try:
            dims = json.loads(row["dimensions"] or "{}")
            prov = json.loads(row["provenance"] or "{}")
        except ValueError:
            skipped += 1
            continue
        if not dims or not isinstance(prov.get("form"), dict) or any(name not in dims for name in WEIGHTS):
            skipped += 1
            continue
        missing = [m for m in prov.get("missing_core", []) if isinstance(m, str) and m.strip()]
        # blocked is re-derived from the stored flags and language under the current rule (the posting text is
        # not stored, so the quote cannot be re-verified here); four missing core items still block
        form = prov["form"]
        red_flags = [f for f in form.get("red_flags", []) if isinstance(f, dict)]
        language = str(form.get("language", "en")).lower()[:2]
        blocked = any(flag_blocks(f, None) for f in red_flags) or language not in WORKING_LANGUAGES + ("",) or len(missing) >= 4
        prov["blocked"] = blocked
        label, score = derive_label(dims, row["confidence"] or "unknown", blocked, missing,
                                    str(prov.get("injection_notice") or ""), prov.get("facets") or {})
        prov["label_rule_version"] = LABEL_RULE_VERSION
        if label != row["label"] or score != row["score"]:
            changed += 1
            moves[f"{row['label']}->{label}"] = moves.get(f"{row['label']}->{label}", 0) + 1
        conn.execute("UPDATE verdict SET label=?, score=?, provenance=? WHERE job_key=? AND profile_version=? AND "
                     "rubric_version=? AND prompt_version=? AND model_id=?",
                     (label, score, json.dumps(prov, ensure_ascii=False), row["job_key"], row["profile_version"],
                      row["rubric_version"], row["prompt_version"], row["model_id"]))
    conn.commit()
    return {"seen": seen, "changed": changed, "skipped": skipped, "moves": moves, "label_rule_version": LABEL_RULE_VERSION}


def parse_message(message) -> tuple[dict | None, str]:
    """(form, error): the JSON form from a Message, or why it is unusable."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        return None, "refusal"
    if stop == "max_tokens":
        return None, "max_tokens"
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        form = json.loads(text)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(form, dict) or not isinstance(form.get("dimensions"), dict):
        return None, "schema"
    if any(name not in form["dimensions"] for name in WEIGHTS):
        return None, "schema"
    return form, ""


def usage_dict(message) -> dict:
    usage = getattr(message, "usage", None)
    out = {}
    for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        out[field] = int(getattr(usage, field, 0) or 0)
    return out


def cost_of(usage: dict, model: str, batch: bool = False) -> float:
    base, write, read, output = PRICES.get(model, PRICES[DEFAULT_MODEL])
    dollars = (usage.get("input_tokens", 0) * base + usage.get("cache_creation_input_tokens", 0) * write
               + usage.get("cache_read_input_tokens", 0) * read + usage.get("output_tokens", 0) * output) / 1_000_000
    return dollars / 2 if batch else dollars


# -- inputs ----------------------------------------------------------------------------------------

def day_csv(root: Path, day: str) -> Path | None:
    plain = root / "weekly-sweeps" / f"linkedin-raw-{day}.csv"
    return plain if plain.exists() else None


def read_rows(path: Path) -> list[dict]:
    import csv
    seen, out = set(), []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = judge_store.job_key(row)
            if not key.strip("|") or key in seen:
                continue
            seen.add(key)
            out.append({**row, "job_key": key})
    return out


def gold_rows_with_text(root: Path) -> list[dict]:
    """Gold-labelled postings with text: from the daily CSVs first, then (2024-12-20) from the tracked export
    dashboard\\gold-labels.json for labels whose CSV has been pruned, so the calibration set survives retention."""
    try:
        from scripts import gold_form, gold_import
    except ModuleNotFoundError:
        import gold_form
        import gold_import
    with judge_store.open_db(root) as conn:
        keys = judge_store.gold_keys(conn)
    rows = [row for row in gold_form.candidate_rows(root) if row["job_key"] in keys]
    found = {row["job_key"] for row in rows}
    export = root / gold_import.GOLD_EXPORT
    if export.exists():
        try:
            labels = json.loads(export.read_text(encoding="utf-8")).get("labels", [])
        except (ValueError, OSError, AttributeError):
            labels = []
        for item in labels:
            key = item.get("job_key")
            if key in keys and key not in found and (item.get("posting_text") or "").strip():
                row = {c: "" for c in CSV_COLUMNS}
                row.update(item.get("posting") or {})
                row.update(title=item.get("title") or "", company=item.get("company") or "", description=item["posting_text"],
                           job_url=row.get("job_url") or item.get("url") or "", job_key=key, file_date=item.get("posting_date") or "")
                rows.append(row)
    return rows


def user_env(name: str) -> str:
    """A process variable, else the Windows user variable (the Task Scheduler session does not always
    carry user variables; the chain reads them the same way)."""
    value = os.environ.get(name, "")
    if value:
        return value
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return str(winreg.QueryValueEx(handle, name)[0])
    except Exception:  # noqa: BLE001
        return ""


def api_key() -> str:
    return user_env("ANTHROPIC_API_KEY")


def make_client(key: str):
    """A personal key (sk-ant-usr-...) is not scoped to a workspace: every request must name one in the
    anthropic-workspace-id header, read from ANTHROPIC_WORKSPACE_ID. A workspace-scoped key needs nothing."""
    import anthropic
    headers = {}
    workspace = user_env("ANTHROPIC_WORKSPACE_ID")
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=120.0, default_headers=headers or None)


# Hosts whose job pages never render through the Unlocker (Workday builds the page in the browser): 46 no-text
# verdicts and not one with text by 2024-12-20. Their text comes from the ATS API in target_sweep instead; a row that
# still lacks it is Unscored without a paid request.
NO_FETCH_HOSTS = ("myworkdayjobs.com",)


def fetch_text(url: str, fetcher=None) -> str:
    """Posting text through Bright Data under the shared limits; '' when nothing readable came back, and '' without
    a request for a host in NO_FETCH_HOSTS."""
    if not url or any(host in url.lower() for host in NO_FETCH_HOSTS):
        return ""
    if fetcher is None:
        try:
            from scripts import validate_listing
        except ModuleNotFoundError:
            import validate_listing
        fetcher = lambda target: validate_listing.controlled_unlocker_get(target, timeout=TEXT_FETCH_TIMEOUT, retry_transient=False)  # noqa: E731
    page = fetcher(url)
    html = page.get("html") or page.get("body") or ""
    if not html:
        return ""
    body = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", html)
    return clean_description(body)


# -- the run ---------------------------------------------------------------------------------------

class Run:
    def __init__(self, root: Path, day: str, model: str, client=None, fetcher=None, now=None, clock=None):
        self.root, self.day, self.model, self.client, self.fetcher = root, day, model, client, fetcher
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.clock = clock or time.monotonic
        self.deferred = 0  # gate-passers not reached inside the time budget
        self.prompt = load_prompt(root)
        self.calls, self.cost, self.errors, self.written = 0, 0.0, [], {"judged": 0, "gate": 0, "unscored": 0, "no_text": 0, "fetch_skipped": 0, "fetched": 0}
        self.usage_total = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}

    def key_tuple(self, row: dict, model: str) -> tuple:
        return (row["job_key"], self.prompt["profile_version"], self.prompt["rubric_version"], self.prompt["prompt_version"], model)

    def have_verdict(self, conn, row: dict, model: str) -> bool:
        # An Unscored row (no text that night, a transient API error, a refusal) is not a verdict: the next
        # run judges the posting again. Until 2024-12-19 it was terminal, and 108 postings, 80 of them from
        # one Bright Data pause, were never looked at again.
        return conn.execute(
            "SELECT 1 FROM verdict WHERE job_key=? AND profile_version=? AND rubric_version=? AND prompt_version=? AND model_id=?"
            " AND label != 'Unscored'",
            self.key_tuple(row, model)).fetchone() is not None

    def fetched_today(self, conn, row: dict) -> bool:
        # A no-text Unscored row written today means the page was already fetched and gave nothing; the next
        # day's run tries again (the row's run_date is today's, so tomorrow this returns False).
        return conn.execute(
            "SELECT 1 FROM verdict WHERE job_key=? AND model_id=? AND run_date=? AND label='Unscored'"
            " AND provenance LIKE '%\"reason\": \"no_text\"%'", (row["job_key"], self.model, self.day)).fetchone() is not None

    def store(self, conn, row: dict, model: str, verdict: dict, usage: dict | None = None) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO verdict (job_key, run_date, profile_version, rubric_version, prompt_version, model_id,
               label, score, confidence, summary_line, dimensions, provenance, usage, title, company, url, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["job_key"], self.day, *self.key_tuple(row, model)[1:], verdict["label"], verdict.get("score"), verdict.get("confidence"),
             verdict.get("summary_line", ""), json.dumps(verdict.get("dimensions", {}), ensure_ascii=False),
             json.dumps({k: v for k, v in verdict.items() if k in ("provenance", "missing_core", "red_flags", "injection_notice", "blocked",
                                                                  "reason", "facets", "label_rule_version", "form")}, ensure_ascii=False),
             json.dumps(usage or {}), row.get("title", ""), row.get("company", ""), row.get("job_url", ""),
             self.now().isoformat(timespec="seconds")))

    def judge_one(self, row: dict) -> tuple[dict | None, dict, str]:
        """(verdict, usage, error) for one posting in real time."""
        params = request_params(self.prompt, row, self.model)
        try:
            message = self.client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001
            return None, {}, f"{type(exc).__name__}: {rate_control.sanitize(exc)}"
        self.calls += 1
        usage = usage_dict(message)
        for field, value in usage.items():
            self.usage_total[field] += value
        self.cost += cost_of(usage, self.model)
        form, error = parse_message(message)
        if form is None:
            return None, usage, error
        # Verify against everything the model saw (title, pay and location lines plus the text): on the
        # first night a third of the "missing" quotes were the title or the pay line, not the description.
        return decide(form, posting_text(row)), usage, ""

    def run(self, rows: list[dict], dry_run: bool = False, limit: int = MAX_PER_NIGHT, budget_s: float | None = None) -> dict:
        to_judge, gate_skips, unscored = [], [], []
        with judge_store.open_db(self.root) as conn:
            for row in rows:
                checks = judge_store.gate(row)
                row["gate"] = checks
                if not judge_store.passes_gate(checks):
                    if not self.have_verdict(conn, row, "gate"):
                        failed = ", ".join(name for name in ("title", "place", "pay") if not checks[name])
                        self.store(conn, row, "gate", {"label": "Skip", "score": 0, "confidence": "gate",
                                                       "summary_line": f"Failed the code gate: {failed}.", "reason": failed})
                        self.written["gate"] += 1
                    continue
                if self.have_verdict(conn, row, self.model):
                    continue
                to_judge.append(row)
            if dry_run:
                return self.summary(to_judge, dry_run=True)
            if len(to_judge) > limit:
                to_judge.sort(key=lambda r: r.get("date_posted") or "", reverse=True)
                self.errors.append(f"{len(to_judge) - limit} gate-passers over the nightly cap of {limit} left for tomorrow")
                to_judge = to_judge[:limit]
            started = self.clock()
            for position, row in enumerate(to_judge):
                if budget_s is not None and self.clock() - started >= budget_s:
                    # 2024-12-18: judge-late was killed at the step's 30-minute cap, 85 verdicts in, no summary,
                    # the step marked failed. Verdicts are committed one by one, so stopping here loses nothing.
                    self.deferred = len(to_judge) - position
                    self.errors.append(f"time budget of {budget_s / 60:.0f} min spent; {self.deferred} gate-passer(s) left for the next run")
                    break
                if not row["gate"]["text"]:
                    if self.fetched_today(conn, row):
                        # one paid fetch attempt per posting per day (2024-12-20: three passes re-fetched the same 21
                        # Workday rows and spent the Bright Data budget); the earlier pass's Unscored row stands
                        self.written["fetch_skipped"] += 1
                        continue
                    text = fetch_text(row.get("job_url", ""), self.fetcher)
                    if text and len(text) >= judge_store.MIN_TEXT_CHARS:   # a shell page is no text (2024-12-20)
                        row["description"], self.written["fetched"] = text, self.written["fetched"] + 1
                    else:
                        self.store(conn, row, self.model, {"label": "Unscored", "score": None, "confidence": "unknown",
                                                           "summary_line": "No posting text could be read.", "reason": "no_text"})
                        self.written["unscored"] += 1
                        self.written["no_text"] += 1   # never reached the model: not evidence of an API problem
                        unscored.append(row)
                        continue
                if self.cost >= MAX_COST_PER_NIGHT:
                    self.errors.append(f"nightly cost cap ${MAX_COST_PER_NIGHT:.2f} reached; the rest waits for tomorrow")
                    break
                verdict, usage, error = self.judge_one(row)
                if verdict is None:
                    self.store(conn, row, self.model, {"label": "Unscored", "score": None, "confidence": "unknown",
                                                       "summary_line": f"The judge could not score this posting ({error}).", "reason": error}, usage)
                    self.written["unscored"] += 1
                    self.errors.append(error)
                    unscored.append(row)
                    if error.startswith(("AuthenticationError", "PermissionDeniedError")):
                        break
                    continue
                self.store(conn, row, self.model, verdict, usage)
                self.written["judged"] += 1
                conn.commit()
        return self.summary(to_judge)

    def summary(self, to_judge: list[dict], dry_run: bool = False) -> dict:
        labels = {}
        if not dry_run:
            with judge_store.open_db(self.root) as conn:
                for label, n in conn.execute("SELECT label, COUNT(*) FROM verdict WHERE run_date=? GROUP BY label", (self.day,)):
                    labels[label] = n
        return {"candidates": len(to_judge), "calls": self.calls, "cost": round(self.cost, 4), "usage": self.usage_total,
                "written": self.written, "deferred": self.deferred, "labels": labels, "errors": self.errors[:5], "dry_run": dry_run}


def provider_state(result: dict, skipped_reason: str = "") -> dict:
    if skipped_reason:
        return {PROVIDER: {"state": rate_control.SKIPPED, "reason": skipped_reason, "consecutive_degraded_runs": 0}}
    judged, unscored = result["written"]["judged"], result["written"]["unscored"]
    # Rows with no fetchable text are Unscored without a model call; a pass whose only candidates are their retries
    # (2024-12-20, 21 Workday rows) is not an API failure. all_unscored means every model call came back unscored.
    model_unscored = unscored - result["written"].get("no_text", 0)
    auth = any(e.startswith(("AuthenticationError", "PermissionDeniedError")) for e in result["errors"])
    if auth or (result["candidates"] and judged == 0 and model_unscored > 0):
        state, reason = rate_control.FAILED, "auth_failed" if auth else "all_unscored"
    elif result["errors"]:
        state, reason = rate_control.DEGRADED, rate_control.sanitize(result["errors"][0], 60)
    else:
        state, reason = rate_control.OK, ""
    return {PROVIDER: {"state": state, "reason": reason, "consecutive_degraded_runs": 0}}


def pilot(run: Run, rows: list[dict], count: int) -> int:
    """Judge `count` gate-passers in real time and print what the cost model needs to know."""
    picked = [r for r in rows if judge_store.passes_gate(judge_store.gate(r)) and judge_store.gate(r)["text"]][:count]
    print(f"pilot: {len(picked)} postings on {run.model}")
    for index, row in enumerate(picked, 1):
        params = request_params(run.prompt, row, run.model)
        raw = run.client.messages.with_raw_response.create(**params)
        message = raw.parse()
        usage = usage_dict(message)
        run.cost += cost_of(usage, run.model)
        form, error = parse_message(message)
        verdict = decide(form, posting_text(row)) if form else None
        headers = {k: raw.headers.get(k) for k in ("anthropic-ratelimit-requests-limit", "anthropic-ratelimit-input-tokens-limit",
                                                    "anthropic-ratelimit-output-tokens-limit") if raw.headers.get(k)}
        print(f"{index}. {row.get('title')} @ {row.get('company')}: {usage}  stop={message.stop_reason}")
        if verdict:
            print(f"   -> {verdict['label']} score {verdict['score']} confidence {verdict['confidence']} provenance {verdict['provenance']}")
            print(f"   {verdict['summary_line']}")
        else:
            print(f"   -> unusable: {error}")
        if index == 1 and headers:
            print(f"   rate limits: {headers}")
    print(f"pilot cost ${run.cost:.4f}; per posting ${run.cost / max(1, len(picked)):.4f}; "
          f"200 a night real time about ${200 * run.cost / max(1, len(picked)):.2f}, batch half of that")
    return 0


def main(argv: list[str] | None = None, client=None, fetcher=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--pilot", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=MAX_PER_NIGHT)
    parser.add_argument("--minutes", type=float, default=TIME_BUDGET_MINUTES, help="stop cleanly after this long; 0 = no time budget")
    parser.add_argument("--gold", action="store_true", help="judge the gold-labelled postings instead of today's CSV")
    parser.add_argument("--rescore", action="store_true",
                        help="re-derive every stored verdict's label and score under the current label rules; no model call")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else project_root()
    if args.rescore:
        with judge_store.open_db(root) as conn:
            result = rescore(conn)
        moves = ", ".join(f"{k} {v}" for k, v in sorted(result["moves"].items())) or "none"
        print(f"rescore {result['label_rule_version']}: {result['seen']} verdicts, {result['changed']} changed "
              f"({moves}), {result['skipped']} without a stored form")
        return 0
    if args.gold:
        rows = gold_rows_with_text(root)
        print(f"judge --gold: {len(rows)} labelled postings with text")
    else:
        path = day_csv(root, args.date)
        if path is None:
            print(f"judge: no CSV for {args.date}")
            print(rate_control.status_line(provider_state({}, "no_csv")))
            return 0
        rows = read_rows(path)
    key = api_key()
    if client is None and not args.dry_run:
        if not key:
            print("judge: ANTHROPIC_API_KEY is not set; nothing judged (add it as a Windows user variable)")
            print(rate_control.status_line(provider_state({}, "api_key_missing")))
            return 0
        client = make_client(key)
    run = Run(root, args.date, args.model, client=client, fetcher=fetcher)
    if args.pilot:
        return pilot(run, rows, args.pilot)
    started = time.monotonic()
    result = run.run(rows, dry_run=args.dry_run, limit=args.limit, budget_s=args.minutes * 60 if args.minutes else None)
    if result["dry_run"]:
        print(f"judge dry run: {len(rows)} rows, {result['candidates']} would be judged on {args.model}, "
              f"{result['written']['gate']} new gate skips written")
        return 0
    print(f"judge {args.date}: {result['written']['judged']} judged, {result['written']['gate']} gate skips, "
          f"{result['written']['unscored']} unscored, {result['written']['fetched']} texts fetched, "
          f"{result['calls']} calls, ${result['cost']:.3f}, {time.monotonic() - started:.0f} s"
          + (f", {result['deferred']} deferred" if result["deferred"] else "") + f"; today's labels {result['labels']}")
    for error in result["errors"]:
        print(f"  ! {error}")
    providers = provider_state(result)
    print(rate_control.status_line(providers))
    return 1 if providers[PROVIDER]["state"] == rate_control.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
