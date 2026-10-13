from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
from pymongo.database import Database

from app.config import get_settings
from app.database import get_db
from app.security import decode_access_token


def get_current_user(request: Request, db: Database = Depends(get_db)) -> dict:
    """The logged-in user's document. 401 when the access token is missing, expired or invalid;
    the frontend then calls /api/auth/refresh and retries."""
    token = request.cookies.get(get_settings().access_cookie_name)
    user_id = decode_access_token(token) if token else None
    user = db.users.find_one({"_id": ObjectId(user_id)}) if user_id and ObjectId.is_valid(user_id) else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user
