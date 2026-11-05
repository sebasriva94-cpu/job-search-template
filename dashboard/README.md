# Job dashboard hub

This folder holds the curated data behind the one central job dashboard.

- `job-dashboard-data.json` is the editable source for validated Apply/Skip decisions. Each
  Apply role carries a structured `verified_on` date (YYYY-MM-DD); set it whenever you re-verify.
- `apply-liveness.json` (generated daily, git-ignored): per-URL verdicts from
  `scripts\check_apply_liveness.py`. The dashboard shows them as "auto-check <date>: live on <host>"
  instead of a plain HTTP status, because LinkedIn answers a removed posting with HTTP 200.
- `queue-seen.json` (generated daily, git-ignored): the needs-validation queue's memory.
- `..\job-dashboard.html` is the browser view.
- `..\job-dashboard.md` is the Claude/chat view.
- `scripts\job_dashboard.py` rebuilds both views.

Rules:
1. Raw LinkedIn/JobSpy matches are not trusted by themselves.
2. A role moves to **Apply** only after the employer careers page or ATS confirms it is live and
   the apply link works. Record the date in `verified_on`.
3. Bad fits move to **Skip** with one plain-English reason.
4. Do not create new dated PDFs as the hub; rewrite the one dashboard.

## Apply-column states (since 2024-10-29)
- **✓ ok**: `verified_on` within 14 days.
- **⏳ stale, re-verify**: `verified_on` (or the file-level `last_validated`) older than 14 days.
  Still listed under Apply; re-verify on the ATS, then update the date.
- **⚠ recheck**: `check_apply_liveness.py` found the posting closed or redirected
  (`needs_recheck: true`). Rendered in its own "Recheck before applying" section, never as a green
  Apply. Confirm on the employer site, then clear the flag or move the role to Skip. A role that
  comes back live is un-flagged automatically the next morning.
- **Expired (since 2024-12-17)**: `verified_on` older than 21 days, or a location that is neither Raleigh-Durham nor
  remote, also moves the role to "Recheck before applying" (`curated_status`); nothing stays green for ever.
- **✅ applied (the check mark, since 2024-11-05)**: set `applied_on` (YYYY-MM-DD) on the entry,
  plus an optional `outcome` such as "Rejected YYYY-MM-DD" or "Recruiter screen YYYY-MM-DD". The
  role leaves Apply, renders under "Already applied or in process", the liveness checker stops
  checking it, and the queue never re-surfaces it. A pipeline.md row that names the company and
  title and says applied, interviewing or rejected does the same, but the field is exact and does
  not depend on that row repeating the curated title. Keep the entry; do not move it to Skip.

## Needs-validation queue
Fresh candidates not yet validated, shown below Apply/Skip. Curated Apply/Skip always win.

- **Source.** The queue ranks the latest `weekly-sweeps\linkedin-raw-*.csv` newest posting first,
  undated rows last. Relevance comes from the one title vocabulary in `scripts\sweep_common.py`
  (`TITLE_INCLUDE` / `TITLE_EXCLUDE`, shared by every sweep and the pull). The score-ranked queue
  from the job-agent database was retired on 2024-12-17.
- **Listing check.** Before render, each candidate URL is checked with the same classifier used
  for curated roles (`--no-queue-check` skips it). A listing found closed is dropped, its slot is
  refilled from the next candidate, and it is remembered so it is never fetched again.
- **Memory** (`queue-seen.json`). Each row shows "new today" or "shown since <date>". A row shown
  for more than 7 days is dropped automatically; entries not seen for 30 days are forgotten.
- **Carry-over (since 2024-11-05).** The feed only holds postings from the last 72 hours, so a
  validated apply-now role used to vanish after one to three mornings. Now the memory keeps each candidate's link and location, and a validated role stays on
  the dashboard for its full 7-day window even after the feed drops it; it is re-checked each
  morning and leaves when it is curated, found closed, or expires.

## Pipeline health banner
`scripts\automation\pull-jobs.ps1` rewrites `..\automation-logs\pipeline-status.json` after every
step and once more with the run's `alert` record (level ok / degraded / failed, a one-line headline,
failed_steps, degraded_steps, degraded_providers). The dashboard header renders it: green when the
last run was clean, amber for an in-progress or degraded run, red when a step failed or when the
status file is from another date after 03:00 (a missed night). This record IS the
alert (an email alert could not fire with the laptop closed); the 09:30 cloud brief reads the same file from the
private brief repo. Closed curated roles make `apply-check` degraded, not failed (they already render
under "Recheck before applying"); the step fails only when it could not run. Since 2024-12-30 `jsearch`
follows the same idea: a quota, rate-limit, 5xx, network or bad-JSON failure on its three searches makes
the step degraded (provider `jsearch`, exit 0, so the night and the GitHub run stay green), a rejected
token (401 or 403) makes it failed with exit 1, and a code bug still crashes it.

Provider health (since 2024-12-11). Each step in the status file also carries a `state` (`ok`,
`degraded`, `failed`) and a `providers` summary (state, a short sanitized reason, `cooldown_until`,
`consecutive_degraded_runs`), read from the `PROVIDER_STATUS {...}` line the Python step prints.
A throttled, paused or skipped provider is amber with a plain explanation ("LinkedIn paused: rate
limited (HTTP 429), paused until ...") and the banner no longer says "steps clean". It turns red for
a failed or misconfigured provider (rejected token), a provider that tripped a new pause on two scheduled runs in a
row, or every discovery step unavailable. Old status files without these fields still read green.

### Rate control (`scripts\rate_control.py`)
Cooldowns, daily budgets and failure counts persist in `dashboard\provider-state.json` (git-ignored),
so a pause survives into the next process. A throttle, CAPTCHA or sign-in wall is never a clean empty
result, and there is no proxy rotation or evasion; Task Scheduler is not used to retry a limited
provider. Limits:

- **LinkedIn (JobSpy)**: one query group at a time, all 16 groups each night (a run cut short
  resumes where it stopped); 60-120 s between groups on top of JobSpy's own 3-7 s between result
  pages, up to 25 results (three pages) per group. Since 2024-12-13 the first 429 of a run pauses
  LinkedIn 10-15 minutes, retries that group once and carries on (the night is degraded, reason
  `http_429_paused`); a second 429 in the same run pauses it until the next 00:30 window (at least
  12 h). Why: LinkedIn's 429 is a burst throttle of about ten pages that clears in about a minute,
  and 16 groups of ten pages with a next-night cooldown lost three whole nights in a row (10/14 to
  10/16) although each run had pulled jobs first. CAPTCHA or sign-in wall 24 h; repeated timeouts or
  5xx 6 h. A genuine zero-result query stays `ok`; an unusually empty run is flagged as a coverage
  warning. A night that only honours an existing pause does not count as another degraded run.
- **Bright Data** (listing checks): 2 at once, one new request per 1.5 s, 60 per render and 150 per
  day. A 429 pauses it for `Retry-After` (15 minutes without one); 402 or a rejected token for 24 h;
  a 5xx or network error is retried once after 20-40 s. LinkedIn and Indeed pages are never fetched
  directly when Bright Data is paused (the listing stays "could not check", never Apply now); an
  employer's own page gets one direct fallback.
- **Apify** (employer headquarters): 1 at once, 2 s apart, 30 new employers per run and 60 per day.
  A 429 gets 3 attempts with 1-2 s then 2-4 s jittered waits, then a 30-minute pause; a 5xx or
  network error up to 2 retries (5-10 s, 15-30 s); 401/403 a 24 h pause. A failed lookup is cached
  for 6 hours so an outage does not repeat paid calls; unknown employers are still cached 30 days.
- **Public ATS boards** (target sweep): limits are keyed by provider and host, so one throttled
  Workday tenant never stops Greenhouse. Greenhouse, Ashby, SmartRecruiters, Workable, Breezy: 4 at
  once, 1 per host, 250 ms apart; Workday, UltiPro, iCIMS: 2 at once, 1 per host, 1 s apart (iCIMS
  HTML 1.5 s). 429: `Retry-After` or 15 minutes; 401/403: host blocked 6 h; timeout or 5xx: one retry
  after 5-15 s; 404/410: no retry; three transient failures in 10 minutes pause the host 30 minutes.
  The sweep is degraded but usable with some boards down, and fails only if none could be read.

Since 2024-12-12 the daily CSV carries a `description` column: the posting text as one line of plain
text, capped at 6,000 characters, for the sources that return it with the search (Indeed, JSearch,
USAJOBS, Greenhouse with `?content=true`, Ashby). LinkedIn, Workday, UltiPro, iCIMS, SmartRecruiters,
Workable and Breezy leave it blank for now. The judge reads this text straight from the CSV. JobSpy is pinned at 1.3.0 (curl_cffi session, no automatic retries, `fetch_description` flag).

The chain since 2024-12-17 (tasks registered by `scripts\automation\register-tasks.ps1`, all local time):

- **00:30 `Pull-LinkedIn-Jobs-Night`, `pull-jobs.ps1 -Phase full`, the fast lane.** `linkedin_pull.py 100
  --no-linkedin` (Indeed for five Raleigh terms and eleven remote terms, plus the RemoteOK and
  Remotive APIs, each section merged into the day's locked CSV as it finishes); the target-company (18
  North Carolina boards: Greenhouse, Workday, UltiPro, Workable, Breezy, SmartRecruiters, iCIMS), USAJOBS,
  HubSpot and JSearch sweeps into the same CSV; the judge (`scripts\judge.py`); it stops cleanly after 25 minutes (its default `--minutes`) and reports the gate-passers
  it did not reach as degraded, so a slow Bright Data night never ends in a kill; `apply-check` (curated
  Apply links, degraded when some read closed); `queue-check` (`job_dashboard.py --queue-check-only`:
  the Bright Data listing checks for the needs-validation queue, into `queue-seen.json`, 10-minute
  deadline); checks still running after six minutes are left for the next run, so the step's deadline never
  throws away the finished ones; `render` (`--no-link-check --no-queue-check`: no network, seconds); the alert record;
  one more render so the banner carries it; `scripts\cloud_sync.py` pushes the inputs to the private-brief-repo
  repo. About 25 minutes; the dashboard is ready by about 01:00.
- **01:00 `Pull-LinkedIn-Jobs-Trickle`, `-Phase trickle`.** Since 2024-12-18 the lane is `scripts\linkedin_feed.py`:
  one Bright Data job-listings batch (dataset `gd_lpfll7v5hcqtkxl6l`, `discover_by_keyword`, the same 16 query groups
  as the guest pull) that returns structured records with the full posting text, pay when published and the posted
  date in about two minutes. Spend: `rate_control.BRIGHTDATA_FEED` caps it at 300 records a night (18 per input), a
  second run the same day is refused, the LinkedIn job ids of the last 8 days of CSVs (newest first, at most 1,500)
  ride along in `jobs_to_not_include` so a posting is never bought twice, the window is `Past 24 hours` after the
  first night, and every run appends a JSON line (counts, seconds, estimated dollars at
  `BRIGHTDATA_FEED_PRICE_PER_1K`, default 1.5) to `automation-logs\linkedin-feed-YYYY-MM.jsonl`. The step exits 1
  when the feed cannot run (no token, trigger refused, collection failed or timed out) and the chain falls back to
  the guest-endpoint trickle below for that night; `JOBSEARCH_LINKEDIN_LANE=guest` forces the fallback for a
  comparison night.
  **Fallback: `linkedin_pull.py 100 --trickle`**: LinkedIn alone, the 16 query groups of up to 25 results (three pages) one group every 12 to 20 minutes, so the
  night's LinkedIn work spreads over four to five hours instead of twenty minutes. A 429 pauses 15, then
  30, then 60 minutes, retries the group once and otherwise defers it to the next window; a 429 never
  persists a next-night cooldown any more. The lane keeps its own clock: no group starts after 4 h 50 min (TRICKLE_BUDGET) and a pause the budget
  cannot hold defers the group; what is left waits for the next night (the step's 300-minute cap is only a backstop). For rows that pass the title, place and pay gates and carry
  no text, the posting text is fetched from the public job page (`scripts\linkedin_detail.py`,
  JSON-LD first, markup fallback) 8 to 15 seconds apart, once per role key (a posting the day's CSV already
  holds, or that an earlier term returned, is not fetched again: 2024-12-18 spent 30 of 47 fetches on repeats);
  a 429 there pauses the fetches until the next group, a second 429 or a sign-in wall stops them for the run,
  and the judge's Bright Data fallback remains. Every LinkedIn request (search
  group or detail page) appends one line to `automation-logs\linkedin-probe-YYYY-MM.jsonl`: time,
  lane, group, outcome, rows, seconds since the previous request and since the last 429. No URLs, no
  page text. Ten nights of that log decide the next pacing change. Why: the 429s of 10/17 and 10/18
  came on the first page of the night, which the burst-throttle theory behind the 10/16 re-pacing does
  not explain, and 10/19's 212 LinkedIn rows carried no text (80 Unscored).
- **The trickle ends with the processing steps (since 2024-12-20).** Judge, queue-check, render (step names
  suffixed `-trickle`, the night's records carried into the status file) and the cloud sync run at about 01:05, so the
  LinkedIn rows reach the 09:30 brief while the laptop is awake. The render writes the status file's `processed` stamp.
- **`-Phase late`** is no longer a scheduled task (the 07:30 task never fired on a morning the laptop slept). The hourly
  catch-up runs it (steps suffixed `-late`) whenever today's CSV is newer than `processed`.
- **07:00 hourly for 16 hours and at sign-in, `Pull-LinkedIn-Jobs-Catchup`, `-Phase catchup`.** Exits
  in a second when today's run finished or is under way (status touched within 45 minutes: every step rewrites it, none runs longer than 30 min); otherwise runs
  the full chain. This is the guarantee after a missed night (WakeToRun does not reliably wake a sleeping laptop and
  StartWhenAvailable does not always run the missed instance). The tasks are pinned to local time. Task Scheduler history is still disabled (needs an
  administrator: `wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true`).
  2024-12-19: the hourly trigger is a Daily trigger carrying a one-hour repetition for 16 hours (the -Once trigger
  registered on 10/20 repeated on that day only and then had no next run). The chain's decisions now live in
  `pull-jobs.lib.ps1` with Pester 3 tests in `tests\pull-jobs.lib.Tests.ps1` and `tests\scheduled-tasks.Tests.ps1`
  (`powershell -NoProfile -Command "Invoke-Pester -Path tests"`): a late or trickle pass that finds no night run for
  today becomes the full chain itself (it used to write a finished status with no pull step, which silenced the
  catch-up); every full run that came from a catch-up or such a switch also runs `linkedin-feed` (the feed's daily
  budget refuses a second run, exit 0); a status left unfinished for 45 minutes is a dead chain and the next run
  carries a failed `chain-died` step naming the last finished step; `pipeline-status.json`, `job-dashboard-data.json`
  and `apply-liveness.json` are replaced atomically (temp file, then one replace); the banner shows the alert
  record's headline as `Chain: ...`.

The night task must run after
midnight because the CSV and every later step are keyed to the calendar date. Nothing opens Claude on
the laptop in the morning any more (the 08:05 `Open-Claude-Morning` task was retired 2024-12-10): the
daily brief is a cloud routine that reads the private private brief repo, which
`\JobSearch\Cloud-Sync` (at sign-in and 08:50 hourly, `scripts\cloud_sync.py`) keeps current. On Mondays that brief also
carries the weekly numbers (the `scripts\market_snapshot.py` text the sync writes as
`inputs\market-snapshot.md`, stage counts against the previous Monday, write-off suggestions); the
laptop's Monday weekly review is retired.

`pull-jobs.ps1 -Phase pull` and `-Phase process` remain for a manual split run (the pull alone, or
everything but the pull reading the CSV a pull already saved). Every step has a deadline and a
failure record.

## Automation bootstrap and verification

The dedicated Windows venv is reproducible from the pinned file:

```powershell
scripts\automation\venv\Scripts\python.exe -m pip install -r scripts\automation\requirements.txt
```

Before re-registering the scheduled tasks (`register-tasks.ps1`: 00:30 full, 01:00 trickle, the catch-up at sign-in,
unlock, resume and hourly; Cloud-Sync was registered by hand and is left alone), run these in order:

1. `scripts\automation\venv\Scripts\python.exe -m unittest discover -s tests -v`
2. `powershell -NoProfile -Command "Invoke-Pester -Path tests"` (the PowerShell chain logic)
3. only after those pass, a live smoke run (`pull-jobs.ps1 -Phase catchup`) and `scripts\automation\register-tasks.ps1`

The importer gate (`importer_gate.py`) went with the job-agent retirement on 2024-12-17: there is no
import step left to gate.

`weekly-sweeps\linkedin-raw-*.csv` remains discovery input only. The curated Apply/Skip authority
is still `dashboard\job-dashboard-data.json`; generated dashboard files must be rebuilt through
`scripts\job_dashboard.py`, never hand-edited.

## The judge: gold set and verdict store (since 2024-12-13)
`dashboard\verdicts.db` is a small
SQLite file owned by the Python chain (git-ignored; tables `verdict`, `gold`, `disagreement`,
`sentinel_run`, schema in `scripts\judge_store.py`).
Rows are keyed on the role key (normalized company | normalized title), so curated data, pipeline.md
and the daily CSV join without a URL.

- `python scripts\gold_import.py --seed` loads past decisions as gold labels: curated Apply and Skip
  entries, and pipeline.md rows whose decision is Pursue or whose status says applied, submitted,
  interview, screen or offer (Apply) or skip (Skip). Found-and-never-acted rows are not labels. A
  seed never overwrites a label the owner set in the form.
- `python scripts\gold_form.py --count 100` writes `reports\gold-form-<date>.html`: 100 postings with
  text, 40/30/30 gate-pass, near-miss (exactly one of title, place, pay failed), other; rows already
  labelled are skipped, so it can be re-run on later nights to add more. The owner labels Apply / Maybe /
  Skip plus an optional reason; choices save in the browser and Export writes
  `gold-labels-<date>.json`, which `python scripts\gold_import.py --labels <file>` loads.
- The code gate (`judge_store.gate`): title vocabulary (`sweep_common.relevant`), Raleigh-Durham or
  remote, yearly pay at or above $70,000 when listed (hourly x 2080; unknown pay passes), and at
  least 300 characters of text. Only gate-passers reach the model.

### The judge step (`scripts\judge.py`, in the chain right after the sweeps)
Reads today's CSV. A row that fails title, place
or pay gets a gate verdict (Skip, model `gate`) with no model call. A gate-passer without text has its
page fetched through Bright Data under the shared limits; still blank = `Unscored`. Each remaining row
is one real-time call to `claude-sonnet-5-5` (SDK `anthropic==1.15.0`): the rubric
(`scripts\judge_prompt.md`, `rubric_version` in its first line) plus `candidate-profile.md` as a
1-hour cached system prefix, the posting as the user turn, `scripts\judge_schema.json` as the output
schema, effort low. The model only labels five dimensions strong / partial / weak with a phrase copied
from the posting; code verifies every phrase (exact after normalising dashes, quotes and spaces, else
fuzzy at 0.90), caps an unverified strong at partial, scores (experience 35, skills 25, archetype 15,
culture 15, pay 10; partial = half) and labels: Apply at 75 or more with experience and skills not
weak, confidence not unknown and no injection notice; Skip under 45, or a clearance or language flag,
or four or more missing core requirements; otherwise Maybe. Confidence is a rule from verified
evidence (5 supported, 3 to 4 partial, else unknown), never self-reported. Refusal, truncation or bad
JSON = `Unscored` with the reason, never silently dropped. Verdicts are keyed on (role key, profile
version, rubric version, prompt version, model), so a re-run makes no call and a rubric change
re-judges. The `disagreement` table holds archived rows from when the keyword band was compared with the judge
(until 2024-12-17); nothing writes it now. Caps: 400 postings and an estimated $5 a night.

Real time, not batch, on purpose: the chain needs verdicts before the render and the 08:30 brief, and
the Batch API can take up to an hour; at about 50 gate-passers a night the difference is a few
dollars a month. A batch mode for re-scoring passes is planned, not built. `--pilot N` judges N rows and prints
token usage, cache hits and rate-limit headers; `--dry-run` lists what would be sent. The key lives in
the Windows user environment as `ANTHROPIC_API_KEY` (the chain reads user variables the same way it
reads the Bright Data token); without it the step skips itself and the banner says so. Shadow mode: the dashboard shows no judge label until `scripts\judge_calibrate.py` reports READY.

Since 2024-12-19 (rubric 2024-12-19a, after the first gold round): the form carries two facets with quotes, `level`
(entry, target, senior, unstated: what the posting says about the seat's level) and `office_days` (remote,
hybrid_3_or_fewer, hybrid_4_plus, onsite, unstated). The label rule gained one clause: a weak archetype on a seat the
posting itself calls entry level, Level I or Tier 1 (verified quote) is Skip, not Maybe; a junior-sounding title alone
changes nothing. The culture rule (an example configuration in `judge_prompt.md`): remote anywhere in the US, hybrid with
three or fewer office days in the home region, on site only inside the on-site area. The label rules live in code (`judge.derive_label`,
`LABEL_RULE_VERSION`, stamped into each verdict's provenance); `python scripts\judge.py --rescore` re-derives every
stored label and score from the saved forms with no model call, so a rule change never needs the model again. An
Unscored row (no text, transient error, refusal) is not a verdict: the next run judges the posting again.

`python scripts\judge.py --gold` judges every gold-labelled posting whose text is in any daily CSV
(the plain files, so the prefilter's drops do not matter), which is how the form's labels get their
verdicts. `python scripts\judge_calibrate.py` then writes `reports\judge-calibration-<date>.html`:
the **model pairs** first (gold rows the model itself labelled: quadratic-weighted kappa, Cohen's kappa, the 3x3
matrix, per-class precision and recall, Apply recall), the **gate pairs** (gold rows the code gate skipped, with the
agreement count), the combined table that was the only number before 2024-12-19, the Unscored count, and every
disagreement with its stage and the judge's summary line. READY needs `--min-n 100` model pairs, `--threshold 0.6`
weighted kappa on them and `--min-apply-recall 0.8` on them (a first round scored mostly
gate Skips the owner agreed with; at 100 to 150 pairs the kappa confidence interval is about plus or minus 0.25, so the recall gate
carries the weight). The run is recorded in `sentinel_run`.

The gold table is the one part of `verdicts.db` that cannot be rebuilt, and the database is git-ignored on a
laptop with no other backup. Since 2024-12-19 `gold_import.py` rewrites `dashboard\gold-labels.json` after every
import (and on `--export`): every gold row with its label, reason, source, time, and the posting text and CSV date
it was labelled on, sorted and canonical so git shows what changed. The gold-round skill commits and pushes it;
`gold_import.py --restore dashboard\gold-labels.json` rebuilds the table on a new machine.

## Curated data
`job-dashboard-data.json` is the curated Apply/Skip **authority**. The key tools list lives in
`scripts\validate_listing.py` (`KEY_TOOLS`).

## Running on GitHub Actions

A laptop is off most nights, so the chain can also run on a GitHub Actions Windows runner from
`.github/workflows/night-chain.yml`. The same `pull-jobs.ps1` runs there with `JOBSEARCH_ROOT` set to the checkout and `JOBSEARCH_PYTHON`
to the runner's interpreter.

- **Schedule.** Cron slots at 04:30 and 05:30 UTC; the first step sets the runner to Eastern time and keeps the
  slot whose local hour is 0, so the chain starts at 00:30 local in both summer and standard time. `full` then
  `trickle` run back to back; nothing waits for 01:00 on the runner.
- **State.** `scripts/automation/state_sync.py restore` copies the orphan `state` branch into the checkout before
  the chain and `save` writes it back after: verdicts.db, queue-seen.json, provider-state.json, employer-hq.json,
  apply-liveness.json, pipeline-status.json, jsearch-last-run.txt, 21 days of `linkedin-raw-*.csv`, and the logs of
  the current and previous month. The branch grows by a few MB a night; replace it with a fresh orphan when that matters.
- **Outputs.** `job-dashboard.md`, `dashboard/job-dashboard-data.json` and `automation-logs/pipeline-status.json` are
  committed on main after each run (tracked since 2024-12-23). `job-dashboard.html` is not: it embeds the laptop's
  decision token (`dashboard/.decision-token`), which authenticates the Later and Applied buttons to the localhost
  helper, and a page rendered on the runner would carry a different token. To read the dashboard on the laptop,
  `show-my-jobs` pulls main, restores the `state` branch (`state_sync.py restore` from a worktree of it) and renders
  the page locally with `--no-link-check --no-queue-check`.
- **Secrets**, entered in the repository settings by the candidate, never by Claude: `ANTHROPIC_API_KEY`,
  `USAJOBS_API_KEY`, `JSEARCH_API_TOKEN`, `APIFY_TOKEN`, `BRIGHTDATA_API_TOKEN`, and `BRIEF_REPO_TOKEN`, a fine-grained
  token with contents read and write on `your-github-user/private-brief-repo` so `cloud_sync.py` can push the brief's inputs.
  Repository variable `BRIGHTDATA_UNLOCKER_ZONE` (default web_unlocker). The Bright Data CLI reads the same token as
  `BRIGHTDATA_API_KEY`.
- **Minutes.** 27 to 38 wall minutes a night, billed double on Windows: 1,600 to 2,300 a month; check your plan's included minutes.
- **First runs.** Actions tab, Night chain, Run workflow with `dry_run` on: no paid calls, state and outputs are not
  committed, the setup and the restore are proved. Then a manual run with `dry_run` off, then the schedule.
- **Parallel week.** The laptop tasks keep running for seven nights; compare its status file with the committed one
  each morning (rows, labels, step states). Then run `register-tasks.ps1` with the three chain tasks removed, delete
  the laptop's key variables, and keep the runner as the only chain. Two chains on two machines pay for the feed twice.
- **Logs.** Each run uploads `automation-logs` as an artifact kept 14 days; the status file's `alert.headline` is in
  the run's summary output.
