"""Federal (USAJobs) tailored-resume builder.

Clones the paragraph and run XML of the ratified federal base
(resumes/base_resume_federal.docx: Arial; name 14pt; headings 12pt bold caps; body 11pt;
date and salary lines 10pt italic; 0.5in margins; hanging bullets) and rebuilds the body from a content spec,
so a tailored federal DOCX matches the ratified base exactly. The PDF is printed by Chrome headless from a
style-matched HTML page and the page count is returned so every resume can be held to the two-page cap.

Spec keys: slug, summary, competencies, skills [(label, text)], jobs [{title, company, start, end, hours, bullets}],
education [..], certs [..]. Dates are MM/DD/YYYY strings; hours is an int (hours per week).

    from build_resume_federal import build
    build(spec, date="2024-11-05", out_dir=DRAFTS)   # writes <out_dir>/<slug>-<date>.docx and .pdf
"""
from __future__ import annotations

import copy
import html as html_lib
import pathlib

import docx
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

import build_resume as br

BASE = br.ROOT / "resumes" / "base_resume_federal.docx"  # your own federal base resume
OUT_DIR = br.OUT_DIR
TMP = br.TMP
NAME = br.NAME
CONTACT = br.CONTACT
BULLET = br.BULLET
# Your own federal eligibility line (citizenship, any veterans' preference, clearance status). Example only:
ELIGIBILITY = "U.S. Citizen  |  Veterans' Preference: <none or type>  |  Clearance: <none or status>"
META_LINE = "Salary: Available upon request | Supervisor: Available upon request | May contact: Available upon request"

# paragraph indices in the federal base that serve as XML templates
TEMPLATE_INDEX = dict(name=0, contact=1, elig=2, heading=3, body=4, comp=6, skill=8, title=15, company=16,
                      dates=17, meta=18, bullet=19, edu=46, cert=49)

CSS = """
@page { size: Letter; margin: 0.5in; }
html, body { margin: 0; padding: 0; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; color: #000; background: #fff; }
p { margin: 0; line-height: 1.15; }
.name { font-size: 14pt; font-weight: bold; margin-bottom: 1pt; }
.contact { font-size: 10pt; margin-bottom: 1pt; }
.elig { font-size: 10pt; margin-bottom: 3pt; }
h2 { font-family: Arial, Helvetica, sans-serif; font-size: 12pt; font-weight: bold; margin: 8pt 0 3pt 0; padding-bottom: 1pt;
     line-height: 1.15; page-break-after: avoid; break-after: avoid; }
.body { margin-bottom: 3pt; break-inside: avoid; page-break-inside: avoid; }
.comp { margin-bottom: 3pt; }
.skill { margin-bottom: 2pt; }
.title { font-weight: bold; margin-top: 4pt; page-break-after: avoid; break-after: avoid; }
.company { page-break-after: avoid; break-after: avoid; }
.dates { font-size: 10pt; font-style: italic; page-break-after: avoid; break-after: avoid; }
.meta { font-size: 10pt; font-style: italic; margin-bottom: 2pt; page-break-after: avoid; break-after: avoid; }
.bullet { padding-left: 15.85pt; text-indent: -11.5pt; margin-bottom: 3pt; break-inside: avoid; page-break-inside: avoid; }
.edu { margin-bottom: 1pt; }
.cert { margin-bottom: 1pt; }
"""


def dates_line(job: dict) -> str:
    return f"{job['start']} - {job['end']}  |  {job['hours']} hours/week  |  Federal position: No"


def all_text(spec: dict) -> list[str]:
    texts = [spec["summary"], spec["competencies"]]
    texts += [f"{label}: {text}" for label, text in spec["skills"]]
    for job in spec["jobs"]:
        texts += [job["title"], job["company"], dates_line(job), *job["bullets"]]
    texts += spec["education"] + spec["certs"]
    return texts


def check_text(spec: dict) -> None:
    for t in all_text(spec):
        if br.FORBIDDEN.search(t):
            raise ValueError(f"forbidden dash or tilde in: {t!r}")
        if "  " in t.replace("  |  ", ""):
            raise ValueError(f"double space in: {t!r}")


def _heading_has_rule() -> bool:
    doc = docx.Document(str(BASE))
    ppr = doc.paragraphs[TEMPLATE_INDEX["heading"]]._p.pPr
    return ppr is not None and ppr.find(qn("w:pBdr")) is not None


def build_docx(spec: dict, out_path: pathlib.Path) -> None:
    doc = docx.Document(str(BASE))
    paragraphs = doc.paragraphs
    templates = {k: copy.deepcopy(paragraphs[i]._p) for k, i in TEMPLATE_INDEX.items()}
    body = doc.element.body
    sect = body.find(qn("w:sectPr"))
    for child in list(body):
        if child is not sect:
            body.remove(child)

    def add(kind: str, texts: list[str], keep_next: bool = False) -> Paragraph:
        p = copy.deepcopy(templates[kind])
        sect.addprevious(p)
        para = Paragraph(p, doc._body)
        br._set_runs(para, texts)
        if keep_next:
            para.paragraph_format.keep_with_next = True
        if kind in ("bullet", "body", "cert", "comp"):
            para.paragraph_format.keep_together = True
        return para

    add("name", [NAME])
    add("contact", [CONTACT])
    add("elig", [ELIGIBILITY])
    add("heading", ["PROFESSIONAL SUMMARY"], keep_next=True)
    add("body", [spec["summary"]])
    add("heading", ["CORE COMPETENCIES"], keep_next=True)
    add("comp", [spec["competencies"]])
    add("heading", ["TECHNICAL SKILLS"], keep_next=True)
    for label, text in spec["skills"]:
        add("skill", [f"{label}: ", text])
    add("heading", ["PROFESSIONAL EXPERIENCE"], keep_next=True)
    for job in spec["jobs"]:
        add("title", [job["title"]], keep_next=True)
        add("company", [job["company"]], keep_next=True)
        add("dates", [dates_line(job)], keep_next=True)
        add("meta", [META_LINE], keep_next=True)
        for b in job["bullets"]:
            add("bullet", [BULLET + b])
    add("heading", ["EDUCATION"], keep_next=True)
    for line in spec["education"]:
        add("edu", [line])
    add("heading", ["CERTIFICATIONS"], keep_next=True)
    for line in spec["certs"]:
        add("cert", [BULLET + line])
    doc.save(str(out_path))


def build_html(spec: dict) -> str:
    e = html_lib.escape
    css = CSS
    if _heading_has_rule():
        css = css.replace("padding-bottom: 1pt;\n     line-height", "padding-bottom: 1pt; border-bottom: 0.75pt solid #000;\n     line-height")
    nb = "&nbsp;&nbsp;|&nbsp;&nbsp;"
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(NAME)}</title><style>{css}</style></head><body>"]
    parts.append(f'<p class="name">{e(NAME)}</p>')
    parts.append(f'<p class="contact">{e(CONTACT).replace("  |  ", nb)}</p>')
    parts.append(f'<p class="elig">{e(ELIGIBILITY).replace("  |  ", nb)}</p>')
    parts.append("<h2>PROFESSIONAL SUMMARY</h2>")
    parts.append(f'<p class="body">{e(spec["summary"])}</p>')
    parts.append("<h2>CORE COMPETENCIES</h2>")
    parts.append(f'<p class="comp">{e(spec["competencies"])}</p>')
    parts.append("<h2>TECHNICAL SKILLS</h2>")
    for label, text in spec["skills"]:
        parts.append(f'<p class="skill"><b>{e(label)}: </b>{e(text)}</p>')
    parts.append("<h2>PROFESSIONAL EXPERIENCE</h2>")
    for job in spec["jobs"]:
        parts.append(f'<p class="title">{e(job["title"])}</p>')
        parts.append(f'<p class="company">{e(job["company"])}</p>')
        parts.append(f'<p class="dates">{e(dates_line(job)).replace("  |  ", nb)}</p>')
        parts.append(f'<p class="meta">{e(META_LINE)}</p>')
        for b in job["bullets"]:
            parts.append(f'<p class="bullet">\u2022&nbsp; {e(b)}</p>')
    parts.append("<h2>EDUCATION</h2>")
    for line in spec["education"]:
        parts.append(f'<p class="edu">{e(line)}</p>')
    parts.append("<h2>CERTIFICATIONS</h2>")
    for line in spec["certs"]:
        parts.append(f'<p class="cert">\u2022&nbsp; {e(line)}</p>')
    parts.append("</body></html>")
    return "\n".join(parts)


def build(spec: dict, date: str, out_dir: pathlib.Path = OUT_DIR) -> dict:
    check_text(spec)
    TMP.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{spec['slug']}-{date}"
    docx_path = out_dir / f"{stem}.docx"
    pdf_path = out_dir / f"{stem}.pdf"
    html_path = TMP / f"{stem}.html"
    build_docx(spec, docx_path)
    html_path.write_text(build_html(spec), encoding="utf-8")
    pages = br.render_pdf(html_path, pdf_path)
    bullets = sum(len(j["bullets"]) for j in spec["jobs"])
    return {"slug": spec["slug"], "docx": str(docx_path), "pdf": str(pdf_path), "html": str(html_path),
            "pages": pages, "bullets": bullets, "words": sum(len(t.split()) for t in all_text(spec))}
