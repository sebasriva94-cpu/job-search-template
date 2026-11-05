"""TEMPLATE. Copy to spec_<company>_<role>_<YYYY_MM_DD>.py and fill every <...>.

Tailored resume requested <YYYY-MM-DD>: <Company> <Role> (<ATS id>, <location rule>, <pay if published>).
Every fact comes from the base resume, candidate-profile.md, and the verified bullet library.
Left out on purpose: <claims the posting wants that are not on the owner's record, and why>.

    python scripts\\resume_tailoring\\spec_<company>_<role>_<YYYY_MM_DD>.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import build_resume as br  # noqa: E402
from bullets import CERTS, JOB_C, EDU, JOB_A, LANG, JOB_D, PROJECTS, JOB_B, C, K, N, P, T, job  # noqa: E402
# Optional extra bullets, import only what you use (every shared bullet lives in bullets.py):
# Add your own extra bullets to bullets.py and import them here.

DATE = "<YYYY-MM-DD>"

SPEC = {
    "slug": "<company>-<role-words>",
    "company": "<Company>",
    "role": "<Role title exactly as posted (ATS id)>",
    "where": "<Remote (US) | Hybrid, n days in <city>, NC | On site <city>, NC>",
    # Location header: remote or home-region role -> leave as None (Anytown,
    # ST). Onsite or hybrid in another metro -> br.relocating_contact("<Metro>"), e.g. "Minneapolis", so the
    # header reads "Anytown, ST (relocating to Minneapolis)". Never claim the owner already lives there.
    "contact": None,
    "pay": "<published range or 'not published'>",
    "url": "<posting url>",
    "verdict": "<Apply | Maybe>. <one sentence on the match and the one real gap>",
    "summary": ("<3 lines max, plain language, exact posting title, no filler phrases>"),
    "skills": [
        ("<Group named the way the posting names it>", "<tools the owner has used, posting's wording first>"),
        LANG,
    ],
    "jobs": [
        job(JOB_A, [K["support"], K["endpoint"], K["identity"]]),
        job(JOB_B, [T["support"], T["guides"]]),
        job(JOB_C, [C["troubleshoot"], C["identity"]]),
        job(JOB_D, [N["support"], N["accounts"]]),
    ],
    "projects": {"heading": PROJECTS, "bullets": [P["jobs"]]},
    "education": EDU,
    "certs": CERTS,
}


def main() -> int:
    r = br.build(SPEC, date=DATE)
    print(f"{r['slug']} pages={r['pages']} bullets={r['bullets']} words={r['words']}")
    print(r["pdf"])
    print(r["docx"])
    return 0 if r["pages"] <= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
