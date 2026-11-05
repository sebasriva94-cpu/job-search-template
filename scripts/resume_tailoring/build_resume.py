"""Tailored-resume builder.

DOCX: clones the standard base resume's paragraph and run XML (fonts, sizes, spacing, rule under headings,
hanging bullet indent) and rebuilds the body from a content spec, so the house style is exact.
PDF: Chrome headless prints a style-matched HTML page (no Word or LibreOffice on this machine).

    from build_resume import build
    build(spec)               # writes resumes/tailored/<slug>-<date>.docx and .pdf, returns page count
"""
from __future__ import annotations

import copy
import html as html_lib
import os
import pathlib
import re
import subprocess

import docx
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the repo root
BASE = ROOT / "resumes" / "base_resume.docx"  # your own base resume (resumes/ is gitignored)
OUT_DIR = ROOT / "resumes" / "tailored"
TMP = ROOT / "resumes" / "_build"  # HTML scratch; under the gitignored resumes/ tree
# Chrome's headless profile (it writes cookies and a Login Data store) lives outside the synced Documents tree (2024-12-19)
PROFILE = pathlib.Path(os.environ.get("LOCALAPPDATA", str(TMP))) / "JobSearch" / "chrome-profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DATE = "2024-10-29"

NAME = "ALEX EXAMPLE"
HOME_CITY = "Anytown, ST"
CONTACT_REST = "555-010-0100  |  you@example.com  |  linkedin.com/in/your-profile"
CONTACT = f"{HOME_CITY}  |  {CONTACT_REST}"
RELOCATING = f"{HOME_CITY} (relocating"  # how every relocation header starts: the owner's real city first
FIT_METRO = "Minneapolis"  # the longest metro measured to keep "... (relocating to X)" on one line


def relocating_contact(metro: str) -> str:
    """The header for an onsite or hybrid role outside the home region: the real
    city first, then the move, e.g. "Anytown, ST (relocating to Minneapolis)" (rather than "Minneapolis, MN (relocating)"). A remote or home-region role keeps the default CONTACT. A metro
    name longer than FIT_METRO would wrap the header onto two lines and push one-page resumes to two,
    so it falls back to "(relocating)"."""
    metro = str(metro or "").strip()
    if not metro or "," in metro:
        raise ValueError(f"expected a metro name such as 'Minneapolis', got {metro!r}")
    note = f"(relocating to {metro})" if len(metro) <= len(FIT_METRO) else "(relocating)"
    line = f"{HOME_CITY} {note}  |  {CONTACT_REST}"
    if FORBIDDEN.search(line):
        raise ValueError(f"forbidden dash or tilde in: {line!r}")
    return line


def contact_line(spec: dict) -> str:
    """The spec's own header when it carries one (a relocation metro), else the home default."""
    return str(spec.get("contact") or CONTACT)
BULLET = "\u2022  "
FORBIDDEN = re.compile(r"[\u2014\u2013~]")  # em dash, en dash, tilde: never in anything written for the owner

# paragraph indices in the base that serve as XML templates
TEMPLATE_INDEX = dict(name=0, contact=1, heading=2, body=3, skill=5, title=12, company=13, bullet=14, edu=36, cert=39)

CSS = """
@page { size: Letter; margin: 0.5in; }
html, body { margin: 0; padding: 0; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000; background: #fff; }
p { margin: 0; line-height: 1.15; }
.name { font-size: 14pt; font-weight: bold; margin-bottom: 1pt; }
.contact { margin-bottom: 3pt; }
h2 { font-family: Arial, Helvetica, sans-serif; font-size: 12pt; font-weight: bold; margin: 7pt 0 3pt 0; padding-bottom: 1pt;
     border-bottom: 0.75pt solid #000; line-height: 1.15; page-break-after: avoid; break-after: avoid; }
.body { margin-bottom: 3pt; break-inside: avoid; page-break-inside: avoid; }
.skill { margin-bottom: 2pt; }
.title { font-weight: bold; margin-top: 4pt; page-break-after: avoid; break-after: avoid; }
.company { font-style: italic; margin-bottom: 1pt; page-break-after: avoid; break-after: avoid; }
.bullet { padding-left: 15.85pt; text-indent: -11.5pt; margin-bottom: 3pt; break-inside: avoid; page-break-inside: avoid; }
.edu { margin-bottom: 1pt; }
.cert { margin-bottom: 1pt; }
"""


def all_text(spec: dict) -> list[str]:
    texts = [spec["summary"]]
    texts += [f"{label}: {text}" for label, text in spec["skills"]]
    for job in spec["jobs"]:
        texts += [job["title"], job["dates"], job["company"], *job["bullets"]]
    if spec.get("projects"):
        texts += [spec["projects"]["heading"], *spec["projects"]["bullets"]]
    texts += spec["education"] + spec["certs"]
    return texts


def check_text(spec: dict) -> None:
    custom = spec.get("contact")
    if custom and not str(custom).startswith(RELOCATING):
        raise ValueError(f"a spec contact must start \"{RELOCATING}\" (build it with relocating_contact); "
                         f"another city first claims the owner lives there: {custom!r}")
    for t in [contact_line(spec)] + all_text(spec):
        if FORBIDDEN.search(t):
            raise ValueError(f"forbidden dash or tilde in: {t!r}")
        if "  " in t.replace("  |  ", ""):
            raise ValueError(f"double space in: {t!r}")


def _set_runs(para: Paragraph, texts: list[str]) -> None:
    runs = para.runs
    if len(runs) < len(texts):
        raise ValueError("template paragraph has fewer runs than texts")
    for run, text in zip(runs, texts):
        run.text = text
    for run in runs[len(texts):]:
        run._r.getparent().remove(run._r)


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
        _set_runs(para, texts)
        if keep_next:
            para.paragraph_format.keep_with_next = True
        if kind in ("bullet", "body", "cert"):
            para.paragraph_format.keep_together = True
        return para

    add("name", [NAME])
    add("contact", [contact_line(spec)])
    add("heading", ["PROFESSIONAL SUMMARY"], keep_next=True)
    add("body", [spec["summary"]])
    add("heading", [spec.get("skills_heading", "TECHNICAL SKILLS")], keep_next=True)
    for label, text in spec["skills"]:
        add("skill", [f"{label}: ", text])
    add("heading", ["PROFESSIONAL EXPERIENCE"], keep_next=True)
    for job in spec["jobs"]:
        add("title", [f"{job['title']}  |  {job['dates']}"], keep_next=True)
        add("company", [job["company"]], keep_next=True)
        for b in job["bullets"]:
            add("bullet", [BULLET + b])
    if spec.get("projects"):
        add("heading", [spec["projects"]["heading"]], keep_next=True)
        for b in spec["projects"]["bullets"]:
            add("bullet", [BULLET + b])
    add("heading", ["EDUCATION"], keep_next=True)
    for line in spec["education"]:
        add("edu", [line])
    if spec["certs"]:
        add("heading", ["CERTIFICATIONS"], keep_next=True)
    for line in spec["certs"]:
        add("cert", [BULLET + line])
    doc.save(str(out_path))


def build_html(spec: dict) -> str:
    e = html_lib.escape
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(NAME)}</title><style>{CSS}</style></head><body>"]
    parts.append(f'<p class="name">{e(NAME)}</p>')
    parts.append(f'<p class="contact">{e(contact_line(spec)).replace("  |  ", "&nbsp;&nbsp;|&nbsp;&nbsp;")}</p>')
    parts.append("<h2>PROFESSIONAL SUMMARY</h2>")
    parts.append(f'<p class="body">{e(spec["summary"])}</p>')
    parts.append(f"<h2>{html_lib.escape(spec.get('skills_heading', 'TECHNICAL SKILLS'))}</h2>")
    for label, text in spec["skills"]:
        parts.append(f'<p class="skill"><b>{e(label)}: </b>{e(text)}</p>')
    parts.append("<h2>PROFESSIONAL EXPERIENCE</h2>")
    for job in spec["jobs"]:
        parts.append(f'<p class="title">{e(job["title"])}&nbsp;&nbsp;|&nbsp;&nbsp;{e(job["dates"])}</p>')
        parts.append(f'<p class="company">{e(job["company"])}</p>')
        for b in job["bullets"]:
            parts.append(f'<p class="bullet">\u2022&nbsp; {e(b)}</p>')
    if spec.get("projects"):
        parts.append(f"<h2>{e(spec['projects']['heading'])}</h2>")
        for b in spec["projects"]["bullets"]:
            parts.append(f'<p class="bullet">\u2022&nbsp; {e(b)}</p>')
    parts.append("<h2>EDUCATION</h2>")
    for line in spec["education"]:
        parts.append(f'<p class="edu">{e(line)}</p>')
    if spec["certs"]:
        parts.append("<h2>CERTIFICATIONS</h2>")
    for line in spec["certs"]:
        parts.append(f'<p class="cert">\u2022&nbsp; {e(line)}</p>')
    parts.append("</body></html>")
    return "\n".join(parts)


def render_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path) -> int:
    profile = PROFILE
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
         "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", html_path.as_uri()],
        check=True, capture_output=True, timeout=180,
    )
    return len(PdfReader(str(pdf_path)).pages)


def screenshot(html_path: pathlib.Path, png_path: pathlib.Path, height: int = 2150) -> None:
    profile = PROFILE
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
         f"--screenshot={png_path}", f"--window-size=816,{height}", "--hide-scrollbars", html_path.as_uri()],
        check=True, capture_output=True, timeout=180,
    )


def build(spec: dict, date: str = DATE) -> dict:
    check_text(spec)
    TMP.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{spec['slug']}-{date}"
    docx_path = OUT_DIR / f"{stem}.docx"
    pdf_path = OUT_DIR / f"{stem}.pdf"
    html_path = TMP / f"{stem}.html"
    build_docx(spec, docx_path)
    html_path.write_text(build_html(spec), encoding="utf-8")
    pages = render_pdf(html_path, pdf_path)
    bullets = sum(len(j["bullets"]) for j in spec["jobs"]) + len((spec.get("projects") or {}).get("bullets", []))
    return {"slug": spec["slug"], "docx": str(docx_path), "pdf": str(pdf_path), "html": str(html_path),
            "pages": pages, "bullets": bullets, "words": sum(len(t.split()) for t in all_text(spec))}
