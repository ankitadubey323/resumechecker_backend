# Resume Customizer — Backend

FastAPI + MongoDB API for the AI Resume Customizer. It handles authentication, resume upload and parsing, profile details, job descriptions, and LLM-powered resume tailoring (Groq via LangChain).

## Tech stack

- **FastAPI** (Python 3.12) with **Uvicorn**
- **MongoDB** (Atlas or local) via **PyMongo**
- **LangChain + Groq** for the LLM (`openai/gpt-oss-120b`, with an automatic fallback model)
- **pdfplumber** / **python-docx** to extract resume text
- **ReportLab** / **python-docx** to export PDF, DOCX and TXT
- **bcrypt** + **PyJWT** for auth: access and refresh tokens in HttpOnly cookies
- **slowapi** for rate limiting
- **pytest** + **mongomock** for tests

## Project structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app, CORS, routers, /health
│   ├── config.py            # Settings loaded from .env
│   ├── database.py          # MongoDB connection
│   ├── security.py          # Password hashing, JWT
│   ├── dependencies.py      # get_current_user
│   ├── rate_limit.py
│   ├── routers/
│   │   ├── auth.py          # signup, login, refresh, logout, me
│   │   ├── dashboard.py     # profile, photo, resume, details, job description
│   │   └── customize.py     # AI customization, versions, downloads, cover letter
│   ├── llm/
│   │   ├── prompts.py       # System prompts (resume + cover letter)
│   │   ├── schemas.py       # Structured LLM output (Pydantic)
│   │   └── customizer.py    # LangChain chain, fallback model, post-checks, streaming
│   └── services/
│       ├── resume_parser.py # PDF/DOCX text extraction
│       ├── exporter.py      # PDF/DOCX/TXT export
│       ├── ats.py           # Rule-based ATS score
│       ├── dashboard.py
│       └── images.py        # Profile photo processing
├── tests/
├── requirements.txt
├── .python-version          # 3.12
└── .env.example
```

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your values
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | | `development` | Set to `production` when deployed (secure cookies) |
| `FRONTEND_URL` | | `http://localhost:3000` | Frontend origin, for CORS |
| `MONGO_URI` | * | | Full MongoDB connection string (or use the fields below) |
| `MONGO_USERNAME` / `MONGO_PASSWORD` | * | | MongoDB credentials |
| `MONGO_HOST` | | `localhost` | e.g. `cluster0.xxxxx.mongodb.net` for Atlas |
| `MONGO_PORT` | | `27017` | |
| `MONGO_DATABASE` | | `resume_customizer` | |
| `ACCESS_TOKEN_SECRET` | ✅ | | Random string, at least 32 characters |
| `REFRESH_TOKEN_SECRET` | ✅ | | Random string, at least 32 characters (different from the access secret) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | | `15` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | | `7` | |
| `GROQ_API_KEY` | ✅ | | From https://console.groq.com/keys |
| `GROQ_MODEL` | | `openai/gpt-oss-120b` | Main model |
| `GROQ_FALLBACK_MODEL` | | `qwen/qwen3.8-27b` | Used automatically if the main model fails |
| `LLM_TIMEOUT_SECONDS` | | `90` | |
| `CUSTOMIZATIONS_PER_HOUR` | | `10` | Per-user limit |

\* Provide either `MONGO_URI`, or the username, password and host fields.

To generate a secret:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

- API: http://localhost:8000
- Interactive docs (Swagger): http://localhost:8000/docs
- Health check: http://localhost:8000/health

## Tests

```bash
pytest -q
```

Tests use an in-memory MongoDB (mongomock) and a fake LLM, so they need no database or API key.

## API overview

**Auth:** `/api/auth`

| Method | Path | Description |
|---|---|---|
| POST | `/signup` | Create an account (name, email, phone, password) |
| POST | `/login` | Log in; sets HttpOnly cookies |
| POST | `/refresh` | Rotate the refresh token, issue a new access token |
| POST | `/logout` | Revoke the refresh token, clear cookies |
| GET | `/me` | Current user |

**Dashboard:** `/api`

| Method | Path | Description |
|---|---|---|
| GET | `/dashboard` | Profile, resume, job description and readiness state |
| POST / GET / DELETE | `/profile/photo` | Upload, view or remove the profile photo |
| POST / DELETE | `/resume` | Upload a PDF/DOCX resume (max 5 MB), or remove it |
| PUT | `/profile/details` | Save manually entered details |
| PUT | `/profile/input` | Choose the AI input: `resume` or `details` |
| POST | `/job-descriptions` | Save a job description |

**Customization:** `/api/customizations`

| Method | Path | Description |
|---|---|---|
| POST | `` | Generate a tailored resume (optional `sections`: summary, skills, experience, projects) |
| GET | `?source=resume\|details` | List saved versions |
| GET | `/latest` | Newest result for the selected input |
| GET | `/{id}` | One version: result, ATS score, diff texts, cover letter |
| GET | `/{id}/download?format=pdf\|docx\|txt` | Download the tailored resume |
| POST | `/{id}/cover-letter` | Generate a cover letter (**streamed** as plain text) |
| GET | `/{id}/cover-letter/download?format=pdf\|docx\|txt` | Download the cover letter |

## How the AI customization works

1. The candidate's data comes from **one** input: the uploaded resume, or the manually entered details, whichever the user selected.
2. LangChain sends it, together with the job description, to Groq using **structured output**: a strict JSON schema validated with Pydantic.
3. The prompt forbids inventing experience, employers, numbers or skills. After the call, a post-check removes any invented experience entries.
4. If the main model fails or is rate-limited, the **fallback model** is used automatically.
5. The result includes the tailored resume, match scores (before and after), matched and missing keywords, bullet changes and suggestions. A rule-based **ATS score** is calculated on top.

## Error handling

| Case | Response |
|---|---|
| Wrong file type or file too large | `400` / `413` with a clear message |
| Empty or too-short job description | `422` |
| Missing resume/details or job description | `409` |
| LLM rate limit / timeout / failure | `429` / `504` / `502` with a user-friendly message |
| Too many requests | `429` (per-IP and per-user limits) |

## Deployment (Render)

- **Runtime:** Python 3.12 (from `.python-version`)
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
  (`--proxy-headers` makes the per-IP rate limits use the visitor's real IP instead of Render's proxy.)
- **Health check path:** `/health`
- Set all the environment variables above, with `ENVIRONMENT=production` and `FRONTEND_URL` set to your frontend URL.
- In MongoDB Atlas → Network Access, allow your host's IP addresses.
