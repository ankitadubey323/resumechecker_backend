"""Field rules shared by the auth schemas. Keep in sync with frontend/src/lib/validation.ts."""

import re

import phonenumbers

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,99}$")

PASSWORD_MIN = 8
PASSWORD_MAX = 64  # bcrypt only uses the first 72 bytes; 64 chars keeps typical passwords under that
PASSWORD_MAX_BYTES = 72
PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def validate_full_name(value: str) -> str:
    value = " ".join(value.split())
    if not NAME_RE.fullmatch(value):
        raise ValueError(
            "Name must be 2-100 characters and contain only letters, spaces, . ' or -"
        )
    return value


def normalize_phone(value: str) -> str:
    """Parse an international phone number (must include +country code) into E.164."""
    try:
        parsed = phonenumbers.parse(value.strip(), None)
    except phonenumbers.NumberParseException:
        raise ValueError("Enter a valid phone number including country code, e.g. +14155552671")
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Enter a valid phone number including country code, e.g. +14155552671")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def validate_password_strength(password: str, email: str | None = None) -> str:
    problems: list[str] = []
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        problems.append(f"be {PASSWORD_MIN}-{PASSWORD_MAX} characters long")
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        problems.append("be shorter (too many special or non-English characters)")
    if not re.search(r"[a-z]", password):
        problems.append("contain a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("contain an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("contain a number")
    if not PASSWORD_SPECIAL_RE.search(password):
        problems.append("contain a special character")
    if password != password.strip():
        problems.append("not start or end with a space")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 3 and local in password.lower():
            problems.append("not contain your email address")
    if problems:
        raise ValueError("Password must " + ", ".join(problems))
    return password
