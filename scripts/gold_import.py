"""Load gold labels into verdicts.db: the seeds from past decisions, and the form's exported JSON.

Seeds (decision 3, 2024-12-13): every curated Apply entry in dashboard\\job-dashboard-data.json is
Apply, every curated Skip entry is Skip; a pipeline.md row is Apply when its decision is Pursue or its
status says applied, submitted, interview, screen or offer, and Skip when the decision or status says
skip. Rows that were found and never acted on are not labels. A seed never overwrites a label the owner set
in the form.

Usage: python scripts\\gold_import.py --seed
       python scripts\\gold_import.py --labels "%USERPROFILE%\\Downloads\\gold-labels-YYYY-MM-DD.json"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path

try:
    from scripts import judge_store
    from scripts.sweep_common import project_root
except ModuleNotFoundError:
    import judge_store
    from sweep_common import project_root

APPLY_STATUS = re.compile(r"\b(applied|submitted|interview|screen|offer)\b", re.I)
SKIP_WORDS = re.compile(r"\bskip", re.I)
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def seed_curated(data: dict) -> list[dict]:
    rows = [{"label": "Apply", "title": e.get("title", ""), "company": e.get("company", ""),
             "url": e.get("apply_url", ""), "reason": "curated Apply"} for e in data.get("apply", [])]
    rows += [{"label": "Skip", "title": e.get("title", ""), "company": e.get("company", ""),
              "reason": (e.get("tag") or "curated Skip")[:80]} for e in data.get("skip", [])]
    return [r for r in rows if r["title"] and r["company"]]


def _cells(line: str) -> list[str]:
    return [LINK.sub(r"\1", cell).strip() for cell in line.strip().strip("|").split("|")]


def seed_pipeline(text: str) -> list[dict]:
    """Rows of the pipeline.md table (Hash | Company | Title | Location | Salary | Score | Decision | Status | ...)."""
    rows: list[dict] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if header is None:
            if "Company" in cells and "Decision" in cells:
                header = [c.lower() for c in cells]
            continue
        if set(line.strip()) <= {"|", "-", " "}:
            continue
        if len(cells) < len(header):
            continue
        row = dict(zip(header, cells))
        title = re.sub(r"\s*\(.*?\)\s*|\s*·.*$", "", row.get("title", "")).strip()
        company, decision, status = row.get("company", ""), row.get("decision", ""), row.get("status", "")
        if not title or not company:
            continue
        if SKIP_WORDS.search(decision) or SKIP_WORDS.search(status):
            label = "Skip"
        elif decision.lower().startswith("pursue") or APPLY_STATUS.search(status) or APPLY_STATUS.search(decision):
            label = "Apply"
        else:
            continue
        rows.append({"label": label, "title": title, "company": company, "reason": "pipeline"})
    return rows


def form_labels(payload: dict) -> list[dict]:
    rows = []
    for item in payload.get("labels", []):
        if not item.get("label"):
            continue
        rows.append({"job_key": item.get("job_key") or "", "label": item["label"], "reason": item.get("reason", ""),
                     "title": item.get("title", ""), "company": item.get("company", ""), "url": item.get("url", "")})
    return rows


GOLD_EXPORT = Path("dashboard") / "gold-labels.json"


# The CSV fields kept with each label so a gold row can be judged from the export alone (the gate reads them).
POSTING_FIELDS = ("location", "min_amount", "max_amount", "pay_interval", "is_remote", "date_posted", "job_url", "site")


def export_gold(conn, root: Path) -> Path:
    """Write the gold table to dashboard\\gold-labels.json, each label with the posting text it was made on.

    verdicts.db is git-ignored and the laptop has no other copy (2024-12-19 review), so the owner's labels, the only
    ground truth the judge is measured against, lived on one disk. This file is tracked: rows sorted by job_key,
    keys sorted, written through a temp file, identical when nothing changed, so git shows exactly what moved.
    The text comes from the newest daily CSV that holds the role (what the form showed the owner); when no CSV holds
    it any more, the text and the posting fields already in the file are kept (2024-12-20), so a gold row stays
    scorable after the CSVs are pruned or lost: judge.gold_rows_with_text reads this file as its fallback.
    --restore loads the file into an empty gold table."""
    try:
        from scripts import gold_form
    except ModuleNotFoundError:
        import gold_form
    text = {r["job_key"]: r for r in gold_form.candidate_rows(root)}
    path = root / GOLD_EXPORT
    previous: dict = {}
    if path.exists():
        try:
            previous = {r["job_key"]: r for r in json.loads(path.read_text(encoding="utf-8")).get("labels", [])}
        except (ValueError, OSError, KeyError, AttributeError):
            previous = {}
    rows = []
    for gold in sorted(judge_store.gold_rows(conn), key=lambda r: r["job_key"]):
        posting = text.get(gold["job_key"]) or {}
        old = previous.get(gold["job_key"]) or {}
        fields = {c: str(posting.get(c) or "") for c in POSTING_FIELDS} if posting else (old.get("posting") or {})
        rows.append({"job_key": gold["job_key"], "label": gold["label"], "reason": gold["reason"] or "",
                     "source": gold["source"], "title": gold["title"] or "", "company": gold["company"] or "",
                     "url": gold["url"] or "", "labelled_at": gold["labelled_at"],
                     "posting_text": posting.get("description") or old.get("posting_text") or "",
                     "posting_date": posting.get("file_date") or old.get("posting_date") or "",
                     "posting": fields})
    payload = {"schema": 1, "labels": rows,
               "note": "Export of the gold table in dashboard/verdicts.db; python scripts/gold_import.py --restore <this file>"}
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def restore_gold(conn, path: Path) -> int:
    """Load an export back into the gold table, seeds first and the form last so the owner's labels win as before,
    each row keeping the time it was labelled."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = sorted(payload.get("labels", []), key=lambda item: item.get("source") == "form")
    written = 0
    for item in items:
        stamp = dt.datetime.fromisoformat(item["labelled_at"]) if item.get("labelled_at") else None
        written += judge_store.upsert_gold(conn, [item], item.get("source") or "form", now=stamp)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", action="store_true", help="load the curated dashboard data and pipeline.md")
    parser.add_argument("--labels", default="", help="the JSON file the gold form exported")
    parser.add_argument("--restore", default="", help="load a dashboard\\gold-labels.json export into the gold table")
    parser.add_argument("--export", action="store_true", help="only rewrite dashboard\\gold-labels.json")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else project_root()
    if not (args.seed or args.labels or args.restore or args.export):
        parser.error("give --seed, --labels <file>, --restore <file> or --export")
    with judge_store.open_db(root) as conn:
        if args.restore:
            n0 = restore_gold(conn, Path(args.restore))
            print(f"restored {n0} gold labels from {args.restore}")
        if args.seed:
            data = json.loads((root / "dashboard" / "job-dashboard-data.json").read_text(encoding="utf-8"))
            curated = seed_curated(data)
            pipeline_path = root / "pipeline.md"
            pipeline = seed_pipeline(pipeline_path.read_text(encoding="utf-8")) if pipeline_path.exists() else []
            n1 = judge_store.upsert_gold(conn, curated, "curated")
            n2 = judge_store.upsert_gold(conn, pipeline, "pipeline")
            print(f"seeded {n1} curated and {n2} pipeline labels")
        if args.labels:
            payload = json.loads(Path(args.labels).read_text(encoding="utf-8"))
            n3 = judge_store.upsert_gold(conn, form_labels(payload), "form")
            print(f"loaded {n3} form labels from {args.labels}")
        total = conn.execute("SELECT label, COUNT(*) FROM gold GROUP BY label ORDER BY label").fetchall()
        print("gold set now:", ", ".join(f"{label} {n}" for label, n in total))
        exported = export_gold(conn, root)
        print(f"gold export: {exported} ({len(total) and sum(n for _, n in total)} rows); commit and push it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
