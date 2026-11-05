---
name: build-tailored-resume
description: Build a tailored resume (DOCX + PDF) for one specific posting with the house builder. Use when the candidate says "tailor my resume for", "build a resume for", "resume for this job", or after a show-my-jobs Apply decision the owner wants to act on. One spec file per job, bullets only from the verified library, PDF shown for approval before it is called final. Never fabricates.
---

# build-tailored-resume

The builder `scripts\resume_tailoring\build_resume.py` clones the ratified base resume's Word styling and renders a PDF
through headless Chrome. A **spec** (a Python dict) says what goes on the page. Every bullet comes from the verified
library; new wording is allowed only when it restates a fact already in `candidate-profile.md`.

## Where the verified bullets live

- `scripts\resume_tailoring\bullets.py`: the one library (ships with fictional example data; replace it with your
  own verified facts). Import from it: `from bullets import K, T, C, N, P, JOB_A, ...`.
  - core: `K` (JOB_A bullets), `T` (JOB_B), `C` (JOB_C), `N` (JOB_D), `P` (projects), the `JOB_A` to `JOB_D`
    role headers, `EDU`, `CERTS`, `LANG`, `PROJECTS`, and `job(meta, bullets)`.
  - federal (USAJOBS) role headers: `FED_EDU`, `FED_CERTS`, `FED_JOB_A`, `FED_JOB_B`, `FED_JOB_C`, `FED_JOB_D`, so a
    federal header is never called `JOB_A` or `EDU` anywhere.
- Add any new shared bullet to `bullets.py`, never retype it inside a spec.
Grep `bullets.py` before writing a bullet; if the fact exists, import it, do not retype it.

## Steps

1. **Inputs.** Read `candidate-profile.md`, `prompts\resume-tailoring-prompt.md` (the style rules) and the posting
   (parsed file in `parsed-jobs\`, or the URL through the fallback chain). Note the exact title, location rule, pay,
   and the required vs preferred items.
2. **Reuse check.** `ls resumes\tailored\` and `scripts\resume_tailoring\spec_*.py` for the same company or a near
   identical title. If one exists, start from that spec.
3. **New spec.** Copy `scripts\resume_tailoring\spec_TEMPLATE.py` to `spec_<company>_<role>_<YYYY_MM_DD>.py`. Fill the
   docstring (posting, date, what was deliberately left out and why), `DATE`, and the dict: slug, company, role, where,
   pay, url, verdict, summary (3 lines max, plain, no filler phrases), skills groups led by the posting's own tool
   names where the owner has them, jobs with 3 to 6 bullets each chosen from the library, projects, education, certs.
   Location line: a remote role, or onsite/hybrid in the home region, keeps the default
   `br.CONTACT` (Anytown, ST). An onsite or hybrid role in another metro (Greater Minneapolis is a target area)
   sets the spec's `contact` field to `br.relocating_contact("<Metro>")`, e.g. "Minneapolis", so the header reads
   "Anytown, ST (relocating to Minneapolis)" (one line). Use the metro, not the
   suburb, for Greater Minneapolis towns. Never claim the owner already lives in the target metro, and never
   leave the home-city line on a resume for an onsite role elsewhere.
4. **Build.** `python scripts\resume_tailoring\spec_<...>.py` (the builder's own interpreter, it needs
   python-docx and pypdf there). Exit 1 means more than two pages: cut bullets, do not shrink fonts.
   The builder rejects em-dashes, en-dashes and tildes by itself.
5. **Quality pass** against the prompt's checklist: no tool the owner has not used, no inflated number, title in the summary
   matches the posting, >= 70 percent of the required keywords present verbatim where true, 2 pages max.
6. **Show, then wait.** Open the PDF for the owner (`resumes\tailored\<slug>-<date>.pdf`). State the gaps you left out on
   purpose. Only after the owner approves call it final; if the owner asks for changes, edit the spec and rebuild (never the DOCX).
7. **Record.** Add or update the `pipeline.md` row (tailored on <date>, spec file name). Commit the spec file.

## Rules

- Never fabricate a tool, credential, metric or date. A certification in progress stays "in progress".
- No em-dashes, en-dashes or tildes anywhere in the resume or in chat.
- The base `resumes\base_resume.docx` is never edited by this skill.
