import secrets
from datetime import timedelta

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.database import get_db, utcnow
from app.dependencies import get_current_user
from app.rate_limit import limiter
from app.schemas import LoginRequest, MessageOut, SignupRequest, UserOut
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Two browser tabs can refresh at the same moment with the same token. A reuse
# this soon after rotation is treated as that race, not as theft.
REFRESH_REUSE_GRACE = timedelta(seconds=30)


# --- sessions ----------------------------------------------------------------


def _cookie_args() -> dict:
    s = get_settings()
    return {"httponly": True, "secure": s.is_production, "samesite": "lax"}


def _start_session(response: Response, db: Database, user_id: str, family: str | None = None) -> None:
    """Issue a new access token + refresh token pair as HttpOnly cookies.

    The refresh token's id (jti) is stored so it can be used exactly once:
    every refresh replaces it, and all tokens from one login share a family.
    """
    s = get_settings()
    now = utcnow()
    jti = secrets.token_urlsafe(24)
    family = family or secrets.token_urlsafe(16)
    expires_at = now + timedelta(days=s.refresh_token_expire_days)
    db.refresh_tokens.insert_one(
        {"_id": jti, "user_id": user_id, "family": family, "used_at": None, "created_at": now, "expires_at": expires_at}
    )
    max_age = s.refresh_token_expire_days * 86400
    # The access JWT itself expires after ACCESS_TOKEN_EXPIRE_MINUTES; the cookie lives as long as
    # the session so the frontend's route guard can tell a logged-in visitor from a new one.
    response.set_cookie(s.access_cookie_name, create_access_token(user_id), max_age=max_age, path="/", **_cookie_args())
    # Only sent to /api/auth/* (refresh, logout), never to the rest of the API.
    response.set_cookie(
        s.refresh_cookie_name,
        create_refresh_token(user_id, jti, family, expires_at),
        max_age=max_age,
        path="/api/auth",
        **_cookie_args(),
    )


def _clear_session_cookies(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(s.access_cookie_name, path="/", **_cookie_args())
    response.delete_cookie(s.refresh_cookie_name, path="/api/auth", **_cookie_args())


def _session_expired(clear_cookies: bool = True) -> JSONResponse:
    response = JSONResponse(
        {"detail": {"code": "session_expired", "message": "Your session has expired. Please log in again."}},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )
    if clear_cookies:
        _clear_session_cookies(response)
    return response


# --- endpoints ---------------------------------------------------------------


EMAIL_TAKEN = {"code": "email_taken", "message": "An account with this email already exists. Log in instead."}


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
def signup(
    request: Request,
    response: Response,
    body: SignupRequest,
    db: Database = Depends(get_db),
) -> UserOut:
    """Create the account and log the user straight in."""
    if db.users.find_one({"email": body.email}, {"_id": 1}):
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN)

    user = {
        "full_name": body.full_name,
        "email": body.email,
        "phone": body.phone,
        "password_hash": hash_password(body.password),
        "created_at": utcnow(),
    }
    try:
        user["_id"] = db.users.insert_one(user).inserted_id
    except DuplicateKeyError:  # a concurrent signup for this email won the race
        raise HTTPException(status.HTTP_409_CONFLICT, EMAIL_TAKEN)

    _start_session(response, db, str(user["_id"]))
    return UserOut.from_doc(user)


@router.post("/login", response_model=UserOut)
@limiter.limit("10/minute")
def login(request: Request, response: Response, body: LoginRequest, db: Database = Depends(get_db)) -> UserOut:
    user = db.users.find_one({"email": body.email})
    # verify_password runs a full bcrypt check even when user is None (constant timing).
    if not verify_password(body.password, user["password_hash"] if user else None):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    assert user is not None

    if needs_rehash(user["password_hash"]):  # BCRYPT_ROUNDS changed since this hash was made
        db.users.update_one({"_id": user["_id"]}, {"$set": {"password_hash": hash_password(body.password)}})

    _start_session(response, db, str(user["_id"]))
    return UserOut.from_doc(user)


@router.post("/refresh", response_model=UserOut)
@limiter.limit("30/minute")
def refresh(request: Request, response: Response, db: Database = Depends(get_db)):
    """Swap a valid refresh token for a new access + refresh token pair (rotation).

    If an already-used refresh token shows up again, someone may have copied it,
    so every session from that login is revoked.
    """
    raw = request.cookies.get(get_settings().refresh_cookie_name)
    claims = decode_refresh_token(raw) if raw else None
    if claims is None:
        return _session_expired()

    now = utcnow()
    token = db.refresh_tokens.find_one_and_update(
        {"_id": claims.jti, "used_at": None, "expires_at": {"$gt": now}}, {"$set": {"used_at": now}}
    )
    if token is None:
        previous = db.refresh_tokens.find_one({"_id": claims.jti})
        if previous and previous.get("used_at") and now - previous["used_at"] < REFRESH_REUSE_GRACE:
            # Another tab just rotated it and already has new cookies; don't clear them.
            return _session_expired(clear_cookies=False)
        db.refresh_tokens.delete_many({"family": claims.family})
        return _session_expired()

    user = db.users.find_one({"_id": ObjectId(claims.user_id)}) if ObjectId.is_valid(claims.user_id) else None
    if user is None:  # account deleted
        db.refresh_tokens.delete_many({"family": claims.family})
        return _session_expired()

    _start_session(response, db, claims.user_id, family=claims.family)
    return UserOut.from_doc(user)


@router.post("/logout", response_model=MessageOut)
def logout(request: Request, response: Response, db: Database = Depends(get_db)) -> MessageOut:
    raw = request.cookies.get(get_settings().refresh_cookie_name)
    # Expired tokens are fine here: we only need the family to revoke it.
    claims = decode_refresh_token(raw, verify_exp=False) if raw else None
    if claims:
        db.refresh_tokens.delete_many({"family": claims.family})
    _clear_session_cookies(response)
    return MessageOut(message="Logged out")


@router.get("/me", response_model=UserOut)
def me(user: dict = Depends(get_current_user)) -> UserOut:
    return UserOut.from_doc(user)
