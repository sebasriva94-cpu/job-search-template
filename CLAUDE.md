# Job search automation: Claude Code context (template)

Claude's map of this repo. `README.md` is the overview and `dashboard\README.md` is the operations manual.
Fill in the bracketed parts for your own search; the values shown are an example configuration.

## Search rules (example: edit to your own)

- Area: the home region (example: Durham, Wake or Orange County), a second metro (example: Greater
  Minneapolis, any office pattern, see `sweep_common.is_local`), or fully remote in the US.
- Pay: at least the floor in `scripts\validate_listing.py` (`FLOOR`) when published.
- Level: Level II / Tier 2, or no level word. Posted under 3 weeks. Staffing agencies shown, flagged when hourly.
- The same rules, in prose, are the `culture_logistics` and `compensation` sections of `scripts\judge_prompt.md`.

## Run and check

From the repo root, with the venv interpreter:

```powershell
scripts\automation\venv\Scripts\python.exe -m unittest discover -s tests        # must pass before any commit
scripts\automation\venv\Scripts\python.exe scripts\job_dashboard.py --no-link-check --no-queue-check   # rebuild dashboard, no network
powershell -File scripts\automation\pull-jobs.ps1 -Phase catchup      # run tonight's chain now if it has not run
```

## Read-before-act rules

- Resume -> read `candidate-profile.md` and `prompts\resume-tailoring-prompt.md`; use the `build-tailored-resume` skill.
- Show or refresh jobs -> the `show-my-jobs` skill. Gold labels, judge rubric, calibration -> the `gold-round` skill.
- Any edit to `scripts\judge_prompt.md` or `candidate-profile.md` gets an independent review and a `rubric_version` bump.
- `dashboard\verdicts.db` is Python-owned; query it read-only unless a skill says otherwise.

## Web research fallback chain

When a fetch is blocked or returns junk, step down and report which tool got through: WebFetch -> a search/fetch
MCP (for example Exa) -> Bright Data Web Unlocker (zone in `BRIGHTDATA_UNLOCKER_ZONE`) -> a real browser -> ask.

## Standing rules

- Never fabricate qualifications. Never auto-submit an application or send anything without explicit approval.
- API keys live in environment variables, never in files.
- Stay inside the budgets in `scripts\rate_control.py`; no proxy rotation, no logged-in scraping, no evasion.
