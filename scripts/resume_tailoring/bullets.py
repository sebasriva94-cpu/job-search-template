"""The verified resume bullet library: the one place every shared bullet, role header and skill string lives.

EXAMPLE DATA. Everything below describes a fictional candidate ("Alex Example") so the builder and tests run out
of the box. Replace it with your own verified facts: every bullet should trace to your base resume or
candidate-profile.md, with no invented tools, metrics, dates or titles.

Spec files (spec_*.py) import from here. Pure data: no builder import, so the unit tests can load it without
python-docx.
"""
from __future__ import annotations

# ===== core library ==============================================================================
EDU = [
    "Example State University - B.S. Information Systems, 2016",
]
CERTS = [
    "CompTIA A+ (example)",
    "CompTIA Security+ (in progress)",
]
JOB_A = dict(title="Desktop Support Technician", dates="Mar 2022 - Present", company="Example Retail Co., Anytown, ST")
JOB_B = dict(title="Service Desk Analyst", dates="Jun 2020 - Feb 2022", company="Example Bank, Anytown, ST")
JOB_C = dict(title="Systems Administrator (Contract)", dates="Jan 2019 - May 2020", company="Example Manufacturing Co., Anytown, ST")
JOB_D = dict(title="Help Desk Technician", dates="Aug 2016 - Dec 2018", company="Example School District, Anytown, ST")
LANG = ("Languages", "English (native)")

# ---- verified bullet library (fictional examples) ------------------------------------------------
K = {  # JOB_A
    "support": "Delivered Tier 2 support to 400+ store and office staff at 50+ tickets/week, holding a 92% SLA.",
    "endpoint": "Provisioned and managed Windows and macOS laptops with Microsoft Intune and Jamf Pro through the full lifecycle.",
    "identity": "Administered Okta SSO and MFA, Microsoft 365, and Google Workspace accounts and groups with least-privilege access.",
    "automation": "Automated new-hire account setup with a scripted workflow, cutting setup time from a day to about two hours.",
}
T = {  # JOB_B
    "support": "Supported 250+ branch staff on Windows through ServiceNow at 40+ tickets/week.",
    "guides": "Wrote step-by-step user guides that improved first-contact resolution.",
}
C = {  # JOB_C
    "identity": "Administered Active Directory and Microsoft 365 accounts, MFA, and VPN access for plant and office staff.",
    "troubleshoot": "Troubleshot authentication, connectivity, and application issues and kept support documentation current.",
}
N = {  # JOB_D
    "support": "Provided Tier 1/2 support for 300+ Windows endpoints and classroom devices across six schools.",
    "accounts": "Handled onboarding and offboarding in Active Directory and Microsoft 365 with audit-ready records.",
}
P = {
    "jobs": ("Built a scheduled Python job-search pipeline (multi-source sweeps, an LLM scoring judge calibrated against "
             "hand labels, and an HTML dashboard) run nightly by GitHub Actions, with a unit-tested codebase."),
}
PROJECTS = "AUTOMATION & AI TOOLING (Independent Projects)"


def job(meta: dict, bullets: list[str]) -> dict:
    return {**meta, "bullets": bullets}


# ===== federal (USAJOBS) role headers: the FED_ prefix is the only name these carry, in every spec file =====
FED_EDU = [
    "B.S. Information Systems, Example State University - 2016",
]
FED_CERTS = list(CERTS)
FED_JOB_A = dict(title="Desktop Support Technician", company="Example Retail Co., Anytown, ST, United States",
                 start="03/01/2022", end="Present", hours=40)
FED_JOB_B = dict(title="Service Desk Analyst", company="Example Bank, Anytown, ST, United States",
                 start="06/01/2020", end="02/28/2022", hours=40)
FED_JOB_C = dict(title="Systems Administrator (Contract)", company="Example Manufacturing Co., Anytown, ST, United States",
                 start="01/01/2019", end="05/31/2020", hours=40)
FED_JOB_D = dict(title="Help Desk Technician", company="Example School District, Anytown, ST, United States",
                 start="08/01/2016", end="12/31/2018", hours=40)
