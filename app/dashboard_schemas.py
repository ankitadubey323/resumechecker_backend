"""Request/response models for the dashboard. Keep rules in sync with frontend/src/lib/profile.ts."""

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.schemas import UserOut

MIN_SKILLS = 3
JD_MIN_CHARS = 100
JD_MAX_CHARS = 20_000

Text = Annotated[str, StringConstraints(strip_whitespace=True)]
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_month(v: str | None) -> str | None:
    if v is None:
        return v
    if not MONTH_RE.fullmatch(v):
        raise ValueError("Use the format YYYY-MM")
    if not 1950 <= int(v[:4]) or v > date.today().strftime("%Y-%m"):
        raise ValueError("Date can't be in the future or before 1950")
    return v


class WorkExperience(_Strict):
    company: Text = Field(min_length=1, max_length=100)
    role: Text = Field(min_length=1, max_length=100)
    start: str
    end: str | None = None  # None = current job
    description: Text = Field(default="", max_length=1500)

    @field_validator("start", "end")
    @classmethod
    def _months(cls, v: str | None) -> str | None:
        return _check_month(v)

    @model_validator(mode="after")
    def _order(self) -> "WorkExperience":
        if self.end and self.end < self.start:
            raise ValueError("End date must be after the start date")
        return self


class Project(_Strict):
    title: Text = Field(min_length=1, max_length=100)
    description: Text = Field(default="", max_length=1500)
    tech: list[Annotated[Text, StringConstraints(min_length=1, max_length=40)]] = Field(default=[], max_length=20)
    link: AnyHttpUrl | None = None

    @field_validator("link", mode="before")
    @classmethod
    def _empty_link(cls, v: object) -> object:
        return None if v == "" else v


class Education(_Strict):
    degree: Text = Field(min_length=1, max_length=100)
    institution: Text = Field(min_length=1, max_length=150)
    year: int | None = Field(default=None, ge=1950, le=date.today().year + 6)


class ProfileDetails(_Strict):
    # Field order matters: later validators read earlier values from info.data.
    experience_level: Literal["fresher", "experienced"]
    headline: Text = Field(min_length=2, max_length=120)
    years_experience: float = Field(ge=0, le=50)
    skills: list[str] = Field(max_length=50)
    experiences: list[WorkExperience] = Field(default=[], max_length=20)
    projects: list[Project] = Field(max_length=20)
    education: list[Education] = Field(default=[], max_length=10)

    @field_validator("years_experience")
    @classmethod
    def _years(cls, v: float, info: ValidationInfo) -> float:
        level = info.data.get("experience_level")
        if level == "fresher":
            return 0
        if level == "experienced" and v <= 0:
            raise ValueError("Enter your total years of experience")
        return round(v, 1)

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for s in v:
            s = " ".join(s.split())
            if not 1 <= len(s) <= 40:
                raise ValueError("Each skill must be 1-40 characters")
            seen.setdefault(s.lower(), s)
        if len(seen) < MIN_SKILLS:
            raise ValueError(f"Add at least {MIN_SKILLS} skills")
        return list(seen.values())

    @field_validator("experiences")
    @classmethod
    def _experiences(cls, v: list[WorkExperience], info: ValidationInfo) -> list[WorkExperience]:
        if info.data.get("experience_level") == "experienced" and not v:
            raise ValueError("Add at least one work experience")
        return v

    @field_validator("projects")
    @classmethod
    def _projects(cls, v: list[Project]) -> list[Project]:
        if not v:
            raise ValueError("Add at least one project")
        return v

    @field_validator("education")
    @classmethod
    def _education(cls, v: list[Education], info: ValidationInfo) -> list[Education]:
        if info.data.get("experience_level") == "fresher" and not v:
            raise ValueError("Add your education")
        return v


class JobDescriptionIn(_Strict):
    text: Text = Field(max_length=JD_MAX_CHARS)

    @field_validator("text")
    @classmethod
    def _length(cls, v: str) -> str:
        if not v:
            raise ValueError("Paste the job description")
        if len(v) < JD_MIN_CHARS:
            raise ValueError(f"The job description looks too short (minimum {JD_MIN_CHARS} characters)")
        return v


class ResumeOut(BaseModel):
    filename: str
    size: int
    page_count: int | None
    word_count: int
    preview: str
    uploaded_at: datetime


class JobDescriptionOut(BaseModel):
    id: str
    text: str  # full text, so the user can see and edit the saved job description
    preview: str
    length: int
    created_at: datetime


class Completion(BaseModel):
    percent: int
    missing: list[str]


class CustomizationSummary(BaseModel):
    id: str
    created_at: datetime
    outdated: bool


class DashboardOut(BaseModel):
    user: UserOut
    details: ProfileDetails | None
    details_updated_at: datetime | None
    photo_version: int | None  # changes when the photo changes (cache-busting); None = no photo
    resume: ResumeOut | None
    latest_job_description: JobDescriptionOut | None
    completion: Completion
    ready_for_job_description: bool
    ready_for_customization: bool  # step 3 unlocks: profile/resume done AND a job description exists
    customization_input: Literal["resume", "details"] | None  # what the AI will use (most recent input)
    latest_customization: CustomizationSummary | None
