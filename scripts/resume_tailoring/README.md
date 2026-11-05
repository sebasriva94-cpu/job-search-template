# Resume tailoring builder

`build_resume.py` clones the paragraph and run XML of `resumes/base_resume.docx`
(Arial; name 14pt; headings 12pt bold caps with a rule; body 10pt; 0.5in margins; hanging bullets) and
rebuilds the body from a content spec, so a tailored DOCX matches the ratified base exactly. The PDF is
printed by Chrome headless from a style-matched HTML page (no Word or LibreOffice on this machine) and the
builder returns the page count so every resume can be held to two pages.

`bullets.py` is the one verified bullet library (fictional Employers A to D and independent projects)
since 2024-12-17; the dated spec files import from it. For a new resume copy `spec_TEMPLATE.py`, edit the spec
(summary, skills groups, bullet selection and order, what changed, honesty check), and run it with the pinned venv:

    scripts\automation\venv\Scripts\python.exe scripts\resume_tailoring\spec_<company>.py

Outputs land in `resumes/tailored/<slug>-<date>.docx` and `.pdf` plus an index HTML. Rules baked in:
no em dashes, en dashes, or tildes; no double spaces; every bullet keeps together across a page break.
Facts only from the base resume, candidate-profile.md, the LinkedIn export, or the story bank.
