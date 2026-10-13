from typing import Literal

from bson import Binary
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from pymongo.database import Database

from app.dashboard_schemas import DashboardOut, JobDescriptionIn, ProfileDetails
from app.database import get_db, utcnow
from app.dependencies import get_current_user
from app.rate_limit import limiter
from app.services.dashboard import build_dashboard, readiness
from app.services.images import MAX_PHOTO_BYTES, PhotoError, process_avatar
from app.services.resume_parser import MAX_RESUME_BYTES, ResumeParseError, parse_resume

router = APIRouter(prefix="/api", tags=["dashboard"])


def _read_capped(upload: UploadFile, limit: int, too_big: str) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, too_big)
    return data


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(user: dict = Depends(get_current_user), db: Database = Depends(get_db)) -> DashboardOut:
    return build_dashboard(db, user)


@router.put("/profile/details", response_model=DashboardOut)
def save_details(
    body: ProfileDetails, user: dict = Depends(get_current_user), db: Database = Depends(get_db)
) -> DashboardOut:
    db.profiles.update_one(
        {"_id": user["_id"]},
        # Saving details also selects them as the input for customization.
        {"$set": {**body.model_dump(mode="json"), "details_updated_at": utcnow(), "selected_input": "details"}},
        upsert=True,
    )
    return build_dashboard(db, user)


@router.post("/profile/photo", response_model=DashboardOut)
@limiter.limit("20/hour")
def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> DashboardOut:
    data = _read_capped(file, MAX_PHOTO_BYTES, "Photos must be 2 MB or smaller.")
    try:
        webp = process_avatar(data)
    except PhotoError as e:
        raise HTTPException(e.status_code, str(e))
    db.profiles.update_one(
        {"_id": user["_id"]}, {"$set": {"photo": Binary(webp), "photo_updated_at": utcnow()}}, upsert=True
    )
    return build_dashboard(db, user)


@router.get("/profile/photo")
def get_photo(user: dict = Depends(get_current_user), db: Database = Depends(get_db)) -> Response:
    profile = db.profiles.find_one({"_id": user["_id"]}, {"photo": 1})
    if not profile or not profile.get("photo"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No photo")
    # private: only this user's browser may cache it; the URL carries ?v=<version>.
    return Response(bytes(profile["photo"]), media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/profile/photo", response_model=DashboardOut)
def delete_photo(user: dict = Depends(get_current_user), db: Database = Depends(get_db)) -> DashboardOut:
    db.profiles.update_one({"_id": user["_id"]}, {"$unset": {"photo": "", "photo_updated_at": ""}})
    return build_dashboard(db, user)


@router.post("/resume", response_model=DashboardOut)
@limiter.limit("20/hour")
def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> DashboardOut:
    data = _read_capped(file, MAX_RESUME_BYTES, "The file is larger than 5 MB.")
    filename = (file.filename or "resume").replace("\\", "/").rsplit("/", 1)[-1][:255]
    try:
        parsed = parse_resume(filename, data)
    except ResumeParseError as e:
        raise HTTPException(e.status_code, str(e))

    db.resumes.replace_one(
        {"_id": user["_id"]},
        {
            "filename": filename,
            "content_type": parsed.content_type,
            "size": len(data),
            "page_count": parsed.page_count,
            "data": Binary(data),
            "text": parsed.text,
            "uploaded_at": utcnow(),
        },
        upsert=True,
    )
    # Uploading a resume also selects it as the input for customization.
    db.profiles.update_one({"_id": user["_id"]}, {"$set": {"selected_input": "resume"}}, upsert=True)
    return build_dashboard(db, user)


class InputSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Literal["resume", "details"]


@router.put("/profile/input", response_model=DashboardOut)
def select_input(
    body: InputSelection, user: dict = Depends(get_current_user), db: Database = Depends(get_db)
) -> DashboardOut:
    """Choose which input (uploaded resume or entered details) the AI customizes from."""
    uid = user["_id"]
    exists = (
        db.resumes.find_one({"_id": uid}, {"_id": 1})
        if body.input == "resume"
        else db.profiles.find_one({"_id": uid, "details_updated_at": {"$ne": None}}, {"_id": 1})
    )
    if not exists:
        what = "Upload a resume" if body.input == "resume" else "Save your details"
        raise HTTPException(status.HTTP_409_CONFLICT, f"{what} first.")
    db.profiles.update_one({"_id": uid}, {"$set": {"selected_input": body.input}}, upsert=True)
    return build_dashboard(db, user)


@router.delete("/resume", response_model=DashboardOut)
def delete_resume(user: dict = Depends(get_current_user), db: Database = Depends(get_db)) -> DashboardOut:
    db.resumes.delete_one({"_id": user["_id"]})
    return build_dashboard(db, user)


@router.post("/job-descriptions", response_model=DashboardOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("60/hour")
def submit_job_description(
    request: Request,
    body: JobDescriptionIn,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> DashboardOut:
    if not readiness(db, user["_id"]):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Upload your resume or fill in your details before adding a job description."
        )
    db.job_descriptions.insert_one({"user_id": user["_id"], "text": body.text, "created_at": utcnow()})
    return build_dashboard(db, user)
