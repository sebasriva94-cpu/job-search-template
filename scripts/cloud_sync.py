"""Keep the cloud daily brief's private GitHub repo up to date from the laptop (added 2024-12-09).

The owner's laptop is usually closed, so the daily brief now runs as a Claude routine in Anthropic's cloud
and reads a small private repo (Documents/GitHub/private-brief-repo, pushed to github.com/your-github-user/your-private-brief-repo).
Whenever the laptop is awake this script:
  1. pulls the repo, so briefs the cloud wrote (briefs/daily-brief-*.md) reach weekly-reviews/;
  2. copies the few files the brief reads into inputs/;
  3. commits and pushes if anything changed.
Only these leave the laptop: pipeline.md, job-dashboard.md, automation-logs/pipeline-status.json,
dashboard/decisions.json, the NAMES of the interview prep packs and, since 2024-12-10, the Raleigh-Durham
market numbers from scripts/market_snapshot.py (counts and the employers reposting most) for the Monday
edition of the brief, which replaced the laptop's weekly review. Nothing else.

    python scripts/cloud_sync.py [--repo PATH] [--no-push]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import shutil
import subprocess
import sys
import time

try:
    from scripts.sweep_common import project_root
except ModuleNotFoundError:  # Direct execution: scripts/ is sys.path[0].
    from sweep_common import project_root

DEFAULT_REPO = pathlib.Path(os.environ.get("BRIEF_REPO", os.path.expanduser(r"~\Documents\GitHub\private-brief-repo")))
BRANCH = "main"
# inputs/<name> in the repo <- <path> in the Job Search folder.
INPUTS = {
    "pipeline.md": "pipeline.md",
    "job-dashboard.md": "job-dashboard.md",
    "pipeline-status.json": "automation-logs/pipeline-status.json",
    "decisions.json": "dashboard/decisions.json",
}
PACKS_DIR = "interview-prep/packs"
SNAPSHOT_NAME = "market-snapshot.md"
LOCK_STALE_SECONDS = 15 * 60


def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def has_remote_branch(repo: pathlib.Path) -> bool:
    return bool(git(repo, "ls-remote", "--heads", "origin", BRANCH).stdout.strip())


def market_snapshot_text(root: pathlib.Path, today: dt.date) -> str:
    """Last week's Raleigh-Durham numbers for the Monday brief. A failure is reported, never raised."""
    try:
        try:
            from scripts import market_snapshot
        except ModuleNotFoundError:
            import market_snapshot
        return market_snapshot.render(market_snapshot.snapshot(root, today))
    except Exception as error:  # noqa: BLE001 - the sync must still push the other inputs
        return f"Market snapshot unavailable on {today.isoformat()} ({type(error).__name__}: {str(error)[:120]})"


def copy_inputs(root: pathlib.Path, repo: pathlib.Path, today: dt.date | None = None) -> None:
    inputs = repo / "inputs"
    inputs.mkdir(exist_ok=True)
    for name, rel in INPUTS.items():
        source, target = root / rel, inputs / name
        if source.is_file():
            shutil.copyfile(source, target)
        elif target.exists():  # Gone on the laptop (e.g. decisions reset): drop the stale copy too.
            target.unlink()
    packs = root / PACKS_DIR
    names = sorted(p.name for p in packs.iterdir() if p.is_file()) if packs.is_dir() else []
    (inputs / "prep-packs.txt").write_text("".join(f"{n}\n" for n in names), encoding="utf-8", newline="\n")
    text = market_snapshot_text(root, today or dt.date.today())
    (inputs / SNAPSHOT_NAME).write_text(text + "\n", encoding="utf-8", newline="\n")


def copy_briefs_back(root: pathlib.Path, repo: pathlib.Path) -> list[str]:
    """Cloud briefs land in weekly-reviews/; a same-day laptop brief is kept and the cloud one gets -cloud."""
    copied = []
    reviews = root / "weekly-reviews"
    for brief in sorted((repo / "briefs").glob("daily-brief-*.md")):
        target = reviews / brief.name
        if target.exists() and target.read_bytes() != brief.read_bytes():
            target = reviews / f"{brief.stem}-cloud.md"
        if target.exists() and target.read_bytes() == brief.read_bytes():
            continue
        reviews.mkdir(exist_ok=True)
        shutil.copyfile(brief, target)
        copied.append(target.name)
    return copied


def sync(root: pathlib.Path, repo: pathlib.Path, push: bool = True, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now().astimezone()
    result = {"pulled": False, "briefs_copied": [], "committed": False, "pushed": False}
    online = push and has_remote_branch(repo)
    if online:
        git(repo, "pull", "--rebase", "--autostash", "--quiet", "origin", BRANCH)
        result["pulled"] = True
    result["briefs_copied"] = copy_briefs_back(root, repo)
    copy_inputs(root, repo, now.date())
    git(repo, "add", "-A", "inputs")
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(repo, "commit", "--quiet", "-m", f"Laptop inputs {now:%Y-%m-%d %H:%M %Z}")
        result["committed"] = True
    # A commit left behind by an earlier failed push still goes up.
    ahead = online and git(repo, "rev-list", "--count", f"origin/{BRANCH}..HEAD").stdout.strip() != "0"
    if push and (result["committed"] or ahead or not online):
        git(repo, "push", "--quiet", "origin", f"HEAD:{BRANCH}")
        result["pushed"] = True
    return result


def acquire_lock(repo: pathlib.Path) -> pathlib.Path | None:
    lock = repo / ".git" / "cloud-sync.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink()
        with lock.open("x") as handle:
            handle.write(str(os.getpid()))
        return lock
    except FileExistsError:
        return None


def log(root: pathlib.Path, line: str) -> None:
    now = dt.datetime.now().astimezone()
    path = root / "automation-logs" / f"cloud-sync-{now:%Y-%m}.log"
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now.isoformat(timespec='seconds')}] {line}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=pathlib.Path, default=DEFAULT_REPO)
    parser.add_argument("--no-push", action="store_true", help="copy and commit locally only")
    args = parser.parse_args(argv)
    root = project_root()
    lock = acquire_lock(args.repo)
    if lock is None:
        log(root, "skipped: another sync is running")
        return 0
    try:
        result = sync(root, args.repo, push=not args.no_push)
    except subprocess.CalledProcessError as error:
        log(root, f"FAILED: git {' '.join(error.cmd[3:])}: {(error.stderr or error.stdout or '').strip()[:300]}")
        return 1
    finally:
        lock.unlink(missing_ok=True)
    log(root, f"ok: {result}")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
