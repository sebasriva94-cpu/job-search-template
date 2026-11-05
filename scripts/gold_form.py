"""Build the gold-set labelling page: postings with text for the candidate to label Apply / Maybe / Skip.

The page is one HTML file: one card per
posting with title, company, location, pay and the description, three buttons and a one-word reason
box. Choices are kept in the browser (localStorage) and exported as one JSON file, which
scripts\\gold_import.py --labels loads into verdicts.db.

Stratified so the judge's hard cases are over-represented: 40 percent rows that pass the code gate
(title vocabulary, North Carolina or remote, pay floor), 30 percent near-misses (exactly one of those three
failed), 30 percent everything else. Only rows with real posting text are offered, and rows already
in the gold set are skipped, so the script can be re-run on later nights to add more.

Usage: python scripts\\gold_form.py [--count 100] [--seed 20261016] [--out reports\\gold-form-<date>.html]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import random
import re
from pathlib import Path

try:
    from scripts import judge_store
    from scripts.sweep_common import project_root
except ModuleNotFoundError:
    import judge_store
    from sweep_common import project_root

MIX = {"pass": 0.4, "near": 0.3, "other": 0.3}
DAILY_CSV = re.compile(r"^linkedin-raw-(\d{4}-\d{2}-\d{2})\.csv$")


def candidate_rows(root: Path) -> list[dict]:
    """Rows with posting text from every daily CSV, newest file first, one row per role key."""
    files = sorted((p for p in (root / "weekly-sweeps").glob("linkedin-raw-*.csv") if DAILY_CSV.match(p.name)),
                   key=lambda p: p.name, reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for path in files:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if not str(row.get("description") or "").strip():
                    continue
                key = judge_store.job_key(row)
                if not key.strip("|") or key in seen:
                    continue
                seen.add(key)
                row = dict(row)
                row["job_key"], row["file_date"] = key, DAILY_CSV.match(path.name).group(1)
                out.append(row)
    return out


def stratify(rows: list[dict], count: int, seed: int, exclude: set[str] = frozenset()) -> list[dict]:
    """Pick `count` rows: 40/30/30 gate-pass, near-miss, other; buckets that run short are topped up from the rest."""
    buckets: dict[str, list[dict]] = {"pass": [], "near": [], "other": []}
    for row in rows:
        if row["job_key"] in exclude:
            continue
        checks = judge_store.gate(row)
        row["gate"] = checks
        name = "pass" if judge_store.passes_gate(checks) else "near" if judge_store.near_miss(checks) else "other"
        buckets[name].append(row)
    rng = random.Random(seed)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    picked: list[dict] = []
    wanted = {name: round(count * share) for name, share in MIX.items()}
    for name, number in wanted.items():
        picked.extend(buckets[name][:number])
        del buckets[name][:number]
    leftovers = buckets["pass"] + buckets["near"] + buckets["other"]
    rng.shuffle(leftovers)
    picked.extend(leftovers[: max(0, count - len(picked))])
    return picked[:count]


def pay_text(row: dict) -> str:
    low, high = str(row.get("min_amount") or "").strip(), str(row.get("max_amount") or "").strip()
    unit = str(row.get("pay_interval") or "").strip()
    if not low and not high:
        return "pay not listed"
    fmt = lambda v: f"${float(v):,.0f}" if v else ""  # noqa: E731
    span = " to ".join(x for x in (fmt(low), fmt(high)) if x)
    return f"{span} {unit}".strip()


def shown(value: object) -> str:
    """Posting text as displayed: employers' tildes and dashes are rewritten, since the page is written for the owner."""
    text = "" if value is None else str(value)
    return html.escape(text.replace("—", ", ").replace("–", " to ").replace("~", "about "))


def build_html(rows: list[dict], day: str) -> str:
    cards = []
    for index, row in enumerate(rows, 1):
        row = {k: (shown(v) if k in ("title", "company", "location", "description", "site", "date_posted") else v)
               for k, v in row.items()}
        flags = row.get("gate") or judge_store.gate(row)
        checks = ", ".join(f"{name} {'ok' if ok else 'NO'}" for name, ok in flags.items() if name != "text")
        cards.append(f"""
<section class="card" data-key="{html.escape(row['job_key'])}" data-title="{row.get('title') or ''}"
  data-company="{row.get('company') or ''}" data-url="{html.escape(row.get('job_url') or '')}">
  <h2>{index}. {row.get('title') or ''}</h2>
  <p class="meta">{row.get('company') or ''} | {row.get('location') or ''} |
    {html.escape(pay_text(row))} | {row.get('site') or ''} | posted {row.get('date_posted') or '?'}
    | <a href="{html.escape(row.get('job_url') or '#')}" target="_blank" rel="noopener">open posting</a></p>
  <p class="checks">code gate: {html.escape(checks)}</p>
  <details><summary>description</summary><p class="desc">{row.get('description') or ''}</p></details>
  <div class="buttons">
    <button type="button" data-label="Apply">Apply</button>
    <button type="button" data-label="Maybe">Maybe</button>
    <button type="button" data-label="Skip">Skip</button>
    <input type="text" placeholder="one-word reason (optional)" maxlength="80">
  </div>
</section>""")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gold set labelling {html.escape(day)}</title>
<style>
body{{font-family:'Times New Roman',serif;color:#000;background:#fff;max-width:900px;margin:24px auto;padding:0 16px;line-height:1.4}}
h1,h2{{text-align:left;margin:0 0 6px}} h2{{font-size:18px}}
.card{{border:1px solid #999;padding:12px;margin:14px 0}} .card.done{{background:#f3f3f3}}
.meta,.checks{{font-size:14px;margin:4px 0}} .desc{{font-size:14px;white-space:pre-wrap}}
.buttons{{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
button{{font:inherit;padding:6px 14px;border:1px solid #000;background:#fff;cursor:pointer}}
button.on{{background:#000;color:#fff}} input{{font:inherit;padding:5px;border:1px solid #999;flex:1;min-width:160px}}
#bar{{position:sticky;top:0;background:#fff;border-bottom:1px solid #000;padding:8px 0;margin-bottom:12px}}
#bar button{{margin-right:8px}}
</style></head><body>
<div id="bar"><strong>Gold set {html.escape(day)}</strong>: <span id="progress">0</span> of {len(rows)} labelled.
  <button type="button" id="export">Export labels (JSON)</button><button type="button" id="copy">Copy JSON</button>
  <span id="note"></span></div>
<h1>Label each posting: would you apply?</h1>
<p>Apply = you would send an application this week. Maybe = worth a closer look or a question first.
Skip = no. Your choices save in this browser as you go; when done press Export and save the file into
the Job Search folder, then tell Claude to import it.</p>
{''.join(cards)}
<script>
(function(){{
  const KEY='gold-form-{html.escape(day)}';
  let state={{}};
  try{{state=JSON.parse(localStorage.getItem(KEY)||'{{}}');}}catch(e){{state={{}};}}
  const cards=[...document.querySelectorAll('.card')];
  function save(){{try{{localStorage.setItem(KEY,JSON.stringify(state));}}catch(e){{}}
    const n=Object.values(state).filter(v=>v.label).length;
    document.getElementById('progress').textContent=n;}}
  function paint(card){{const k=card.dataset.key,v=state[k]||{{}};
    card.classList.toggle('done',!!v.label);
    card.querySelectorAll('button[data-label]').forEach(b=>b.classList.toggle('on',b.dataset.label===v.label));
    const inp=card.querySelector('input'); if(document.activeElement!==inp) inp.value=v.reason||'';}}
  cards.forEach(card=>{{
    card.querySelectorAll('button[data-label]').forEach(b=>b.addEventListener('click',()=>{{
      const k=card.dataset.key; state[k]=Object.assign({{}},state[k]||{{}},{{label:b.dataset.label,
        title:card.dataset.title,company:card.dataset.company,url:card.dataset.url}}); save(); paint(card);}}));
    card.querySelector('input').addEventListener('input',e=>{{const k=card.dataset.key;
      state[k]=Object.assign({{}},state[k]||{{}},{{reason:e.target.value,title:card.dataset.title,
        company:card.dataset.company,url:card.dataset.url}}); save();}});
    paint(card);
  }});
  function payload(){{return JSON.stringify({{form:KEY,exported_at:new Date().toISOString(),
    labels:Object.entries(state).filter(([k,v])=>v.label).map(([k,v])=>Object.assign({{job_key:k}},v))}},null,1);}}
  document.getElementById('export').addEventListener('click',()=>{{
    const blob=new Blob([payload()],{{type:'application/json'}});const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download='gold-labels-{html.escape(day)}.json';a.click();
    document.getElementById('note').textContent=' saved to Downloads';}});
  document.getElementById('copy').addEventListener('click',()=>{{navigator.clipboard.writeText(payload())
    .then(()=>{{document.getElementById('note').textContent=' copied';}});}});
  save();
}})();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--out", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else project_root()
    day = dt.date.today().isoformat()
    with judge_store.open_db(root) as conn:
        labelled = judge_store.gold_keys(conn)
    rows = candidate_rows(root)
    picked = stratify(rows, args.count, args.seed, exclude=labelled)
    out = Path(args.out) if args.out else root / "reports" / f"gold-form-{day}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(picked, day), encoding="utf-8")
    mix = {name: sum(1 for r in picked if (judge_store.passes_gate(r["gate"]) if name == "pass" else
                                           judge_store.near_miss(r["gate"]) if name == "near" else
                                           not judge_store.passes_gate(r["gate"]) and not judge_store.near_miss(r["gate"])))
           for name in MIX}
    print(f"gold form: {len(picked)} postings from {len(rows)} with text ({len(labelled)} already labelled) -> {out}")
    print(f"  mix: gate-pass {mix['pass']}, near-miss {mix['near']}, other {mix['other']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
