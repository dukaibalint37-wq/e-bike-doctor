"""E-Bike Doctor AI API 2.1 (FastAPI on Render).

New in 2.1 (speed + business model):
- SSE streaming endpoints (/v1/chat/stream, /v1/smart-diagnose/stream, /v1/diagnose/stream)
- persistent keep-alive connection to Gemini, two model tiers (fast / quality), compact prompts
- verified answer cache for repeated generic questions
- server-side daily AI quota (50 / install / day) with signed install tokens and abuse limits
- /v1/metrics: server time, Gemini first-byte time, total time, tokens, error rate, estimated cost
All 2.0 endpoints keep their request/response format (backward compatible).
Provider keys stay server-side.
"""
import base64
import binascii
import json
import logging
import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import ai_core
import cache as answer_cache
import gemini_client
import metrics as metrics_mod
import quota as quota_mod
from store import make_store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ebike-doctor")

app = FastAPI(title="E-Bike Doctor AI API", version=ai_core.BACKEND_VERSION)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
BURST_LIMIT = int(os.getenv("RATE_LIMIT_PER_10_MINUTES", "30"))
WINDOW_SECONDS = 600
LIMITED_PREFIXES = ("/v1/diagnose", "/v1/vision-diagnose", "/v1/chat", "/v1/smart-diagnose", "/v1/register")
ENDPOINTS = ["/health", "/v1/register", "/v1/quota", "/v1/diagnose", "/v1/diagnose/stream", "/v1/smart-diagnose",
             "/v1/smart-diagnose/stream", "/v1/vision-diagnose", "/v1/chat", "/v1/chat/stream", "/v1/metrics"]
_request_times: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()

STORE = make_store()
QUOTA = quota_mod.QuotaManager(STORE)
CACHE = answer_cache.AnswerCache(STORE)
METRICS = metrics_mod.Metrics()


def provider_name() -> str:
    return "gemini" if ai_core.gemini_key() else ("openai" if OPENAI_API_KEY else "none")


def client_ip(request: Request) -> str:
    """Render forwards the client IP in X-Forwarded-For; TRUST_PROXY_HOPS selects the entry added by the proxy."""
    fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    hops = max(1, int(os.getenv("TRUST_PROXY_HOPS", "1")))
    if fwd:
        return fwd[-hops] if len(fwd) >= hops else fwd[0]
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def security_headers_and_burst_limit(request: Request, call_next):
    t0 = time.monotonic()
    if request.method == "POST" and request.url.path.startswith(LIMITED_PREFIXES):
        ip = client_ip(request)
        now = time.monotonic()
        with _rate_lock:
            bucket = _request_times[ip]
            while bucket and now - bucket[0] > WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= BURST_LIMIT:
                return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Please try again later.", "reason": "burst"},
                                    headers={"Retry-After": str(max(1, int(WINDOW_SECONDS - (now - bucket[0]))))})
            bucket.append(now)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Server-Timing"] = f"app;dur={(time.monotonic() - t0) * 1000:.0f}"
    return response


# ----------------------------------------------------------------------------- info / auth
@app.get("/")
def root():
    return {"service": "E-Bike Doctor AI API", "version": ai_core.BACKEND_VERSION, "endpoints": ENDPOINTS}


@app.get("/health")
def health():
    provider = provider_name()
    return {"status": "ok", "ai_configured": provider != "none", "provider": provider, "version": ai_core.BACKEND_VERSION,
            "models": {"fast": gemini_client.models_for("fast"), "quality": gemini_client.models_for("quality")} if provider == "gemini" else [OPENAI_MODEL],
            "daily_limit": QUOTA.daily_limit, "store": STORE.kind, "streaming": True, "endpoints": ENDPOINTS}


class RegisterRequest(BaseModel):
    install_id: str = Field(min_length=16, max_length=64)


@app.post("/v1/register")
def register(req: RegisterRequest, request: Request):
    try:
        token = QUOTA.register(req.install_id, client_ip(request))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid install id") from None
    if token is None:
        raise HTTPException(status_code=429, detail="Too many registrations from this network today", headers={"Retry-After": str(quota_mod.seconds_to_midnight())})
    return {"token": token, "daily_limit": QUOTA.daily_limit}


@app.get("/v1/quota")
def quota_status(request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return {"daily_limit": QUOTA.daily_limit, "remaining": QUOTA.remaining(x_ebd_token, client_ip(request)),
            "resets_in_s": quota_mod.seconds_to_midnight()}


@app.get("/v1/metrics")
def metrics_view(x_admin_token: Optional[str] = Header(default=None)):
    admin = os.getenv("ADMIN_TOKEN", "").strip()
    if not admin or x_admin_token != admin:
        raise HTTPException(status_code=404, detail="Not Found")
    snap = METRICS.snapshot()
    snap["store"] = STORE.kind
    snap["store_errors"] = getattr(STORE, "errors", 0)
    return snap


# ----------------------------------------------------------------------------- quota helpers
QUOTA_MESSAGES = {
    "daily_quota": "Daily AI question limit reached",
    "ip_limit": "Daily AI limit for this network reached",
    "global_limit": "AI service daily capacity reached",
    "cost_limit": "AI service daily budget reached",
}


def consume_quota(token, ip):
    d = QUOTA.check_and_consume(token, ip)
    if not d.allowed:
        status = 503 if d.reason in ("global_limit", "cost_limit") else 429
        raise HTTPException(status_code=status, detail=QUOTA_MESSAGES.get(d.reason, "Limit reached"),
                            headers={"Retry-After": str(d.retry_after), "X-Quota-Reason": d.reason, "X-Quota-Remaining": "0",
                                     "X-Quota-Limit": str(d.limit)})
    return d


def quota_headers(d):
    return {"X-Quota-Remaining": str(d.remaining), "X-Quota-Limit": str(d.limit)} if d else {}


def provider_http_error(exc):
    if exc.status == 429:
        return HTTPException(status_code=429, detail="AI provider quota reached; try again later", headers={"Retry-After": "60", "X-Quota-Reason": "provider"})
    if exc.status == 503:
        return HTTPException(status_code=503, detail="AI provider is not configured on the server")
    return HTTPException(status_code=502, detail="AI provider request failed; check server logs and provider quota")


# ----------------------------------------------------------------------------- core execution
def _call_openai(prompt: str) -> str:
    from openai import OpenAI  # optional fallback for existing deployments
    c = OpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=1)
    return c.responses.create(model=OPENAI_MODEL, input=prompt).output_text


def run_text(endpoint, request, token, prompt_parts, tier, max_tokens, cache_key=None):
    """Non-streaming execution with cache + quota + metrics. Returns JSONResponse (2.0-compatible body)."""
    t0 = time.monotonic()
    provider = provider_name()
    if provider == "none":
        METRICS.record(endpoint, False)
        raise HTTPException(status_code=503, detail="AI provider is not configured on the server")
    if cache_key:
        hit = CACHE.get(cache_key)
        if hit:
            METRICS.record(endpoint, True, server_ms=(time.monotonic() - t0) * 1000, cached=True)
            return JSONResponse({"answer": hit["answer"], "provider": provider, "model": hit.get("model", ""), "cached": True,
                                 "disclaimer": "Cached verified answer. Confirm values with manufacturer documentation."},
                                headers={"X-Cache": "HIT"})
    ip = client_ip(request)
    d = consume_quota(token, ip)
    try:
        if provider == "gemini":
            r = gemini_client.client().generate(prompt_parts, tier=tier, max_tokens=max_tokens)
        else:
            prompt = "\n".join(p.get("text", "") for p in prompt_parts)
            r = {"text": _call_openai(prompt), "model": OPENAI_MODEL, "usage": {}, "ttfb_ms": None, "total_ms": None}
    except gemini_client.ProviderError as exc:
        QUOTA.refund(d, ip)
        METRICS.record(endpoint, False, server_ms=(time.monotonic() - t0) * 1000)
        logger.error("AI request failed endpoint=%s status=%s msg=%s", endpoint, exc.status, str(exc)[:200])
        raise provider_http_error(exc) from None
    except Exception as exc:  # never leak secrets
        QUOTA.refund(d, ip)
        METRICS.record(endpoint, False, server_ms=(time.monotonic() - t0) * 1000)
        logger.error("AI request failed endpoint=%s error_type=%s", endpoint, type(exc).__name__)
        raise HTTPException(status_code=502, detail="AI provider request failed; check server logs") from None
    QUOTA.add_cost(metrics_mod.estimate_cost_usd(r["usage"]))
    METRICS.record(endpoint, True, server_ms=(time.monotonic() - t0) * 1000, gemini_ttfb_ms=r["ttfb_ms"], total_ms=r["total_ms"],
                   usage=r["usage"], model=r["model"])
    if cache_key:
        CACHE.put(cache_key, r["text"], r["model"])
    return JSONResponse({"answer": r["text"], "provider": provider, "model": r["model"], "cached": False,
                         "disclaimer": "AI advice can be wrong. Confirm values with the exact manufacturer's service documentation."},
                        headers={**quota_headers(d), "X-Cache": "MISS" if cache_key else "BYPASS"})


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def run_stream(endpoint, request, token, prompt_parts, tier, max_tokens, cache_key=None):
    """SSE streaming. Errors before the first token become normal HTTP errors (quota refunded)."""
    t0 = time.monotonic()
    if not ai_core.gemini_key():
        METRICS.record(endpoint, False)
        raise HTTPException(status_code=503, detail="Streaming requires GEMINI_API_KEY on the server")
    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if cache_key:
        hit = CACHE.get(cache_key)
        if hit:
            METRICS.record(endpoint, True, server_ms=(time.monotonic() - t0) * 1000, cached=True)

            def cached_gen():
                yield sse("meta", {"cached": True, "quota_remaining": None})
                yield sse("delta", {"t": hit["answer"]})
                yield sse("done", {"model": hit.get("model", ""), "cached": True, "server_ms": round((time.monotonic() - t0) * 1000)})
            return StreamingResponse(cached_gen(), media_type="text/event-stream", headers={**sse_headers, "X-Cache": "HIT"})
    ip = client_ip(request)
    d = consume_quota(token, ip)
    gen = gemini_client.client().stream(prompt_parts, tier=tier, max_tokens=max_tokens)
    try:
        first = next(gen)  # blocks until the first token -> provider errors are reported as HTTP status codes
    except gemini_client.ProviderError as exc:
        QUOTA.refund(d, ip)
        METRICS.record(endpoint, False, server_ms=(time.monotonic() - t0) * 1000)
        raise provider_http_error(exc) from None
    except StopIteration:
        QUOTA.refund(d, ip)
        METRICS.record(endpoint, False)
        raise HTTPException(status_code=502, detail="AI provider returned no content") from None

    def body():
        parts = []
        ttfb = first[1].get("ttfb_ms") if first[0] == "start" else None
        yield sse("meta", {"cached": False, "quota_remaining": d.remaining, "quota_limit": d.limit, "model": first[1].get("model") if first[0] == "start" else ""})
        ok = False
        try:
            for ev, payload in gen:
                if ev == "delta":
                    parts.append(payload)
                    yield sse("delta", {"t": payload})
                elif ev == "done":
                    ok = True
                    QUOTA.add_cost(metrics_mod.estimate_cost_usd(payload.get("usage")))
                    METRICS.record(endpoint, True, server_ms=(time.monotonic() - t0) * 1000, gemini_ttfb_ms=ttfb,
                                   total_ms=payload.get("total_ms"), usage=payload.get("usage"), model=payload.get("model", ""))
                    if cache_key:
                        CACHE.put(cache_key, "".join(parts), payload.get("model", ""))
                    yield sse("done", {"model": payload.get("model"), "gemini_ttfb_ms": round(ttfb or 0), "total_ms": round(payload.get("total_ms") or 0),
                                       "tokens": (payload.get("usage") or {}).get("totalTokenCount")})
        except gemini_client.ProviderError as exc:
            METRICS.record(endpoint, False, server_ms=(time.monotonic() - t0) * 1000)
            yield sse("error", {"detail": "AI stream interrupted", "status": exc.status})
        finally:
            if not ok:
                gen.close()
    return StreamingResponse(body(), media_type="text/event-stream", headers={**sse_headers, **quota_headers(d), "X-Cache": "MISS" if cache_key else "BYPASS"})


# ----------------------------------------------------------------------------- request models
class DiagnosisRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    vehicle_type: Literal["ebike", "scooter"] = "ebike"
    brand: str = Field(default="", max_length=120)
    motor_model: str = Field(default="", max_length=160)
    display_model: str = Field(default="", max_length=160)
    controller_model: str = Field(default="", max_length=160)
    battery_voltage_class: str = Field(default="", max_length=60)
    error_code: str = Field(default="", max_length=80)
    symptom: str = Field(min_length=3, max_length=3000)
    measurements: str = Field(default="", max_length=3000)
    skill_level: Literal["beginner", "technician"] = "beginner"


def diagnose_cache_key(req: DiagnosisRequest):
    if req.measurements.strip() or not answer_cache.is_cacheable_question(req.symptom):
        return None
    return answer_cache.make_key("diagnose", req.language, req.vehicle_type, req.brand, req.motor_model, req.display_model,
                                 req.controller_model, req.battery_voltage_class, req.error_code, req.symptom, req.skill_level)


@app.post("/v1/diagnose")
def diagnose(req: DiagnosisRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_text("diagnose", request, x_ebd_token, [{"text": ai_core.diagnose_prompt(req.model_dump())}], "quality", 900, diagnose_cache_key(req))


@app.post("/v1/diagnose/stream")
def diagnose_stream(req: DiagnosisRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_stream("diagnose_stream", request, x_ebd_token, [{"text": ai_core.diagnose_prompt(req.model_dump())}], "quality", 900, diagnose_cache_key(req))


class SmartDiagnoseRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    case: str = Field(min_length=10, max_length=4000)


@app.post("/v1/smart-diagnose")
def smart_diagnose(req: SmartDiagnoseRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_text("smart", request, x_ebd_token, [{"text": ai_core.smart_prompt(req.language, req.case)}], "fast", 700)


@app.post("/v1/smart-diagnose/stream")
def smart_diagnose_stream(req: SmartDiagnoseRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_stream("smart_stream", request, x_ebd_token, [{"text": ai_core.smart_prompt(req.language, req.case)}], "fast", 700)


class VisionDiagnosisRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    image_base64: str = Field(min_length=100, max_length=7_000_000)
    image2_base64: Optional[str] = Field(default=None, max_length=7_000_000)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    mode: Literal["general", "code", "display", "label", "motor", "connector", "damage", "compare"] = "general"
    brand: str = Field(default="", max_length=120)
    motor_model: str = Field(default="", max_length=160)
    display_model: str = Field(default="", max_length=160)
    symptom: str = Field(default="", max_length=2000)


def _check_image(b64: str, mime: str) -> None:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="Invalid image encoding") from None
    if len(raw) > 5_000_000 or len(raw) < 100:
        raise HTTPException(status_code=413, detail="Image size must be between 100 bytes and 5 MB")
    ok = {"image/jpeg": raw.startswith(b"\xff\xd8\xff"), "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
          "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}[mime]
    if not ok:
        raise HTTPException(status_code=422, detail="Image data does not match declared format")


@app.post("/v1/vision-diagnose")
def vision_diagnose(req: VisionDiagnosisRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    """Photo analysis (never cached: images may contain personal data)."""
    if not ai_core.gemini_key():
        raise HTTPException(status_code=503, detail="Camera AI requires GEMINI_API_KEY configured on the backend")
    _check_image(req.image_base64, req.mime_type)
    parts = [{"text": ai_core.vision_prompt(req.model_dump(exclude={"image_base64", "image2_base64"}))},
             {"inline_data": {"mime_type": req.mime_type, "data": req.image_base64}}]
    if req.mode == "compare":
        if not req.image2_base64:
            raise HTTPException(status_code=422, detail="Compare mode requires image2_base64")
        _check_image(req.image2_base64, req.mime_type)
        parts.append({"inline_data": {"mime_type": req.mime_type, "data": req.image2_base64}})
    resp = run_text("vision", request, x_ebd_token, parts, "quality", 1000)
    return resp


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    language: Literal["hu", "en", "de"] = "hu"
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)


def chat_cache_key(req: ChatRequest):
    users = [m for m in req.messages if m.role == "user"]
    if len(users) != 1 or not answer_cache.is_cacheable_question(users[0].content):
        return None  # follow-ups depend on the conversation -> never cached
    return answer_cache.make_key("chat", req.language, users[0].content)


def chat_parts(req: ChatRequest):
    # Only the last 10 messages are sent: older history adds tokens (latency + cost) with little value.
    msgs = [m.model_dump() for m in req.messages][-10:]
    return [{"text": ai_core.chat_prompt(req.language, msgs)}]


@app.post("/v1/chat")
def chat(req: ChatRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_text("chat", request, x_ebd_token, chat_parts(req), "fast", 800, chat_cache_key(req))


@app.post("/v1/chat/stream")
def chat_stream(req: ChatRequest, request: Request, x_ebd_token: Optional[str] = Header(default=None)):
    return run_stream("chat_stream", request, x_ebd_token, chat_parts(req), "fast", 800, chat_cache_key(req))
