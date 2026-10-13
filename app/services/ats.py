"""Rule-based ATS-friendliness score for a tailored resume (no LLM, fully deterministic).

Weights (total 100):
  40  job keywords present in the resume text
  20  essential sections: contact, summary, skills, experience or projects, education
  15  bullets that start with an action verb
  10  bullets of a scannable length (6-35 words)
  10  bullets with a concrete number (target: 30% of bullets)
   5  no unfilled "[X]" placeholders
"""

import re

from pydantic import BaseModel

from app.llm.schemas import Analysis, TailoredResume

ACTION_VERBS = {
    "achieved", "added", "analyzed", "architected", "automated", "built", "collaborated", "conducted",
    "configured", "contributed", "created", "cut", "debugged", "decreased", "defined", "delivered",
    "deployed", "designed", "developed", "drove", "enabled", "engineered", "enhanced", "established",
    "executed", "fixed", "generated", "handled", "implemented", "improved", "increased", "integrated",
    "introduced", "launched", "led", "maintained", "managed", "mentored", "migrated", "modernized",
    "monitored", "optimized", "organized", "owned", "participated", "performed", "planned", "presented",
    "produced", "reduced", "refactored", "resolved", "reviewed", "scaled", "secured", "set", "shipped",
    "simplified", "standardized", "streamlined", "supported", "tested", "trained", "troubleshot",
    "upgraded", "used", "wrote",
}  # fmt: skip


class AtsCheck(BaseModel):
    label: str
    passed: bool
    detail: str
    points: float
    max_points: float


class AtsReport(BaseModel):
    score: int
    checks: list[AtsCheck]


def resume_text(r: TailoredResume) -> str:
    parts = [r.headline, r.summary]
    parts += [", ".join(g.items) for g in r.skills]
    parts += [f"{e.title} {e.company} " + " ".join(e.bullets) for e in r.experience]
    parts += [f"{p.name} {' '.join(p.tech)} " + " ".join(p.bullets) for p in r.projects]
    parts += [f"{e.degree} {e.institution}" for e in r.education] + r.certifications
    return "\n".join(parts)


def _is_action(bullet: str) -> bool:
    word = re.sub(r"[^a-z]", "", (bullet.split() or [""])[0].lower())
    return word in ACTION_VERBS or (word.endswith("ed") and len(word) > 4) or word.rstrip("s") + "ed" in ACTION_VERBS


def ats_report(resume: TailoredResume, analysis: Analysis) -> AtsReport:
    text = resume_text(resume).lower()
    bullets = [b for e in resume.experience for b in e.bullets] + [b for p in resume.projects for b in p.bullets]
    checks: list[AtsCheck] = []

    def add(label: str, ratio: float, max_points: float, detail: str, threshold: float = 0.7) -> None:
        ratio = max(0.0, min(1.0, ratio))
        checks.append(AtsCheck(label=label, passed=ratio >= threshold, detail=detail, points=round(ratio * max_points, 1), max_points=max_points))

    keywords = list(dict.fromkeys(k.strip() for k in analysis.matched_keywords + analysis.missing_keywords if k.strip()))
    found = [k for k in keywords if k.lower() in text]
    add("Job keywords", len(found) / len(keywords) if keywords else 1, 40,
        f"{len(found)} of {len(keywords)} important job keywords appear in your resume.", threshold=0.6)

    sections = {
        "contact details": bool(resume.contact.email and resume.contact.phone),
        "summary": bool(resume.summary.strip()),
        "skills": bool(resume.skills),
        "experience or projects": bool(resume.experience or resume.projects),
        "education": bool(resume.education),
    }
    missing = [name for name, ok in sections.items() if not ok]
    add("Essential sections", sum(sections.values()) / len(sections), 20,
        "All essential sections are present." if not missing else f"Missing: {', '.join(missing)}.", threshold=1)

    if bullets:
        action = sum(_is_action(b) for b in bullets)
        add("Action verbs", action / len(bullets), 15, f"{action} of {len(bullets)} bullets start with an action verb.")
        good_len = sum(6 <= len(b.split()) <= 35 for b in bullets)
        add("Bullet length", good_len / len(bullets), 10, f"{good_len} of {len(bullets)} bullets are 6-35 words (easy to scan).")
        numbers = sum(bool(re.search(r"\d", re.sub(r"\[[^\]]*\]", "", b))) for b in bullets)
        add("Quantified results", (numbers / len(bullets)) / 0.3, 10,
            f"{numbers} of {len(bullets)} bullets include a number. Aim for at least 30%.", threshold=1)
    else:
        for label, pts in (("Action verbs", 15), ("Bullet length", 10), ("Quantified results", 10)):
            add(label, 0, pts, "No experience or project bullets yet.")

    placeholders = len(re.findall(r"\[[^\]]{1,12}\]", resume_text(resume)))
    add("No placeholders", 0 if placeholders else 1, 5,
        "No unfilled placeholders." if not placeholders else f"{placeholders} [X] placeholder(s) still need real values.", threshold=1)

    return AtsReport(score=round(sum(c.points for c in checks)), checks=checks)
