"""The verified resume bullets live in scripts/resume_tailoring/bullets.py; every spec imports them from there.

These tests keep that true: bullets.py stays pure data, every spec file (including the template) imports, the
federal headers never share a name with the standard ones, and the contact-line rules hold.
"""
import ast
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SPEC_DIR = Path(__file__).resolve().parent.parent / "scripts" / "resume_tailoring"

CORE = ["EDU", "CERTS", "JOB_A", "JOB_B", "JOB_C", "JOB_D", "LANG", "K", "T", "C", "N", "P", "PROJECTS", "job"]
STANDARD_HEADERS = ("EDU", "CERTS", "JOB_A", "JOB_B", "JOB_C", "JOB_D")


def _stub_missing_builder_deps():
    """The spec files import the builder, which imports python-docx and pypdf at module level. Both are in
    scripts\\automation\\requirements.txt; stub only what is missing on a thinner venv so the import tests run
    everywhere (RealBuildTests then skips instead of failing)."""
    stubs = {}
    for name, attrs in (("docx", {}), ("docx.oxml", {}), ("docx.oxml.ns", {"qn": None}),
                        ("docx.text", {}), ("docx.text.paragraph", {"Paragraph": object}),
                        ("pypdf", {"PdfReader": object})):
        try:
            importlib.import_module(name)
        except ImportError:
            mod = types.ModuleType(name)
            mod.__dict__.update(attrs)
            stubs[name] = mod
    return stubs


def example_spec(bullets) -> dict:
    """A complete spec built only from the library, the way a real spec_*.py does it."""
    return {
        "slug": "example-it-support", "company": "Example Corp", "role": "IT Support Specialist II",
        "summary": "IT Support Engineer with Tier 2 endpoint and identity experience.",
        "skills": [("Endpoint", "Intune, Jamf Pro"), bullets.LANG],
        "jobs": [bullets.job(bullets.JOB_A, [bullets.K["support"], bullets.K["endpoint"]]),
                 bullets.job(bullets.JOB_D, [bullets.N["support"]])],
        "projects": {"heading": bullets.PROJECTS, "bullets": [bullets.P["jobs"]]},
        "education": bullets.EDU, "certs": bullets.CERTS,
    }


class ResumeBulletsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._path = mock.patch.object(sys, "path", [str(SPEC_DIR)] + sys.path)
        cls._path.start()
        cls._mods = mock.patch.dict(sys.modules, _stub_missing_builder_deps())
        cls._mods.start()
        cls.bullets = importlib.import_module("bullets")
        cls.spec_names = sorted(p.stem for p in SPEC_DIR.glob("spec*.py"))
        cls.modules = {n: importlib.import_module(n) for n in cls.spec_names}

    @classmethod
    def tearDownClass(cls):
        for name in list(cls.modules) + ["bullets", "build_resume", "build_resume_federal"]:
            sys.modules.pop(name, None)
        cls._mods.stop()
        cls._path.stop()

    def test_bullets_is_pure_data_without_builder_imports(self):
        tree = ast.parse((SPEC_DIR / "bullets.py").read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(imported - {"__future__"}, set(), "bullets.py must not import the builder or anything else")

    def test_every_spec_file_imports_and_the_core_names_exist(self):
        self.assertIn("spec_TEMPLATE", self.modules)
        for name in CORE:
            self.assertTrue(hasattr(self.bullets, name), f"bullets.py lost {name}")

    def test_federal_headers_never_travel_under_standard_names(self):
        for name in STANDARD_HEADERS:
            self.assertIsNot(getattr(self.bullets, "FED_" + name), getattr(self.bullets, name))

    def test_template_and_skill_point_at_bullets_py(self):
        template = (SPEC_DIR / "spec_TEMPLATE.py").read_text(encoding="utf-8")
        self.assertIn("from bullets import CERTS", template)
        self.assertNotIn("from specs_", template)
        skill = (SPEC_DIR.parent.parent / ".claude" / "skills" / "build-tailored-resume" / "SKILL.md")
        self.assertIn("bullets.py", skill.read_text(encoding="utf-8"))


class ContactLineTests(unittest.TestCase):
    """An onsite or hybrid role outside the home region gets a relocation line for that move
    ("Anytown, ST (relocating to Minneapolis)", one line); a remote or home-region role keeps the plain home city.
    The spec carries the line; the default never changes."""
    @classmethod
    def setUpClass(cls):
        cls._path = mock.patch.object(sys, "path", [str(SPEC_DIR)] + sys.path)
        cls._path.start()
        cls._mods = mock.patch.dict(sys.modules, _stub_missing_builder_deps())
        cls._mods.start()
        cls.br = importlib.import_module("build_resume")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("build_resume", None)
        cls._mods.stop()
        cls._path.stop()

    def spec(self, **over):
        base = {"slug": "x", "summary": "IT Support Specialist.", "skills": [("Endpoint", "Intune")],
                "jobs": [{"title": "IT Support Engineer", "dates": "01/2023 - Present", "company": "Example SaaS Co.",
                          "bullets": ["Supported 500+ employees."]}],
                "education": ["B.S."], "certs": []}
        base.update(over)
        return base

    def test_relocating_contact_names_the_metro_and_home(self):
        line = self.br.relocating_contact("Minneapolis")
        self.assertEqual(line, "Anytown, ST (relocating to Minneapolis)  |  "
                               "555-010-0100  |  you@example.com  |  linkedin.com/in/your-profile")
        self.assertIsNone(self.br.FORBIDDEN.search(line))

    def test_default_spec_keeps_the_home_line(self):
        html = self.br.build_html(self.spec())
        self.assertIn("Anytown, ST", html)
        self.assertNotIn("relocating", html)

    def test_a_spec_contact_overrides_the_header_in_html(self):
        spec = self.spec(contact=self.br.relocating_contact("Minneapolis"))
        html = self.br.build_html(spec)
        self.assertIn("Anytown, ST (relocating to Minneapolis)", html)
        self.assertNotIn(">Anytown, ST  |", html)  # the bare home header is gone

    def test_check_text_covers_the_contact_line(self):
        spec = self.spec(contact="Anytown, ST (relocating to Minneapolis)  |  555-010-0100")
        self.br.check_text(spec)  # clean line passes
        bad = self.spec(contact="Minneapolis, MN — relocating")
        with self.assertRaises(ValueError):
            self.br.check_text(bad)

    def test_a_custom_contact_must_carry_the_relocation_note(self):
        # A bare "Minneapolis, MN" header would claim the candidate already lives there.
        for line in ("Minneapolis, MN  |  555-010-0100", "Minneapolis, MN (relocating)  |  555-010-0100"):
            with self.assertRaises(ValueError, msg=line):
                self.br.check_text(self.spec(contact=line))

    def test_a_long_metro_falls_back_to_the_bare_note(self):
        # "(relocating to Minneapolis, MN)" wrapped the header onto two lines; longer names would too.
        line = self.br.relocating_contact("San Francisco")
        self.assertTrue(line.startswith("Anytown, ST (relocating)  |  "), line)
        with self.assertRaises(ValueError):
            self.br.relocating_contact("Minneapolis, MN")

    def test_the_tailoring_skill_teaches_the_location_rule(self):
        # The builder fix is invisible unless the build-tailored-resume skill names it.
        repo = SPEC_DIR.parent.parent
        skill = (repo / ".claude" / "skills" / "build-tailored-resume" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("relocating_contact", skill)
        self.assertIn("Anytown, ST (relocating to Minneapolis)", skill)


class RealBuildTests(unittest.TestCase):
    """One spec is built into a DOCX with the real python-docx, from your base resume, into a temp folder.

    Skips until you put your own base resume at resumes/base_resume.docx. No Chrome and no PDF here; the DOCX is
    the part of the build that breaks silently when a spec drifts, and it is reopened and read back.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import docx  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("python-docx missing: pip install -r scripts\\automation\\requirements.txt")
        cls._path = mock.patch.object(sys, "path", [str(SPEC_DIR)] + sys.path)
        cls._path.start()
        cls.br = importlib.import_module("build_resume")
        cls.spec = example_spec(importlib.import_module("bullets"))
        if not cls.br.BASE.exists():
            cls._path.stop()
            raise unittest.SkipTest(f"base resume not found: {cls.br.BASE}")

    @classmethod
    def tearDownClass(cls):
        for name, module in list(sys.modules.items()):
            if str(getattr(module, "__file__", "") or "").startswith(str(SPEC_DIR)):
                sys.modules.pop(name, None)
        cls._path.stop()

    def test_a_spec_builds_a_docx_from_the_base_and_reads_back_its_own_text(self):
        import docx
        self.br.check_text(self.spec)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "spec.docx"
            self.br.build_docx(self.spec, out)
            self.assertGreater(out.stat().st_size, 10_000)
            paragraphs = [p.text for p in docx.Document(str(out)).paragraphs]
        text = "\n".join(paragraphs)
        self.assertIn(self.br.NAME, text)
        self.assertIn(self.spec["summary"], paragraphs)
        for job in self.spec["jobs"]:
            self.assertTrue(any(job["title"] in p and job["dates"] in p for p in paragraphs), job["title"])
            for bullet in job["bullets"]:
                self.assertIn(bullet, text)
        for line in self.spec["education"] + self.spec["certs"]:
            self.assertIn(line, text)
        self.assertIsNone(self.br.FORBIDDEN.search(text), "em dash, en dash or tilde reached the resume")
        html = self.br.build_html(self.spec)
        self.assertIn(self.spec["jobs"][0]["title"], html)
        self.assertIn("</html>", html)


if __name__ == "__main__":
    unittest.main()
