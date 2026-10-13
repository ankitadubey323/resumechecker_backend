"""MongoDB access.

Collections (all ids are ObjectIds unless noted):
  users             email (unique), password_hash, ...
  refresh_tokens    _id = jti (str), user_id, family, used_at, expires_at (TTL)
  profiles          _id = user_id; manual details + photo
  resumes           _id = user_id; current uploaded resume
  job_descriptions  user_id, text, created_at
  customizations    user_id, job_description_id, result (LLM output), warnings, model, created_at
"""

from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from app.config import get_settings

_client: MongoClient | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_client() -> MongoClient:
    global _client
    if _client is None:
        # tz_aware: datetimes come back as UTC-aware, so the API always sends "+00:00".
        _client = MongoClient(
            get_settings().mongo_url, tz_aware=True, serverSelectionTimeoutMS=10_000, appname="resume-customizer"
        )
    return _client


def get_db() -> Database:
    return get_client()[get_settings().mongo_database]


def ensure_indexes(db: Database) -> None:
    db.users.create_index("email", unique=True)
    db.refresh_tokens.create_index("family")
    db.refresh_tokens.create_index("user_id")
    db.job_descriptions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    db.customizations.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    # MongoDB deletes these automatically once expires_at has passed.
    db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)
