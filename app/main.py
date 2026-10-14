import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.database import ensure_indexes, get_db
from app.rate_limit import limiter
from app.routers import auth, customize, dashboard

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes(get_db())  # idempotent; also fails fast if MongoDB is unreachable
    yield


settings = get_settings()
app = FastAPI(
    title="Resume Customizer API",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.limiter = limiter

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Personal data: never cache unless the endpoint set its own policy.
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return {"detail": str, "errors": {field: message}} so the UI can show errors per field.
    Nested fields use dotted paths, e.g. "projects.0.title"."""
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if p != "body"]
        # Drop pydantic's internal type tags such as "function-after[...]" from the path.
        loc = [p for p in loc if "[" not in p]
        field = ".".join(loc) if loc else "non_field"
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "missing":
            msg = "This field is required"
        errors.setdefault(field, msg)
    return JSONResponse(
        status_code=422,
        content={"detail": "Please fix the highlighted fields.", "errors": errors},
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many attempts. Please wait a bit and try again."},
    )


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(customize.router)


# HEAD too: uptime monitors (e.g. UptimeRobot) check with HEAD requests.
@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, str]:
    return {"status": "ok"}
