# Job Search Automation: a template

A personal job-search system I built and ran every night for my own search, published here as a blank
template with all personal data removed. It finds postings across several sources, has an LLM score each one
against a written rubric, and produces a one-page dashboard of what to apply to. It also builds tailored
resumes from a library of verified bullets.

All candidate data in this repo is **fictional** ("Alex Example", "Employer A", "Anytown, ST"). The search ships
with an example configuration (the Raleigh-Durham area plus Greater Minneapolis, a $70k pay floor, an empty
employer watchlist) to replace with your own: see `CLAUDE.md` for where each setting lives.

## What it does

| Stage | What runs | Where |
|---|---|---|
| **Sweep** | Pulls postings from Google for Jobs (JSearch), LinkedIn, USAJOBS and target-company boards (Workday, Greenhouse, single careers pages via Playwright); dedupes and normalizes them | `scripts/*_sweep.py`, `scripts/linkedin_*.py`, `scripts/sweep_common.py` |
| **Validate** | Checks that each apply link is still live and that the posting matches the location and level rules | `scripts/validate_listing.py`, `scripts/check_apply_liveness.py` |
| **Judge** | Claude labels each posting on five dimensions (experience, skills, archetype, logistics, pay) with evidence quoted from the posting. The program, not the model, computes Apply / Maybe / Skip from those labels | `scripts/judge.py`, `scripts/judge_prompt.md`, `scripts/judge_schema.json` |
| **Calibrate** | A hand-labelled "gold set" measures how often the judge agrees with a human, so rubric changes are tested and not guessed | `scripts/gold_form.py`, `scripts/gold_import.py`, `scripts/judge_calibrate.py` |
| **Dashboard** | One HTML/Markdown page: new roles, apply queue, pipeline health banner and provider status | `scripts/job_dashboard.py`, `dashboard/README.md` |
| **Resumes** | Builds a tailored DOCX and PDF for one posting from a verified bullet library, with checks for page count, contact line and banned characters | `scripts/resume_tailoring/` |
| **Schedule** | Runs the whole chain nightly on a GitHub Actions Windows runner, with rate control and per-provider budgets | `.github/workflows/night-chain.yml`, `scripts/rate_control.py` |

Engineering notes:
- **384 unit tests** (`python -m unittest discover -s tests`), including fakes for every external provider.
- **Prompt-injection guard.** Posting text is treated as untrusted. Any instructions aimed at AI readers are recorded in an `injection_notice` field and never followed.
- **Rate control.** Every paid API has cooldowns, budgets and lock files, so a retry storm can't drain credits.
- **Claude Code skills** in `.claude/skills/` drive the daily workflow ("show my jobs", "gold round", "build a tailored resume").

## Set up your own copy

1. Use this template to make a **private** repository. Your filled-in profile, pipeline and resumes are personal data.
2. `python -m venv scripts/automation/venv`, then `pip install -r scripts/automation/requirements.txt`.
3. Fill in `candidate-profile.md`, `target-companies.md` and `scripts/resume_tailoring/bullets.py` with your own verified facts.
4. Edit the location, level and pay rules in `scripts/judge_prompt.md` to match your search.
5. Set API keys as environment variables (never in files): `ANTHROPIC_API_KEY`, `USAJOBS_API_KEY`, `USAJOBS_EMAIL`, `JSEARCH_API_TOKEN`, `APIFY_TOKEN`, `BRIGHTDATA_API_TOKEN`.
6. Put your base resume at `resumes/base_resume.docx` (gitignored) to enable the resume builder.
7. To run it nightly in the cloud, add the same keys as repository secrets and uncomment the `schedule:` block in `.github/workflows/night-chain.yml`.

Windows-first (PowerShell scheduled tasks and a Windows Actions runner). The Python parts are cross-platform.

## License

MIT
