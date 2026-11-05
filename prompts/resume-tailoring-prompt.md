# Resume tailoring prompt — readable, non-AI format

Use this prompt whenever tailoring the candidate's resume for a specific role.

---

You are editing an existing resume, not writing a new one.

Goal: produce a clear, conservative, ATS-readable resume that sounds like a real IT support professional. Do not make it look like AI wrote it. Do not over-format it. Do not turn it into marketing copy.

Inputs:
1. Base resume / candidate profile
2. Job description
3. Any verified company notes

Rules:

## 1. Preserve the resume structure
Keep the same basic order:
- Name and contact
- Target title line
- Professional summary
- Core skills
- Professional experience
- Education and certifications

Do not create a new visual design unless asked. No icons, no colored boxes, no columns that break ATS parsing, no decorative headers.

## 2. Keep the summary short
Maximum 3 lines.
Plain language only.
No phrases like:
- dynamic IT professional
- proven track record
- leveraging technology
- fast-paced environment
- passionate about
- results-driven
- spearheaded unless the original resume already uses it naturally

Good summary style:
"IT Support Engineer with <N>+ years supporting corporate, education, and healthcare environments. Strong in endpoint management, identity/access, ITSM, and conference room support across Windows and macOS. Known for calm support, clean documentation, and high-volume ticket ownership."

## 2a. Location line
The resume header's city line follows the role's work model:
- Remote role, or onsite/hybrid in the home region: "Anytown, ST".
- Onsite or hybrid in another metro (Greater Minneapolis is a target area): the owner's city first with the move,
  e.g. "Anytown, ST (relocating to Minneapolis)" (it fits on one line).
Never claim the owner already lives in the target metro, and never leave the home-city line on a resume for an
onsite role elsewhere. In the builder this is the spec's "contact" field (br.relocating_contact); in a
Markdown draft, write the same line under the owner's name. The honesty check must say when a relocation line
was used.

## 3. Tailor by selecting and reordering, not inventing
Use the job description to decide which existing facts to emphasize.
You may:
- move stronger bullets higher
- swap in role-relevant tools from the candidate profile
- tighten wording
- remove irrelevant bullets
- add a skill only if it already appears in the candidate profile or source resume

You may not:
- invent tools, certifications, projects, metrics, titles, employers, dates, or clearance
- inflate help desk work into engineering work
- claim ownership of systems the owner only supported
- add fake leadership language

## 4. Bullet style
Each job should have 3–5 bullets.
Each bullet should be one line when possible.
Use simple verbs:
- Supported
- Managed
- Administered
- Troubleshot
- Maintained
- Documented
- Coordinated
- Improved
- Built

Avoid AI verbs:
- spearheaded
- orchestrated
- leveraged
- optimized unless there is a specific metric
- enhanced
- facilitated
- drove transformation
- streamlined unless tied to a concrete workflow

Good bullet:
"Administered Windows and macOS endpoint provisioning with Intune and Jamf Pro."

Bad bullet:
"Leveraged cutting-edge endpoint management platforms to streamline cross-functional device lifecycle operations."

## 5. Metrics: use only real ones
Allowed known metrics (EXAMPLE list, replace with the verified numbers from your own resume):
- <N>+ years IT support experience
- <N>+ employees supported at Employer A
- <N>+ tickets/week at Employer A
- <N>% SLA compliance where already stated

Do not create new percentages.
Do not overuse metrics. One strong metric per role is enough.

## 6. Skills section
Keep it readable. Group by category.
Do not dump every keyword from the job description.
Use this style:

Endpoint / MDM: Microsoft Intune, Jamf Pro, SCCM, Windows, macOS
Identity / Access: Okta, Entra ID, SSO/MFA, RBAC, least privilege
ITSM / Documentation: ServiceNow, Freshservice, Jira, Confluence
Security / Network: CrowdStrike Falcon, Cloudflare WARP, DNS, TCP/IP, Wireshark
Collaboration / AV: Zoom Rooms, Teams, Google Meet, Logitech, Cisco
Scripting / APIs: PowerShell, Bash, REST/JSON, Postman, curl

## 7. Formatting output
Return clean Markdown first, not a PDF.
Use plain headings and bullets.
No tables.
No emojis.
No decorative separators except simple section headings.
No bold on every bullet.
No dense paragraphs.

After the Markdown resume, include:

## What changed
- 3–5 bullets explaining what was tailored

## Honesty check
- Mention any job requirements not strongly covered
- Mention any salary/location/application caveats

## Final self-check
Before finalizing, verify:
- Does it fit on about 1–2 pages when pasted into Word?
- Does it sound like the candidate, not a generic AI candidate?
- Are there any invented claims?
- Are the strongest role-matching facts in the top third?
- Can a recruiter understand it in 20 seconds?

If the answer to any item is no, revise once before returning the final resume.

## 8. Persisting the approved resume
The job-agent app was retired on 2024-12-17; nothing is saved to its database any more. After the candidate
approves the draft in chat, never before, the `build-tailored-resume` skill writes the DOCX and PDF to
`resumes\tailored\`. The application record lives in `pipeline.md` (narrative) and in
`dashboard\job-dashboard-data.json` (`applied_on` on the role), as the `show-my-jobs` skill describes.
