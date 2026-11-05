---
name: show-my-jobs
description: Show or refresh the candidate's job dashboard. Use when the owner says "show my jobs", "refresh", "what's new", "any jobs today", or asks what to apply to. Reads the generated dashboard, curates the needs-validation queue into Apply or Skip with evidence, re-runs the generator, and renders the Apply list inline in chat. Never applies, never hand-edits generated files.
---

# show-my-jobs

The dashboard is generated. You edit `dashboard\job-dashboard-data.json`, run `scripts\job_dashboard.py`, and show the
result. Read `dashboard\README.md` once per session for the column rules if anything below is unclear.

All commands run from the repo root with `scripts\automation\venv\Scripts\python.exe`.

## Steps

0. **Sync first (since 2024-12-23 the chain runs on GitHub Actions).** The runner commits `job-dashboard.md`,
   `dashboard\job-dashboard-data.json` and `automation-logs\pipeline-status.json` on main and keeps the working state
   (verdicts.db, queue memory, CSVs) on the orphan `state` branch. Before anything else, from the repo root:
   `git checkout --quiet -- job-dashboard.md automation-logs/pipeline-status.json` (the runner owns these two; the
   laptop's chain and this skill's own render rewrite them, and a pull refuses to overwrite local changes), then
   `git pull --rebase --quiet origin main` (if it stops on `dashboard/job-dashboard-data.json`, the owner's curated edits are
   uncommitted: commit them first, then pull again), then
   `git fetch --quiet origin state` and `git worktree add --force ..\job-search-state origin/state` (or
   `git -C ..\job-search-state pull --quiet` when that worktree already exists), then
   `scripts\automation\venv\Scripts\python.exe scripts\automation\state_sync.py restore ..\job-search-state`.
   If the `state` branch does not exist yet (before the first runner night), skip the fetch and the restore and say so
   in one line. Then rebuild the page locally (step 5) so the HTML carries this laptop's decision token.
1. **Check the night ran.** Read `automation-logs\pipeline-status.json`. If `finished` is not today or a step is
   `failed`, say so first in one line (which step, the sanitized provider reason). Do not re-run the pull.
2. **Read the current view.** Read `job-dashboard.md`. Sections: Apply (with verified_on state), Recheck before
   applying, Already applied, Skip, Needs validation queue.
3. **"Show" (default): no new web calls.** Render the Apply section inline as a short Apply / Skip card list: company,
   title, location, pay, why it fits, apply link, verified date. Then list the queue rows marked "new today" with
   their judge label from `dashboard\verdicts.db` when one exists (read-only query on `verdict` by role key). Stop here
   unless the owner asks to refresh or to decide on specific roles.
4. **"Refresh" (only when asked): validate the queue.** For each queue row the owner wants decided (or all "new today"):
   a. Open the posting through the web fallback chain in CLAUDE.md, preferring the employer's own ATS over LinkedIn.
   b. Apply the search rules from CLAUDE.md (North Carolina HQ, Raleigh-Durham or remote, >= $70k when published, level II or
      no level word, under 3 weeks old, staffing flag when hourly).
   c. Edit `dashboard\job-dashboard-data.json`: move the role into `apply` (with `apply_url` = the employer page and
      `verified_on` = today) or `skip` (with one plain-English reason). Read the file first; keep existing entries.
5. **Rebuild.** Run:
   `scripts\automation\venv\Scripts\python.exe scripts\job_dashboard.py --no-link-check --no-queue-check`
   (reuses tonight's link and listing verdicts, so the refresh makes no web calls; drop the flags only if the owner asks for a
   full re-check, it is slow).
   A non-zero exit means stop and show the error; do not hand-edit the HTML.
6. **Pipeline.** Add only Apply-tier roles the owner says they will pursue to `pipeline.md` as `found` (read before write).
7. **Render.** Show the rebuilt Apply list inline. Say in one line what moved (n to Apply, n to Skip, n still queued).

## Rules

- Never apply, never tailor a resume here (that is `build-tailored-resume`), never send anything.
- Never fabricate a reason; if a posting could not be read, say "could not check" and leave it queued.
- No em-dashes, en-dashes or tildes in anything shown to the owner.
