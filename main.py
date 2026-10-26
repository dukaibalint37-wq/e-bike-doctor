"""E-Bike Doctor AI API. Provider keys must remain server-side."""
import json
import logging
import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ebike-doctor")

app = FastAPI(title="E-Bike Doctor AI API", version="0.3.0")

# Gemini is preferred. OpenAI remains an optional fallback for existing deployments.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_10_MINUTES", "10"))
WINDOW_SECONDS = 600
_request_times: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()

class DiagnosisRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    brand: str = Field(default="", max_length=120)
    motor_model: str = Field(default="", max_length=160)
    display_model: str = Field(default="", max_length=160)
    controller_model: str = Field(default="", max_length=160)
    battery_voltage_class: str = Field(default="", max_length=60)
    error_code: str = Field(default="", max_length=80)
    symptom: str = Field(min_length=3, max_length=3000)
    measurements: str = Field(default="", max_length=3000)
    skill_level: Literal["beginner", "technician"] = "beginner"

@app.middleware("http")
async def basic_security_headers_and_limits(request: Request, call_next):
    if request.url.path == "/v1/diagnose" and request.method == "POST":
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with _rate_lock:
            bucket = _request_times[client_ip]
            while bucket and now - bucket[0] > WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= RATE_LIMIT:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please try again later."},
                    headers={"Retry-After": str(max(1, int(WINDOW_SECONDS - (now - bucket[0]))))},
                )
            bucket.append(now)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/health")
def health():
    provider = "gemini" if GEMINI_API_KEY else ("openai" if OPENAI_API_KEY else "none")
    return {"status": "ok", "ai_configured": provider != "none", "provider": provider}

def make_prompt(req: DiagnosisRequest) -> str:
    lang = {"hu": "Hungarian", "en": "English", "de": "German"}[req.language]
    return f"""You are the E-Bike Doctor diagnostic assistant for electric bicycles and light electric vehicles. Respond in {lang}.
Use careful technician-grade reasoning. Separate confirmed facts, likely causes, and unknowns. Never invent a manufacturer fault-code definition, pinout, wire colour, or voltage specification. If exact model documentation is unavailable, explicitly say so and give only safe generic checks. Ask for missing information when needed. Provide a numbered next-step sequence; for each measurement specify meter mode, safe measurement points, expected range only when well-supported, and interpretation. Warn before live electrical tests. Never recommend bypassing a BMS, defeating brake cutoffs, shorting battery cells, or applying pack voltage to signal/data pins. For swollen, hot, damaged, hissing or smoking batteries, tell the user to stop, isolate safely, and seek qualified service. Do not claim to control or read the bike electronically; this is text-based advice only. Avoid requesting names, addresses, phone numbers or other customer personal data.

System facts:
Brand: {req.brand}
Motor: {req.motor_model}
Display: {req.display_model}
Controller: {req.controller_model}
Battery voltage class: {req.battery_voltage_class}
Error code: {req.error_code}
Symptom: {req.symptom}
Measurements: {req.measurements}
User skill: {req.skill_level}

Return: (1) brief assessment and confidence, (2) safety warning if relevant, (3) next best test, (4) possible interpretations, (5) information needed to confirm. Do not imply a diagnosis is certain without evidence."""

def call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini is not configured")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1400},
    }
    data = json.dumps(payload).encode("utf-8")
    request = URLRequest(url, data=data, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}, method="POST")
    try:
        with urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Do not log the URL: the API key is a query parameter.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
        except Exception:
            detail = ""
        logger.error("Gemini HTTP error status=%s response=%s", exc.code, detail)
        raise RuntimeError(f"Gemini provider returned HTTP {exc.code}") from None
    except (URLError, TimeoutError) as exc:
        logger.error("Gemini connection error type=%s", type(exc).__name__)
        raise RuntimeError("Gemini connection failed or timed out") from None
    candidates = result.get("candidates") or []
    if not candidates:
        logger.error("Gemini response had no candidates; prompt feedback=%s", result.get("promptFeedback"))
        raise RuntimeError("Gemini returned no response candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "\n".join(p.get("text", "") for p in parts if p.get("text"))
    if not answer.strip():
        raise RuntimeError("Gemini returned empty text")
    return answer.strip()

def call_openai(prompt: str) -> str:
    # Optional fallback for deployments which still have an OpenAI API key.
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=1)
    response = client.responses.create(model=OPENAI_MODEL, input=prompt)
    return response.output_text

@app.post("/v1/diagnose")
def diagnose(req: DiagnosisRequest):
    if not GEMINI_API_KEY and not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI provider is not configured on the server")
    prompt = make_prompt(req)
    provider = "gemini" if GEMINI_API_KEY else "openai"
    try:
        answer = call_gemini(prompt) if GEMINI_API_KEY else call_openai(prompt)
        model = GEMINI_MODEL if provider == "gemini" else OPENAI_MODEL
        return {
            "answer": answer,
            "model": model,
            "provider": provider,
            "disclaimer": "AI advice can be wrong. Confirm values with the exact manufacturer's service documentation."
        }
    except Exception as exc:
        # Keep secrets out of client responses. Provider status/type is available in server logs.
        logger.error("AI diagnosis failed provider=%s error_type=%s message=%s",
                     provider, type(exc).__name__, str(exc)[:300])
        raise HTTPException(status_code=502, detail="AI provider request failed; check server logs and provider quota") from None

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)


@app.post("/v1/chat")
def chat(req: ChatRequest):
    if not GEMINI_API_KEY and not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AI provider is not configured on the server",
        )

    lang = {
        "hu": "Hungarian",
        "en": "English",
        "de": "German",
    }[req.language]

    conversation = "\n".join(
        ("User" if msg.role == "user" else "Assistant")
        + ": "
        + msg.content
        for msg in req.messages
    )

    prompt = f"""You are E-Bike Doctor, a careful professional
electric bicycle and scooter repair assistant.
Reply in {lang}. Use the conversation history to answer
follow-up questions. Ask for missing model details when needed.
Never invent fault-code meanings, pinouts or specifications.
Never recommend bypassing a BMS or safety cutoffs.
Warn about dangerous battery conditions.

Conversation:
{conversation}

Reply to the latest user message with useful, clear steps."""

    try:
        if GEMINI_API_KEY:
            answer = call_gemini(prompt)
            provider = "gemini"
            model = GEMINI_MODEL
        else:
            answer = call_openai(prompt)
            provider = "openai"
            model = OPENAI_MODEL

        return {
            "answer": answer,
            "provider": provider,
            "model": model,
        }
    except Exception as exc:
        logger.error(
            "AI chat failed provider=%s error_type=%s message=%s",
            "gemini" if GEMINI_API_KEY else "openai",
            type(exc).__name__,
            str(exc)[:300],
        )
        raise HTTPException(
            status_code=502,
            detail="AI chat request failed; check server logs",
        ) from None
