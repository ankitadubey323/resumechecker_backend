from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    frontend_url: str = "http://localhost:3000"

    # MongoDB. Either set MONGO_URI, or the individual parts below.
    mongo_uri: str = ""
    mongo_username: str = ""
    mongo_password: str = ""
    mongo_host: str = "localhost"
    mongo_port: int = 27017
    mongo_srv: bool | None = None  # None = auto (Atlas *.mongodb.net hosts use mongodb+srv)
    mongo_database: str = "resume_customizer"

    # Password hashing cost. Each +1 doubles the work; 12 is ~250ms on a typical server.
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    access_token_secret: str = Field(min_length=32)
    access_token_expire_minutes: int = Field(default=15, ge=1)
    refresh_token_secret: str = Field(min_length=32)
    refresh_token_expire_days: int = Field(default=7, ge=1)
    jwt_algorithm: str = "HS256"

    # LLM (Groq via LangChain). The fallback model is used automatically if the
    # primary one fails or is rate-limited; set it to "" to disable.
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    # Best-effort backup. On Groq's free tier qwen allows only ~1000 output tokens/min, so it
    # can't always take over; gpt-oss-20b can't reliably produce our JSON schema at all.
    groq_fallback_model: str = "qwen/qwen3.8-27b"
    llm_timeout_seconds: int = Field(default=90, ge=10)
    llm_reasoning_effort: str = "low"  # low | medium | high (gpt-oss models); low is faster and cheaper
    llm_max_output_tokens: int = Field(default=8000, ge=1000)  # reasoning + JSON; too low -> empty answers
    customizations_per_hour: int = Field(default=10, ge=1)

    access_cookie_name: str = "access_token"
    refresh_cookie_name: str = "refresh_token"

    @field_validator("access_token_secret", "refresh_token_secret")
    @classmethod
    def reject_placeholder_secret(cls, v: str) -> str:
        if v.startswith("change-me"):
            raise ValueError("still the placeholder value; set a real secret")
        return v

    @model_validator(mode="after")
    def secrets_differ(self) -> "Settings":
        if self.access_token_secret == self.refresh_token_secret:
            raise ValueError("ACCESS_TOKEN_SECRET and REFRESH_TOKEN_SECRET must be different")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def mongo_url(self) -> str:
        if self.mongo_uri:
            return self.mongo_uri
        creds = (
            f"{quote_plus(self.mongo_username)}:{quote_plus(self.mongo_password)}@" if self.mongo_username else ""
        )
        srv = self.mongo_srv if self.mongo_srv is not None else self.mongo_host.endswith(".mongodb.net")
        if srv:
            return f"mongodb+srv://{creds}{self.mongo_host}/?retryWrites=true&w=majority"
        return f"mongodb://{creds}{self.mongo_host}:{self.mongo_port}/"


@lru_cache
def get_settings() -> Settings:
    return Settings()
