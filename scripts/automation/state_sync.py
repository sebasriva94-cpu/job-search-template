"""Carry the night chain's working state between GitHub Actions runs (2024-12-23).

The chain keeps its memory in files that are not source and are not tracked on main: the judge's verdicts.db,
the queue memory, the provider gate state, the employer lookup cache, the apply-liveness verdicts, the status
file, the once-a-day JSearch marker, the daily pull CSVs and the month's logs. A runner has no disk between
nights, so the workflow restores these from the repo's orphan `state` branch before the chain and saves them
back after it. On the laptop nothing calls this; the files simply stay where they are.

    python scripts/automation/state_sync.py restore STATE_DIR [--root ROOT]
    python scripts/automation/state_sync.py save STATE_DIR [--root ROOT] [--today YYYY-MM-DD] [--keep-days 21]

`save` replaces the state folder's contents (its .git aside) with the current selection, so files that have
aged out of the window disappear from the branch too. Dated CSVs older than --keep-days are dropped, which is
the dashboard's own three-week horizon; the monthly logs are kept for the current and the previous month.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import shutil
import sys

STATE_FILES = (
    "dashboard/verdicts.db",
    "dashboard/queue-seen.json",
    "dashboard/provider-state.json",
    "dashboard/employer-hq.json",
    "dashboard/apply-liveness.json",
    "automation-logs/pipeline-status.json",
    "automation-logs/jsearch-last-run.txt",
)
DATED_CSV = re.compile(r"^linkedin-raw-(\d{4}-\d{2}-\d{2})\.csv$")
MONTHLY_LOG = re.compile(r"^(linkedin-pull|linkedin-feed|cloud-sync)-(\d{4})-(\d{2})\.(log|jsonl)$")
KEEP_DAYS = 21


def _months_kept(today: dt.date) -> set[tuple[int, int]]:
    first = today.replace(day=1)
    previous = (first - dt.timedelta(days=1))
    return {(today.year, today.month), (previous.year, previous.month)}


def select(root: pathlib.Path, today: dt.date, keep_days: int = KEEP_DAYS) -> list[str]:
    """Relative paths of the state worth carrying, in a stable order."""
    out = [rel for rel in STATE_FILES if (root / rel).is_file()]
    sweeps = root / "weekly-sweeps"
    if sweeps.is_dir():
        for path in sorted(sweeps.iterdir()):
            match = DATED_CSV.match(path.name)
            if not match:
                continue
            try:
                day = dt.date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if 0 <= (today - day).days < keep_days:
                out.append(f"weekly-sweeps/{path.name}")
    logs = root / "automation-logs"
    if logs.is_dir():
        months = _months_kept(today)
        for path in sorted(logs.iterdir()):
            match = MONTHLY_LOG.match(path.name)
            if match and (int(match.group(2)), int(match.group(3))) in months:
                out.append(f"automation-logs/{path.name}")
    return out


def _copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def restore(state_dir: pathlib.Path, root: pathlib.Path) -> list[str]:
    """Copy every state file present in the state folder into the checkout. Missing files are simply absent:
    the first run starts empty, as the laptop did on its first night."""
    copied: list[str] = []
    for current, _dirs, files in os.walk(state_dir):
        folder = pathlib.Path(current)
        if ".git" in folder.relative_to(state_dir).parts:
            continue
        for name in files:
            src = folder / name
            rel = src.relative_to(state_dir).as_posix()
            if rel == "README.md":
                continue
            _copy(src, root / rel)
            copied.append(rel)
    return sorted(copied)


def save(root: pathlib.Path, state_dir: pathlib.Path, today: dt.date, keep_days: int = KEEP_DAYS) -> list[str]:
    """Replace the state folder's contents with the current selection; .git is left alone."""
    state_dir.mkdir(parents=True, exist_ok=True)
    for child in list(state_dir.iterdir()):
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    selected = select(root, today, keep_days)
    for rel in selected:
        _copy(root / rel, state_dir / rel)
    (state_dir / "README.md").write_text(
        "Working state of the Job Search night chain, written by .github/workflows/night-chain.yml after each run.\n"
        "Not source. Restored before the next run by scripts/automation/state_sync.py. Safe to replace with a fresh\n"
        f"orphan branch when it grows. Last save: {today.isoformat()}, {len(selected)} files.\n",
        encoding="utf-8")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("restore", "save"))
    parser.add_argument("state_dir")
    parser.add_argument("--root", default=os.environ.get("JOBSEARCH_ROOT", "."))
    parser.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to the local date")
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    state_dir = pathlib.Path(args.state_dir).resolve()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    if args.action == "restore":
        files = restore(state_dir, root)
    else:
        files = save(root, state_dir, today, args.keep_days)
    print(f"{args.action}: {len(files)} files")
    for rel in files:
        print("  " + rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
