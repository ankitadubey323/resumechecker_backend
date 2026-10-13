import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings

BCRYPT_MAX_BYTES = 72  # bcrypt ignores (or rejects) anything longer


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)
    return bcrypt.hashpw(password.encode(), salt).decode()


# Checked against when the email is unknown, so login takes the same time
# whether or not the account exists (prevents user enumeration by timing).
_dummy_hash: bytes | None = None


def _get_dummy_hash() -> bytes:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16)).encode()
    return _dummy_hash


def verify_password(password: str, password_hash: str | None) -> bool:
    candidate = password.encode()[:BCRYPT_MAX_BYTES]
    target = password_hash.encode() if password_hash else _get_dummy_hash()
    try:
        ok = bcrypt.checkpw(candidate, target)
    except ValueError:
        return False
    return ok and password_hash is not None and len(password.encode()) <= BCRYPT_MAX_BYTES


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with a different BCRYPT_ROUNDS than configured now."""
    try:
        return int(password_hash.split("$")[2]) != get_settings().bcrypt_rounds
    except (IndexError, ValueError):
        return True


def _encode(payload: dict, secret: str) -> str:
    return jwt.encode(payload, secret, algorithm=get_settings().jwt_algorithm)


def _decode(token: str, secret: str, token_type: str, verify_exp: bool = True) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[get_settings().jwt_algorithm],
            options={"require": ["sub", "exp", "iat"], "verify_exp": verify_exp},
        )
    except jwt.PyJWTError:
        return None
    return payload if payload.get("type") == token_type else None


def create_access_token(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    return _encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=s.access_token_expire_minutes), "type": "access"},
        s.access_token_secret,
    )


def decode_access_token(token: str) -> str | None:
    payload = _decode(token, get_settings().access_token_secret, "access")
    return payload["sub"] if payload else None


@dataclass
class RefreshClaims:
    user_id: str
    jti: str
    family: str


def create_refresh_token(user_id: str, jti: str, family: str, expires_at: datetime) -> str:
    return _encode(
        {
            "sub": user_id,
            "jti": jti,
            "fam": family,
            "iat": datetime.now(timezone.utc),
            "exp": expires_at,
            "type": "refresh",
        },
        get_settings().refresh_token_secret,
    )


def decode_refresh_token(token: str, verify_exp: bool = True) -> RefreshClaims | None:
    payload = _decode(token, get_settings().refresh_token_secret, "refresh", verify_exp)
    if not payload or not payload.get("jti") or not payload.get("fam"):
        return None
    return RefreshClaims(user_id=payload["sub"], jti=payload["jti"], family=payload["fam"])
