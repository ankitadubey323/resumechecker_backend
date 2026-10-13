"""Resume customization with LangChain + Groq.

Pipeline:  prompt | ChatGroq.with_structured_output(CustomizationResult)
           primary model, with the fallback model as an automatic backup
           -> post-checks (made-up numbers, skills contradicting missing_keywords)
"""

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import groq
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_groq import ChatGroq

from app.config import get_settings
from app.llm.prompts import SECTIONS, cover_letter_prompt, customize_prompt, sections_instruction
from app.llm.schemas import CustomizationResult
from app.services.dashboard import input_source

logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class CustomizationOutput:
    result: CustomizationResult
    model: str
    warnings: list[str] = field(default_factory=list)


# --- building the candidate input -------------------------------------------


def build_candidate_text(user: dict, profile: dict | None, resume: dict | None) -> str:
    """The candidate's data as plain text for the prompt.

    Only ONE input is used: whichever the user gave most recently (see input_source), so the
    result always reflects what they just provided.
    """
    parts = [f"Name: {user['full_name']}", f"Email: {user['email']}", f"Phone: {user['phone']}"]

    if input_source(profile, resume) == "resume":
        assert resume is not None
        parts += ["", "=== RESUME (extracted from the uploaded file) ===", resume["text"]]
    else:
        p = profile or {}  # endpoint guarantees details exist here; stay safe anyway
        lines = [
            "",
            "=== PROFILE DETAILS (entered by the candidate) ===",
            f"Headline: {p.get('headline')}",
            f"Experience level: {p.get('experience_level')}"
            + (f", {p.get('years_experience')} years" if p.get("experience_level") == "experienced" else ""),
            f"Skills: {', '.join(p.get('skills') or [])}",
        ]
        for e in p.get("experiences") or []:
            lines += [
                f"Work experience: {e['role']} at {e['company']} ({e['start']} to {e.get('end') or 'Present'})",
                f"  {e.get('description') or ''}".rstrip(),
            ]
        for pr in p.get("projects") or []:
            tech = f" [{', '.join(pr.get('tech') or [])}]" if pr.get("tech") else ""
            link = f" ({pr['link']})" if pr.get("link") else ""
            lines += [f"Project: {pr['title']}{tech}{link}", f"  {pr.get('description') or ''}".rstrip()]
        for ed in p.get("education") or []:
            year = f", {ed['year']}" if ed.get("year") else ""
            lines.append(f"Education: {ed['degree']}, {ed['institution']}{year}")
        parts += [line for line in lines if line.strip()]

    return "\n".join(parts)


# --- the chain ---------------------------------------------------------------


def _unwrap(model: str):
    """Turn LangChain's include_raw output into (result, model), raising on a parse failure
    so that with_fallbacks moves on to the next model."""

    def unwrap(out: dict) -> tuple[CustomizationResult, str]:
        if out.get("parsed") is None:
            raise ValueError(f"{model} returned output that doesn't match the schema: {out.get('parsing_error')}")
        return out["parsed"], model

    return RunnableLambda(unwrap)


def _llm(model: str, **overrides) -> ChatGroq:
    s = get_settings()
    params = {
        "model": model,
        "api_key": s.groq_api_key,
        "temperature": 0.2,  # low: faithful rewriting, not creative writing
        "request_timeout": s.llm_timeout_seconds,
        "max_tokens": s.llm_max_output_tokens,
        # The Groq client retries 429/5xx itself and honours Groq's retry-after header, so a
        # brief free-tier rate limit becomes a short wait instead of an error.
        "max_retries": 2,
    }
    if model.startswith("openai/gpt-oss"):
        params["reasoning_effort"] = s.llm_reasoning_effort
    return ChatGroq(**{**params, **overrides})


def _models() -> list[str]:
    s = get_settings()
    return [s.groq_model] + ([s.groq_fallback_model] if s.groq_fallback_model else [])


def _model_chain(model: str) -> Runnable:
    llm = _llm(model)
    # strict=True: Groq enforces the JSON schema server-side, so required fields can't be skipped.
    structured = llm.with_structured_output(CustomizationResult, method="json_schema", strict=True, include_raw=True)
    chain = customize_prompt | structured | _unwrap(model)
    # Models occasionally produce output that fails the schema (Groq returns 400
    # json_validate_failed, or parsing fails). A fresh attempt usually succeeds.
    # Rate limits are not retried here; the fallback model handles those.
    return (
        chain.with_retry(
            retry_if_exception_type=(groq.BadRequestError, ValueError), stop_after_attempt=2, wait_exponential_jitter=False
        )
        # with_fallbacks only re-raises the first error, so log every model's failure here.
        .with_listeners(on_error=lambda run: logger.warning("LLM %s failed: %s", model, run.error))
        .with_config(run_name=model)
    )


def _build_chain() -> Runnable:
    chains = [_model_chain(m) for m in _models()]
    # If the primary model errors, times out or is rate-limited, the next one is tried automatically.
    return chains[0].with_fallbacks(chains[1:]) if len(chains) > 1 else chains[0]


def _to_llm_error(exc: Exception) -> LLMError:
    if isinstance(exc, groq.RateLimitError):
        retry = exc.response.headers.get("retry-after") if exc.response is not None else None
        wait = f"in about {int(float(retry)) + 1} seconds" if retry and retry.replace(".", "", 1).isdigit() else "in a minute"
        return LLMError(f"The AI service is busy right now (free-tier limit). Please try again {wait}.", 429)
    if isinstance(exc, groq.AuthenticationError):
        logger.error("Groq rejected the API key: %s", exc)
        return LLMError("The AI service isn't configured correctly. Please contact support.", 503)
    if isinstance(exc, (groq.APITimeoutError, TimeoutError)):
        return LLMError("The AI took too long to respond. Please try again.", 504)
    if isinstance(exc, groq.APIConnectionError):
        return LLMError("Couldn't reach the AI service. Please try again.", 503)
    logger.exception("Resume customization failed", exc_info=exc)
    return LLMError("The AI couldn't produce a valid result this time. Please try again.", 502)


def customize_resume(candidate: str, job_description: str, sections: list[str] | None = None) -> CustomizationOutput:
    if not get_settings().groq_api_key:
        raise LLMError("AI customization isn't configured (GROQ_API_KEY is missing).", 503)

    scope = sections_instruction(list(sections or SECTIONS))
    try:
        result, model = _build_chain().invoke(
            {"candidate": candidate, "job_description": job_description, "sections_instruction": scope}
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a user-facing error below
        raise _to_llm_error(exc) from exc

    result = _normalize_text(result)
    return CustomizationOutput(result=result, model=model, warnings=_post_check(result, candidate))


def stream_cover_letter(resume_text: str, job_description: str, missing_keywords: list[str]) -> Iterator[str]:
    """Stream a cover letter token by token (plain text, so it can be shown as it's written).

    The first chunk is fetched before returning, so errors such as a rate limit surface as a
    normal LLMError (and a proper HTTP status) instead of breaking a half-sent stream.
    """
    if not get_settings().groq_api_key:
        raise LLMError("AI isn't configured (GROQ_API_KEY is missing).", 503)

    chains = [
        (cover_letter_prompt | _llm(m, temperature=0.3) | StrOutputParser()).with_config(run_name=f"cover-letter:{m}")
        for m in _models()
    ]
    # with_fallbacks also works for streaming: the backup is used if the primary fails before its first token.
    chain = chains[0].with_fallbacks(chains[1:]) if len(chains) > 1 else chains[0]
    stream = chain.stream(
        {
            "resume": resume_text,
            "job_description": job_description,
            "missing_keywords": ", ".join(missing_keywords) or "(none)",
        }
    )
    try:
        first = next(stream, "")
    except Exception as exc:  # noqa: BLE001 - mapped to a user-facing error below
        raise _to_llm_error(exc) from exc

    def rest() -> Iterator[str]:
        yield first.translate(_CHAR_FIXES)
        for chunk in stream:
            yield chunk.translate(_CHAR_FIXES)

    return rest()


# --- post-checks: catch what the prompt alone can't guarantee -----------------

# Characters models like to emit that standard PDF fonts can't draw (they render as ■).
_CHAR_FIXES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "–", "\u2212": "-", "\u00a0": " ", "\u202f": " ", "\u200b": ""})


def _normalize_text(result: CustomizationResult) -> CustomizationResult:
    def fix(value):
        if isinstance(value, str):
            return value.translate(_CHAR_FIXES).strip()
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value

    return CustomizationResult.model_validate(fix(result.model_dump()))


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUMBER_RE.findall(text)}


def _post_check(result: CustomizationResult, candidate: str) -> list[str]:
    warnings: list[str] = []
    resume, analysis = result.resume, result.analysis

    # 1. Experience entries the model invented (e.g. a fresher's project turned into a "job").
    #    Keep an entry only if its company and its years appear in the candidate's own data.
    source_lower = candidate.lower()
    kept = []
    for e in resume.experience:
        years = re.findall(r"(?:19|20)\d\d", f"{e.start} {e.end}")
        if e.company.lower() in source_lower and all(y in candidate for y in years):
            kept.append(e)
        else:
            warnings.append(
                f'Removed "{e.title} at {e.company}" from experience: the AI added it, but it isn\'t in your data.'
            )
    resume.experience = kept

    # 2. Numbers in the tailored text that never appeared in the candidate's data.
    source_numbers = _numbers(candidate)
    texts = [resume.summary] + [b for e in resume.experience for b in e.bullets] + [
        b for p in resume.projects for b in p.bullets
    ]
    for text in texts:
        for match in _NUMBER_RE.finditer(text):
            number = match.group().replace(",", "")
            if number not in source_numbers:
                start, end = max(0, match.start() - 40), min(len(text), match.end() + 40)
                excerpt = ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
                warnings.append(f'"{excerpt}": {match.group()} isn\'t in your original resume. Check it\'s accurate.')

    # 3. A skill can't be both "missing" and listed in the resume.
    missing = {k.lower() for k in analysis.missing_keywords}
    for group in resume.skills:
        removed = [s for s in group.items if s.lower() in missing]
        if removed:
            group.items = [s for s in group.items if s.lower() not in missing]
            logger.info("Removed skills the model also marked as missing: %s", removed)
    resume.skills = [g for g in resume.skills if g.items]

    # 4. Keep scores sane: whole numbers in 0-100, and tailoring never lowers the score.
    analysis.match_score_before = float(min(100, max(0, round(analysis.match_score_before))))
    analysis.match_score_after = float(min(100, max(0, round(analysis.match_score_after))))
    if analysis.match_score_after < analysis.match_score_before:
        analysis.match_score_after = analysis.match_score_before
    return warnings
