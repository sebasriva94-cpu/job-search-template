"""Calibration report: the judge's labels against the candidate's gold labels, as an HTML page.

Decision 10 of the 2024-12-13 plan: the dashboard switches from the keyword band to the judge label
only when quadratic-weighted kappa on at least 150 gold items is 0.6 or better. Reported: weighted
and plain Cohen's kappa, the 3x3 confusion matrix with class rates, per-class precision and recall,
Apply recall (the number that matters: a missed Apply is a missed job), the Unscored count, and the
disagreements between the owner and the judge listed with the judge's summary line so the next labelling
round and any rubric change start from the misses.

Gold rows count only when the judge has a verdict for the same role key (gate verdicts count as Skip).
Seeds from past decisions join only where the posting was seen again with text; the form labels are
the main calibration set.

Usage: python scripts\\judge_calibrate.py [--min-n 150] [--threshold 0.6] [--out reports\\judge-calibration-<date>.html]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
from pathlib import Path

try:
    from scripts import judge_store
    from scripts.sweep_common import project_root
except ModuleNotFoundError:
    import judge_store
    from sweep_common import project_root

ORDER = ("Skip", "Maybe", "Apply")
INDEX = {label: i for i, label in enumerate(ORDER)}


def pairs(conn) -> tuple[list[tuple[str, str, dict]], int]:
    """[(gold, judge, verdict_row)] for gold rows with a usable verdict, plus the Unscored count."""
    gold = {r["job_key"]: r for r in judge_store.gold_rows(conn)}
    latest: dict[str, dict] = {}

    def rank(v) -> tuple:
        # a model verdict outranks a gate Skip or an Unscored row for the same key, whatever was written last
        # (2024-12-20: a gate row rewritten under a new version was silently replacing an older model verdict)
        return (v["model_id"] != "gate", v["label"] != "Unscored", v["created_at"] or "")

    for row in conn.execute("SELECT * FROM verdict ORDER BY created_at"):
        if row["job_key"] in gold:
            current = latest.get(row["job_key"])
            if current is None or rank(row) >= rank(current):
                latest[row["job_key"]] = dict(row)
    out, unscored = [], 0
    for key, verdict in latest.items():
        if verdict["label"] == "Unscored":
            unscored += 1
            continue
        if verdict["label"] in INDEX:
            out.append((gold[key]["label"], verdict["label"], verdict))
    return out, unscored


def confusion(labelled: list[tuple[str, str]]) -> list[list[int]]:
    matrix = [[0] * 3 for _ in ORDER]
    for gold, judge in labelled:
        matrix[INDEX[gold]][INDEX[judge]] += 1
    return matrix


def kappa(matrix: list[list[int]], weighted: bool) -> float | None:
    n = sum(map(sum, matrix))
    if n == 0:
        return None
    k = len(ORDER)
    rows = [sum(r) / n for r in matrix]
    cols = [sum(matrix[i][j] for i in range(k)) / n for j in range(k)]
    weight = lambda i, j: ((i - j) ** 2) / ((k - 1) ** 2) if weighted else (0.0 if i == j else 1.0)  # noqa: E731
    observed = sum(weight(i, j) * matrix[i][j] / n for i in range(k) for j in range(k))
    expected = sum(weight(i, j) * rows[i] * cols[j] for i in range(k) for j in range(k))
    if expected == 0:
        return None
    return round(1 - observed / expected, 3)


def per_class(matrix: list[list[int]]) -> dict[str, dict]:
    out = {}
    for label, i in INDEX.items():
        tp = matrix[i][i]
        gold_n = sum(matrix[i])
        judged_n = sum(matrix[r][i] for r in range(3))
        out[label] = {"gold": gold_n, "judged": judged_n,
                      "recall": round(tp / gold_n, 3) if gold_n else None,
                      "precision": round(tp / judged_n, 3) if judged_n else None}
    return out


MIN_APPLY_RECALL = 0.8


def report(conn, min_n: int, threshold: float, min_apply_recall: float = MIN_APPLY_RECALL) -> dict:
    """Two tables (2024-12-19): the code gate's Skips against the owner's labels, and the model's labels against the owner's.

    A first calibration round was mostly gate Skips the owner agreed with,
    so the number mostly graded the keyword gate. READY is now judged on the model pairs alone: at least
    `min_n` of them, weighted kappa at or above `threshold`, and Apply recall at or above `min_apply_recall`
    (a missed Apply is the costly error, and kappa alone cannot see it at this sample size)."""
    labelled, unscored = pairs(conn)
    model_pairs = [p for p in labelled if p[2].get("model_id") != "gate"]
    gate_pairs = [p for p in labelled if p[2].get("model_id") == "gate"]
    matrix = confusion([(g, j) for g, j, _ in labelled])
    model_matrix = confusion([(g, j) for g, j, _ in model_pairs])
    classes = per_class(matrix)
    model_classes = per_class(model_matrix)
    model_kappa = kappa(model_matrix, True)
    model_recall = model_classes["Apply"]["recall"]
    misses = [{"gold": g, "judge": j, "title": v.get("title", ""), "company": v.get("company", ""),
               "summary": v.get("summary_line", ""), "confidence": v.get("confidence", ""),
               "stage": "gate" if v.get("model_id") == "gate" else "model"}
              for g, j, v in labelled if g != j]
    ready = (model_kappa is not None and len(model_pairs) >= min_n and model_kappa >= threshold
             and model_recall is not None and model_recall >= min_apply_recall)
    return {"n": len(labelled), "unscored": unscored, "weighted_kappa": kappa(matrix, True), "kappa": kappa(matrix, False),
            "matrix": matrix, "classes": classes, "apply_recall": classes["Apply"]["recall"],
            "model": {"n": len(model_pairs), "weighted_kappa": model_kappa, "kappa": kappa(model_matrix, False),
                      "matrix": model_matrix, "classes": model_classes, "apply_recall": model_recall},
            "gate": {"n": len(gate_pairs), "agree": sum(1 for g, j, _ in gate_pairs if g == j),
                     "misses": [(g, j) for g, j, _ in gate_pairs if g != j]},
            "misses": misses, "ready": ready, "min_n": min_n, "threshold": threshold, "min_apply_recall": min_apply_recall}


def _matrix_table(matrix: list[list[int]]) -> str:
    e = html.escape
    cells = "".join(
        f"<tr><th>{e(gold)}</th>" + "".join(f"<td>{matrix[i][j]}</td>" for j in range(3))
        + f"<td>{sum(matrix[i])}</td></tr>" for i, gold in enumerate(ORDER))
    return f"<table><tr><th></th>{''.join(f'<th>{e(l)}</th>' for l in ORDER)}<th>total</th></tr>{cells}</table>"


def _class_table(classes: dict) -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(label)}</td><td>{c['gold']}</td><td>{c['judged']}</td><td>{c['recall']}</td><td>{c['precision']}</td></tr>"
        for label, c in classes.items())
    return f"<table><tr><th>label</th><th>the owner's count</th><th>judge count</th><th>recall</th><th>precision</th></tr>{rows}</table>"


def render(result: dict, day: str) -> str:
    e = html.escape
    model, gate = result["model"], result["gate"]
    misses = "".join(
        f"<tr><td>{e(m['gold'])}</td><td>{e(m['judge'])}</td><td>{e(m.get('stage', ''))}</td><td>{e(m['confidence'])}</td>"
        f"<td>{e(m['title'])} at {e(m['company'])}</td><td>{e(m['summary'])}</td></tr>"
        for m in result["misses"])
    verdict = ("Ready: the dashboard can switch to the judge label." if result["ready"] else
               f"Not yet: needs at least {result['min_n']} model pairs, weighted kappa {result['threshold']} or better on them, "
               f"and Apply recall {result['min_apply_recall']} or better on them.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Judge calibration {e(day)}</title>
<style>body{{font-family:'Times New Roman',serif;color:#000;background:#fff;max-width:900px;margin:24px auto;padding:0 16px;line-height:1.4}}
h1,h2{{text-align:left}} table{{border-collapse:collapse;margin:10px 0}} th,td{{border:1px solid #999;padding:4px 8px;text-align:left;vertical-align:top;font-size:14px}}</style></head><body>
<h1>Judge calibration, {e(day)}</h1>
<p><strong>{e(verdict)}</strong></p>
<h2>Model pairs: what READY is judged on</h2>
<p>Gold rows the model itself labelled. The code gate's Skips are scored separately below, so agreement on obvious
Skips cannot flatter the model.</p>
<table><tr><th>Model pairs</th><td>{model['n']}</td></tr>
<tr><th>Weighted kappa (quadratic)</th><td>{model['weighted_kappa']}</td></tr>
<tr><th>Cohen's kappa</th><td>{model['kappa']}</td></tr>
<tr><th>Apply recall</th><td>{model['apply_recall']}</td></tr></table>
{_matrix_table(model['matrix'])}
{_class_table(model['classes'])}
<h2>Gate pairs</h2>
<p>{gate['n']} gold rows the code gate skipped before any model call; the owner agreed on {gate['agree']}.
{('Gate misses (owner label, gate): ' + e(', '.join(f'{g} vs {j}' for g, j in gate['misses']))) if gate['misses'] else 'No gate misses.'}</p>
<h2>All pairs together (the number reported before 2024-12-19)</h2>
<table><tr><th>Gold items with a verdict</th><td>{result['n']}</td></tr>
<tr><th>Unscored (no verdict usable)</th><td>{result['unscored']}</td></tr>
<tr><th>Weighted kappa (quadratic)</th><td>{result['weighted_kappa']}</td></tr>
<tr><th>Cohen's kappa</th><td>{result['kappa']}</td></tr>
<tr><th>Apply recall</th><td>{result['apply_recall']}</td></tr></table>
{_matrix_table(result['matrix'])}
{_class_table(result['classes'])}
<h2>Where they disagree ({len(result['misses'])})</h2>
<table><tr><th>the owner's</th><th>judge</th><th>stage</th><th>confidence</th><th>posting</th><th>judge's line</th></tr>{misses or '<tr><td colspan="6">none</td></tr>'}</table>
<p>Human raters agree with each other at about kappa 0.8 on this kind of task; 0.6 is the bar to deploy with monitoring.
At 100 to 150 pairs the kappa confidence interval is about plus or minus 0.25, so read Apply recall and the matrix with it.
Kappa is deflated when one class dominates.</p>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-n", type=int, default=100, help="model pairs needed (2024-12-19: was 150 pairs of any kind)")
    parser.add_argument("--threshold", type=float, default=0.6, help="weighted kappa on the model pairs")
    parser.add_argument("--min-apply-recall", type=float, default=MIN_APPLY_RECALL, help="Apply recall on the model pairs")
    parser.add_argument("--out", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else project_root()
    day = dt.date.today().isoformat()
    with judge_store.open_db(root) as conn:
        result = report(conn, args.min_n, args.threshold, args.min_apply_recall)
        model = result["model"]
        conn.execute("INSERT OR REPLACE INTO sentinel_run (run_date, kappa, apply_recall, n, note) VALUES (?,?,?,?,?)",
                     (day, model["weighted_kappa"], model["apply_recall"], model["n"],
                      f"calibration on model pairs; gate pairs {result['gate']['n']}, agree {result['gate']['agree']}; "
                      f"all pairs {result['n']} kappa {result['weighted_kappa']}"))
    out = Path(args.out) if args.out else root / "reports" / f"judge-calibration-{day}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result, day), encoding="utf-8")
    print(f"calibration: model pairs={model['n']} weighted kappa={model['weighted_kappa']} apply recall={model['apply_recall']}; "
          f"gate pairs={result['gate']['n']} agree={result['gate']['agree']}; all pairs={result['n']} kappa={result['weighted_kappa']} "
          f"unscored={result['unscored']} -> {'READY' if result['ready'] else 'NOT YET'}; {out}")
    print(json.dumps({"model_matrix": model["matrix"], "model_classes": model["classes"], "matrix": result["matrix"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
