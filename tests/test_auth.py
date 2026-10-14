from datetime import datetime, timezone

import pytest

VALID = {
    "full_name": "Jane Doe",
    "email": "Jane.Doe@Example.com",
    "phone": "+14155552671",
    "password": "Str0ng!Pass",
    "confirm_password": "Str0ng!Pass",
}


def signup(client, **overrides):
    return client.post("/api/auth/signup", json={**VALID, **overrides})


# --- signup validation -------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "not-an-email"),
        ("email", "a@b"),
        ("phone", "12345"),
        ("phone", "4155552671"),  # missing country code
        ("phone", "+1 000 000 0000"),
        ("full_name", "J"),
        ("full_name", "Jane<script>"),
        ("password", "short1!"),
        ("password", "alllowercase1!"),
        ("password", "NoDigits!!"),
        ("password", "NoSpecial123"),
        ("password", "jane.doe!A1"),  # contains email local part
        ("password", "Aa1!" + "x" * 61),  # 65 chars: over bcrypt-safe limit
        ("password", "Aa1!" + "é" * 40),  # 44 chars but 84 bytes
    ],
)
def test_signup_rejects_invalid_field(client, field, value):
    payload = {field: value}
    if field == "password":
        payload["confirm_password"] = value
    r = signup(client, **payload)
    assert r.status_code == 422
    assert field in r.json()["errors"]


def test_signup_rejects_mismatched_confirm_password(client):
    r = signup(client, confirm_password="Different1!")
    assert r.status_code == 422
    assert r.json()["errors"] == {"confirm_password": "Passwords do not match"}


def test_signup_rejects_missing_and_unknown_fields(client):
    r = client.post("/api/auth/signup", json={"email": "a@example.com", "is_admin": True})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert errors["password"] == "This field is required"
    assert "is_admin" in errors


def test_signup_logs_in_directly(client):
    r = signup(client, phone="+1 (415) 555-2671")
    assert r.status_code == 201
    assert r.json()["phone"] == "+14155552671"
    assert r.json()["email"] == "jane.doe@example.com"
    assert client.cookies.get("access_token") and client.cookies.get("refresh_token")
    assert client.get("/api/dashboard").status_code == 200


# --- login & sessions --------------------------------------------------------


def test_full_flow(client):
    assert signup(client).status_code == 201
    client.cookies.clear()

    r = client.post("/api/auth/login", json={"email": "JANE.DOE@example.com", "password": VALID["password"]})
    assert r.status_code == 200
    assert r.json()["phone"] == "+14155552671"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "password_hash" not in r.text

    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "jane.doe@example.com"

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_and_unknown_email_give_same_error(client):
    signup(client)
    a = client.post("/api/auth/login", json={"email": VALID["email"], "password": "Wrong1!pass"})
    b = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "Wrong1!pass"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_signup_with_existing_email_is_rejected(client):
    signup(client)
    client.cookies.clear()
    r = signup(client, email="JANE.DOE@example.com", password="0ther!Pass", confirm_password="0ther!Pass")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "email_taken"
    # Must not log the second person into the existing account.
    assert not client.cookies.get("access_token")


def test_access_token_refresh_rotation(client, db):
    signup(client)
    old_refresh = client.cookies.get("refresh_token")
    assert old_refresh and client.cookies.get("access_token")

    r = client.post("/api/auth/refresh")
    assert r.status_code == 200 and r.json()["email"] == "jane.doe@example.com"
    new_refresh = client.cookies.get("refresh_token")
    assert new_refresh != old_refresh
    assert client.get("/api/auth/me").status_code == 200


def test_refresh_token_reuse_revokes_family(client, db):
    signup(client)
    stolen = client.cookies.get("refresh_token")
    assert client.post("/api/auth/refresh").status_code == 200
    legit = client.cookies.get("refresh_token")

    # Pretend the rotation happened long ago so this isn't treated as a two-tab race.
    db.refresh_tokens.update_many({"used_at": {"$ne": None}}, {"$set": {"used_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}})
    client.cookies.clear()
    client.cookies.set("refresh_token", stolen, path="/api/auth")
    r = client.post("/api/auth/refresh")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "session_expired"

    # The legitimate session from the same login is revoked too.
    client.cookies.clear()
    client.cookies.set("refresh_token", legit, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_refresh_without_cookie(client):
    r = client.post("/api/auth/refresh")
    assert r.status_code == 401


def test_expired_access_token_rejected(client, monkeypatch):
    signup(client)
    from app import security

    monkeypatch.setattr(security.get_settings(), "access_token_expire_minutes", -1)
    client.post("/api/auth/refresh")  # issues an already-expired access token
    assert client.get("/api/auth/me").status_code == 401


def test_logout_revokes_refresh_token(client):
    signup(client)
    refresh = client.cookies.get("refresh_token")
    client.post("/api/auth/logout")
    client.cookies.set("refresh_token", refresh, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_password_stored_with_bcrypt(client, db):
    signup(client)
    user = db.users.find_one({"email": "jane.doe@example.com"})
    assert user["password_hash"].startswith("$2b$04$")
    assert "Str0ng!Pass" not in str(user)


def test_tampered_cookie_rejected(client):
    client.cookies.set("access_token", "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.")
    assert client.get("/api/auth/me").status_code == 401


def test_login_rate_limited(client):
    codes = [
        client.post("/api/auth/login", json={"email": "x@example.com", "password": "nope"}).status_code
        for _ in range(11)
    ]
    assert codes[-1] == 429


def test_health_accepts_get_and_head(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.head("/health").status_code == 200  # uptime monitors use HEAD
