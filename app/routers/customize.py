import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pymongo.database import Database

from app.config import get_settings
from app.database import get_db, utcnow
from app.dependencies import get_current_user
from app.llm.customizer import LLMError, build_candidate_text, customize_resume, stream_cover_letter
from app.llm.prompts import SECTIONS
from app.llm.schemas import CustomizationResult
from app.rate_limit import limiter
from app.services.ats import AtsReport, ats_report
from app.services.dashboard import input_source, is_outdated, latest_customization_doc, readiness
from app.services.exporter import (
    COVER_LETTER_EXPORTERS,
    EXPORTERS,
    MEDIA_TYPES,
    cover_letter_filename,
    filename_for,
    to_txt,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/customizations", tags=["customize"])

Section = Literal["summary", "skills", "experience", "projects"]
# Sent at the end of a cover-letter stream if the AI fails mid-way; the client strips it.
STREAM_ERROR_MARKER = "\x1e"


class CustomizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Section-wise customization: only these sections are rewritten; the rest are kept as given.
    sections: list[Section] = Field(default=list(SECTIONS), min_length=1)


class CoverLetterOut(BaseModel):
    text: str
    created_at: str


class CustomizationOut(BaseModel):
    id: str
    created_at: str
    model: str
    source: str  # resume | details: which input the result was based on
    sections: list[str]  # which sections the AI was allowed to rewrite
    job_description_preview: str
    outdated: bool  # inputs changed since this was generated
    warnings: list[str]
    result: CustomizationResult
    ats: AtsReport
    input_text: str | None  # what was sent to the AI (for the diff view); None for older versions
    tailored_text: str  # plain-text rendering of the tailored resume (for the diff view)
    cover_letter: CoverLetterOut | None


class VersionSummary(BaseModel):
    id: str
    created_at: str
    source: str
    sections: list[str]
    match_score_after: float
    ats_score: int
    has_cover_letter: bool


def _owned(db: Database, user_id: ObjectId, customization_id: str, projection: dict | None = None) -> dict:
    doc = (
        db.customizations.find_one({"_id": ObjectId(customization_id), "user_id": user_id}, projection)
        if ObjectId.is_valid(customization_id)
        else None
    )
    if doc is None:  # also covers other users' ids: never reveal they exist
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customization not found")
    return doc


def _out(db: Database, user_id: ObjectId, doc: dict) -> CustomizationOut:
    jd = db.job_descriptions.find_one({"_id": doc["job_description_id"]}, {"text": 1})
    result = CustomizationResult.model_validate(doc["result"])
    letter = doc.get("cover_letter")
    return CustomizationOut(
        id=str(doc["_id"]),
        created_at=doc["created_at"].isoformat(),
        model=doc["model"],
        source=doc["source"],
        sections=doc.get("sections", list(SECTIONS)),
        job_description_preview=(jd["text"][:200] if jd else ""),
        outdated=is_outdated(db, user_id, doc),
        warnings=doc.get("warnings", []),
        result=result,
        ats=ats_report(result.resume, result.analysis),
        input_text=doc.get("input_text"),
        tailored_text=to_txt(result.resume).decode(),
        cover_letter=CoverLetterOut(text=letter["text"], created_at=letter["created_at"].isoformat()) if letter else None,
    )


@router.post("", response_model=CustomizationOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_customization(
    request: Request,
    body: CustomizeRequest | None = None,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CustomizationOut:
    uid = user["_id"]
    sections = list(dict.fromkeys((body or CustomizeRequest()).sections))
    if not readiness(db, uid):
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload your resume or fill in your details first.")
    jd = db.job_descriptions.find_one({"user_id": uid}, sort=[("created_at", -1), ("_id", -1)])
    if jd is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Add the job description first.")

    limit = get_settings().customizations_per_hour
    recent = db.customizations.count_documents({"user_id": uid, "created_at": {"$gt": utcnow() - timedelta(hours=1)}})
    if recent >= limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, f"You can customize up to {limit} times per hour. Please try again later."
        )

    profile = db.profiles.find_one({"_id": uid}, {"photo": 0})
    resume = db.resumes.find_one({"_id": uid}, {"data": 0})
    candidate = build_candidate_text(user, profile, resume)

    try:
        # The LLM call blocks for several seconds; keep it off the event loop.
        output = await run_in_threadpool(customize_resume, candidate, jd["text"], sections)
    except LLMError as e:
        raise HTTPException(e.status_code, str(e))

    doc = {
        "user_id": uid,
        "job_description_id": jd["_id"],
        "source": input_source(profile, resume),
        "sections": sections,
        "input_text": candidate,
        "model": output.model,
        "result": output.result.model_dump(mode="json"),
        "warnings": output.warnings,
        "created_at": utcnow(),
    }
    doc["_id"] = db.customizations.insert_one(doc).inserted_id
    return _out(db, uid, doc)


@router.get("", response_model=list[VersionSummary])
def list_versions(
    source: Literal["resume", "details"] = Query(...),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> list[VersionSummary]:
    """All saved versions for one input, newest first (multiple resume versions)."""
    docs = db.customizations.find(
        {"user_id": user["_id"], "source": source},
        {"input_text": 0, "warnings": 0},
        sort=[("created_at", -1), ("_id", -1)],
        limit=30,
    )
    out = []
    for d in docs:
        result = CustomizationResult.model_validate(d["result"])
        out.append(
            VersionSummary(
                id=str(d["_id"]),
                created_at=d["created_at"].isoformat(),
                source=d["source"],
                sections=d.get("sections", list(SECTIONS)),
                match_score_after=result.analysis.match_score_after,
                ats_score=ats_report(result.resume, result.analysis).score,
                has_cover_letter=bool(d.get("cover_letter")),
            )
        )
    return out


@router.get("/latest", response_model=CustomizationOut)
def latest_customization(
    source: Literal["resume", "details"] | None = Query(None, description="Defaults to the selected input."),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CustomizationOut:
    """The newest result for one input. Each input (resume / details) keeps its own result."""
    uid = user["_id"]
    if source is None:
        source = input_source(db.profiles.find_one({"_id": uid}, {"photo": 0}), db.resumes.find_one({"_id": uid}, {"data": 0}))
    doc = latest_customization_doc(db, uid, source)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No customization yet")
    return _out(db, user["_id"], doc)


@router.get("/{customization_id}", response_model=CustomizationOut)
def get_customization(
    customization_id: str, user: dict = Depends(get_current_user), db: Database = Depends(get_db)
) -> CustomizationOut:
    return _out(db, user["_id"], _owned(db, user["_id"], customization_id))


@router.get("/{customization_id}/download")
def download_customization(
    customization_id: str,
    format: str = Query("pdf", pattern="^(pdf|docx|txt)$"),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> Response:
    resume = CustomizationResult.model_validate(_owned(db, user["_id"], customization_id)["result"]).resume
    return Response(
        EXPORTERS[format](resume),
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename_for(resume, format)}"'},
    )


@router.post("/{customization_id}/cover-letter")
@limiter.limit("10/hour")
def generate_cover_letter(
    request: Request,
    customization_id: str,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> StreamingResponse:
    """Stream a cover letter for this version as plain text, token by token. Saved when complete."""
    doc = _owned(db, user["_id"], customization_id)
    result = CustomizationResult.model_validate(doc["result"])
    jd = db.job_descriptions.find_one({"_id": doc["job_description_id"]}, {"text": 1})
    if jd is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The job description for this version no longer exists.")

    try:
        chunks = stream_cover_letter(to_txt(result.resume).decode(), jd["text"], result.analysis.missing_keywords)
    except LLMError as e:
        raise HTTPException(e.status_code, str(e))

    def body() -> Iterator[str]:
        parts: list[str] = []
        try:
            for chunk in chunks:
                parts.append(chunk)
                yield chunk
        except Exception:  # noqa: BLE001 - the HTTP status is already sent; report in-band
            logger.exception("Cover letter stream failed")
            yield f"{STREAM_ERROR_MARKER}The AI stopped unexpectedly. Please try again."
            return
        text = "".join(parts).strip()
        if text:
            db.customizations.update_one(
                {"_id": doc["_id"]}, {"$set": {"cover_letter": {"text": text, "created_at": utcnow()}}}
            )

    # Stop proxies from buffering, so tokens reach the browser as they're written:
    # no-transform stops gzip compression (which would wait for the whole response, e.g. in the
    # Next.js /api proxy) and X-Accel-Buffering disables nginx-style buffering.
    return StreamingResponse(
        body(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{customization_id}/cover-letter/download")
def download_cover_letter(
    customization_id: str,
    format: str = Query("docx", pattern="^(pdf|docx|txt)$"),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> Response:
    doc = _owned(db, user["_id"], customization_id)
    if not doc.get("cover_letter"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Generate a cover letter first")
    resume = CustomizationResult.model_validate(doc["result"]).resume
    return Response(
        COVER_LETTER_EXPORTERS[format](resume, doc["cover_letter"]["text"]),
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{cover_letter_filename(resume, format)}"'},
    )
