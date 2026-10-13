from bson import ObjectId
from pymongo.database import Database

from app.dashboard_schemas import (
    Completion,
    CustomizationSummary,
    DashboardOut,
    JobDescriptionOut,
    ProfileDetails,
    ResumeOut,
)
from app.schemas import UserOut

PREVIEW_CHARS = 1500
DETAIL_FIELDS = list(ProfileDetails.model_fields)

# Never load the binary photo/file when building the dashboard.
_PROFILE_PROJECTION = {"photo": 0}
_RESUME_PROJECTION = {"data": 0}


def is_ready(profile: dict | None, resume: dict | None) -> bool:
    """Step 2 (job description) unlocks once the user has a resume OR saved details.
    Details are only saved when they pass ProfileDetails validation, so their presence is enough."""
    return resume is not None or bool(profile and profile.get("details_updated_at"))


def readiness(db: Database, user_id: ObjectId) -> bool:
    profile = db.profiles.find_one({"_id": user_id}, {"details_updated_at": 1})
    resume = db.resumes.find_one({"_id": user_id}, {"_id": 1})
    return is_ready(profile, resume)


def input_source(profile: dict | None, resume: dict | None) -> str | None:
    """Which input the AI uses: the one the user selected in step 1 ("resume" or "details").

    The user selects it by choosing the step 1 tab; uploading a resume or saving details also
    selects it. If the selected input no longer exists, fall back to whichever one does.
    Returns None when there is neither.
    """
    has_details = bool(profile and profile.get("details_updated_at"))
    has_resume = resume is not None
    selected = (profile or {}).get("selected_input")
    if selected == "details" and has_details:
        return "details"
    if selected == "resume" and has_resume:
        return "resume"
    return "resume" if has_resume else "details" if has_details else None


def is_outdated(db: Database, user_id: ObjectId, customization: dict) -> bool:
    """True when the job description, or the input this customization was made from, changed since."""
    created = customization["created_at"]
    latest_jd = db.job_descriptions.find_one({"user_id": user_id}, {"_id": 1}, sort=[("created_at", -1), ("_id", -1)])
    if customization.get("source") == "details":
        doc = db.profiles.find_one({"_id": user_id}, {"details_updated_at": 1}) or {}
        changed_at = doc.get("details_updated_at")
    else:
        doc = db.resumes.find_one({"_id": user_id}, {"uploaded_at": 1}) or {}
        changed_at = doc.get("uploaded_at")
    return (latest_jd is not None and latest_jd["_id"] != customization["job_description_id"]) or bool(
        changed_at and changed_at > created
    )


def _completion(profile: dict | None, resume: dict | None) -> Completion:
    p = profile or {}
    items = [
        ("Profile photo", 10, bool(p.get("photo_updated_at"))),
        ("Headline", 10, bool(p.get("headline"))),
        ("Experience level", 10, bool(p.get("experience_level"))),
        ("Skills", 20, len(p.get("skills") or []) >= 3),
        ("Projects", 15, bool(p.get("projects"))),
        ("Work experience or education", 15, bool(p.get("experiences") or p.get("education"))),
        ("Resume", 20, resume is not None),
    ]
    return Completion(
        percent=sum(w for _, w, done in items if done),
        missing=[label for label, _, done in items if not done],
    )


def build_dashboard(db: Database, user: dict) -> DashboardOut:
    uid = user["_id"]
    profile = db.profiles.find_one({"_id": uid}, _PROFILE_PROJECTION)
    resume = db.resumes.find_one({"_id": uid}, _RESUME_PROJECTION)
    jd = db.job_descriptions.find_one({"user_id": uid}, sort=[("created_at", -1), ("_id", -1)])

    details = None
    if profile and profile.get("details_updated_at"):
        details = ProfileDetails.model_validate({f: profile.get(f) for f in DETAIL_FIELDS})

    photo_at = profile.get("photo_updated_at") if profile else None
    return DashboardOut(
        user=UserOut.from_doc(user),
        details=details,
        details_updated_at=profile.get("details_updated_at") if profile else None,
        photo_version=int(photo_at.timestamp() * 1000) if photo_at else None,
        resume=ResumeOut(
            filename=resume["filename"],
            size=resume["size"],
            page_count=resume.get("page_count"),
            word_count=len(resume["text"].split()),
            preview=resume["text"][:PREVIEW_CHARS],
            uploaded_at=resume["uploaded_at"],
        )
        if resume
        else None,
        latest_job_description=JobDescriptionOut(
            id=str(jd["_id"]), text=jd["text"], preview=jd["text"][:300], length=len(jd["text"]), created_at=jd["created_at"]
        )
        if jd
        else None,
        completion=_completion(profile, resume),
        ready_for_job_description=is_ready(profile, resume),
        ready_for_customization=is_ready(profile, resume) and jd is not None,
        customization_input=input_source(profile, resume),
        # Each input keeps its own result: show the one for the selected input.
        latest_customization=_latest_customization(db, uid, input_source(profile, resume)),
    )


def latest_customization_doc(db: Database, uid: ObjectId, source: str | None, projection: dict | None = None):
    """The newest customization made from the given input ("resume" or "details")."""
    if source is None:
        return None
    return db.customizations.find_one({"user_id": uid, "source": source}, projection, sort=[("created_at", -1), ("_id", -1)])


def _latest_customization(db: Database, uid: ObjectId, source: str | None) -> CustomizationSummary | None:
    doc = latest_customization_doc(db, uid, source, {"created_at": 1, "job_description_id": 1, "source": 1})
    if doc is None:
        return None
    return CustomizationSummary(id=str(doc["_id"]), created_at=doc["created_at"], outdated=is_outdated(db, uid, doc))
