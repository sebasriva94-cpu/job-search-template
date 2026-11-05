---
name: gold-round
description: Run one labelling and calibration round for the nightly job judge. Use when the candidate says "gold round", "label jobs", "calibrate the judge", "is the judge ready", "check kappa", or after the owner exports labels from the gold form; also for any change to the judge rubric (scripts\judge_prompt.md) or candidate-profile.md, which must pass an independent sub-agent review before the rubric version bumps.
---

# gold-round

The judge (`scripts\judge.py`) stays in shadow until the calibration reads READY (100 model pairs, weighted kappa 0.6 and Apply recall 0.8 on
them; the code gate's Skips are scored apart). This skill is the loop that gets it there. Read the "judge" sections of
`dashboard\README.md` if a detail below is unclear.

All commands run from the repo root with `scripts\automation\venv\Scripts\python.exe` (below: `PY`).

## Mode A: labelling round

1. **Status first.** Query `dashboard\verdicts.db` read-only: count of `gold` rows, count of `verdict` rows by
   `model_id`, newest `sentinel_run`. Tell the owner in one line how many labels exist and how many are still needed to 150.
2. **Build the form.** `PY scripts\gold_form.py --count 100` writes `reports\gold-form-<date>.html` (postings with text,
   stratified 40 gate-pass / 30 near-miss / 30 other; already-labelled rows are skipped). Open it for the owner (HTML in Chrome).
3. **The owner labels** Apply / Maybe / Skip in the browser and clicks Export, which saves `gold-labels-<date>.json`. Wait for
   the file path from the owner; never invent labels.
4. **Import.** `PY scripts\gold_import.py --labels <path-to-json>`. A seed never overwrites a label the owner set. The import
   also rewrites `dashboard\gold-labels.json` (every gold row with the posting text it was labelled on; `verdicts.db`
   itself is git-ignored and the laptop has no other backup). **Commit and push that file** in the same round:
   `git add dashboard\gold-labels.json`, a message naming the round, `git push`. `--restore <file>` rebuilds the table.
5. **Judge the gold.** `PY scripts\judge.py --gold` (needs `ANTHROPIC_API_KEY` in the user environment; if the script
   says it is missing, stop and tell the owner, do not look for the key). Report token usage and Unscored count.
6. **Calibrate.** `PY scripts\judge_calibrate.py` writes `reports\judge-calibration-<date>.html` and prints READY or
   NOT YET. Open the report for the owner. Summarize the **model pairs** (gold rows the model labelled: n, weighted kappa,
   Apply recall), the gate pairs and their agreement, and the top disagreements by stage. READY is judged on the model
   pairs alone: 100 of them, kappa 0.6, Apply recall 0.8 (2024-12-19). After any change to a label rule in
   `scripts\judge.py`, run `PY scripts\judge.py --rescore` first (free) so stored verdicts carry the new rule.
7. **If READY:** say so, and that the dashboard switch (judge label replaces the keyword band) is a separate, approved
   change to `scripts\job_dashboard.py` with its own tests. Do not flip it inside this round.

## Mode B: rubric or profile change (independent review required)

Any edit to `scripts\judge_prompt.md` or `candidate-profile.md` changes what the judge sees, so:

1. Draft the change and save the diff to the scratchpad. Do not bump `rubric_version` yet.
2. **Spawn a reviewer sub-agent** (fresh context, read-only) with: the diff, the current disagreement list
   (`disagreement` table joined to `gold`), and the rule "the model only labels; code decides". Ask it to find: a label
   definition the model could read two ways, a bias the worked example teaches, any instruction that lets posting text
   steer the verdict, and anything that would move a gold Apply to Skip. It returns a short list, no edits.
3. Apply the accepted findings; log rejected ones in one line each in the commit message.
4. Bump `rubric_version` in the first line of `scripts\judge_prompt.md` (the date plus a letter).
5. Run `PY -m unittest discover -s tests -p "test_judge*.py"` and `PY -m unittest discover -s tests -p "test_gold*.py"` from the
   Job Search root (tests/ is not a package), then the full suite before commit.
6. Re-judge the gold (`PY scripts\judge.py --gold`; the new version makes new calls, old verdicts stay) and run the
   calibration report so the change is measured, not assumed.

## Rules

- Verdicts and gold rows are keyed on the role key; never edit `verdicts.db` by hand.
- Paid calls are capped by `judge.py` (400 postings, $5 a night); a `--gold` run over many rows is still paid work,
  so say the expected count before running it when it exceeds 100.
- No em-dashes, en-dashes or tildes in anything written for the owner.
