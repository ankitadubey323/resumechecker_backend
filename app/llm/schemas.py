"""The exact structure the LLM must return (enforced with Groq's JSON-schema mode).

Field descriptions are sent to the model as part of the schema, so they double as
instructions. Keep them short and specific.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Contact(BaseModel):
    email: str = Field(description="Exactly as given.")
    phone: str = Field(description="Exactly as given.")
    location: str | None = Field(default=None, description="Only if present in the candidate data.")
    links: list[str] = Field(default=[], description="LinkedIn, GitHub, portfolio URLs found in the candidate data.")


class SkillGroup(BaseModel):
    category: str = Field(description="e.g. Languages, Frameworks, Databases, Cloud & DevOps, Tools.")
    items: list[str] = Field(description="Most job-relevant first. Only skills the candidate has.")


class ExperienceEntry(BaseModel):
    title: str = Field(description="Job title exactly as given.")
    company: str = Field(description="Company exactly as given.")
    location: str | None = None
    start: str = Field(description="As written in the candidate data, e.g. 'Jan 2022' or '2022-01'.")
    end: str = Field(description="As written, or 'Present' for a current role.")
    bullets: list[str] = Field(description="2-6 tailored bullets, job-relevant first.")


class ProjectEntry(BaseModel):
    name: str
    tech: list[str] = Field(default=[], description="Technologies used, job-relevant first.")
    link: str | None = None
    bullets: list[str] = Field(description="1-3 tailored bullets.")


class EducationEntry(BaseModel):
    degree: str
    institution: str
    year: str | None = None
    details: str | None = Field(default=None, description="GPA, honours or relevant coursework, if given.")


class TailoredResume(BaseModel):
    name: str = Field(description="Candidate's full name exactly as given.")
    headline: str = Field(description="Target role plus 2-4 core relevant skills, e.g. 'Backend Engineer | Python, FastAPI, PostgreSQL'.")
    contact: Contact
    summary: str = Field(description="2-4 sentences tailored to the job. No 'I', no clichés.")
    skills: list[SkillGroup] = Field(description="2-5 groups, most job-relevant group first.")
    experience: list[ExperienceEntry] = Field(default=[], description="Most recent first. Empty if the candidate has none.")
    projects: list[ProjectEntry] = Field(default=[])
    education: list[EducationEntry] = Field(default=[])
    certifications: list[str] = Field(default=[], description="Certifications, awards, publications, exactly as given.")


# Free-text labels (not enums): with Groq's strict mode a single unexpected label such as
# "certifications" makes the whole response fail validation.


class BulletChange(BaseModel):
    section: str = Field(description="summary, experience, projects or skills.")
    original: str = Field(description="Verbatim from the candidate data.")
    improved: str = Field(description="The rewritten version used in the tailored resume.")
    reason: str = Field(description="One sentence: why this is better for this job.")


class Suggestion(BaseModel):
    area: str = Field(description="One word: summary, skills, experience, projects, education, keywords, formatting or other.")
    priority: Literal["high", "medium", "low"]
    suggestion: str = Field(description="Specific and actionable; something the candidate can add or change.")


class Analysis(BaseModel):
    jd_requirements: list[str] = Field(description="The 5-10 most important requirements of the job, short phrases.")
    matched_keywords: list[str] = Field(description="Important job keywords/skills the candidate truthfully has.")
    missing_keywords: list[str] = Field(description="Important job keywords/skills NOT supported by the candidate data.")
    bullet_changes: list[BulletChange] = Field(description="Up to 8 of the most meaningful rewrites.")
    suggestions: list[Suggestion] = Field(description="4-8 ways the candidate could strengthen this resume for this job.")
    # float, not int: a model writing 55.0 must not fail the whole response. Rounded in post-checks.
    match_score_before: float = Field(description="Fit of the ORIGINAL resume to the job, 0-100.")
    match_score_after: float = Field(description="Fit of the TAILORED resume to the job, 0-100. Realistic: rewriting can't add missing skills.")
    score_explanation: str = Field(description="One or two sentences explaining both scores.")


class CustomizationResult(BaseModel):
    resume: TailoredResume
    analysis: Analysis
