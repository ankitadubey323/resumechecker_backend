from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationInfo, field_validator

from app.validators import normalize_phone, validate_full_name, validate_password_strength


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=500)


class SignupRequest(_Strict):
    # Field order matters: later validators read earlier values from info.data.
    full_name: str
    email: EmailStr
    phone: str = Field(max_length=30)
    # Length is checked by validate_password_strength (friendlier message); _Strict caps input at 500.
    password: str
    confirm_password: str

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_full_name(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str, info: ValidationInfo) -> str:
        return validate_password_strength(v, info.data.get("email"))

    @field_validator("confirm_password")
    @classmethod
    def _confirm(cls, v: str, info: ValidationInfo) -> str:
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class LoginRequest(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()


class UserOut(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str

    @classmethod
    def from_doc(cls, doc: dict) -> "UserOut":
        return cls(
            id=str(doc["_id"]),
            full_name=doc["full_name"],
            email=doc["email"],
            phone=doc["phone"],
        )


class MessageOut(BaseModel):
    message: str
