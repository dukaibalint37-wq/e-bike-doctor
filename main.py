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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import ai_core
import cache as answer_cache
import gemini_client
import metrics as metrics_mod
import quota as quota_mod
import licensing as licensing_mod
import admin_auth
import analytics
import interest as interest_mod
import mailer as mailer_mod
import s3presign
import shop as shop_mod
from store import make_store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ebike-doctor")

app = FastAPI(title="E-Bike Doctor AI API", version=ai_core.BACKEND_VERSION)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
BURST_LIMIT = int(os.getenv("RATE_LIMIT_PER_10_MINUTES", "30"))
WINDOW_SECONDS = 600
LIMITED_PREFIXES = ("/v1/diagnose", "/v1/vision-diagnose", "/v1/chat", "/v1/smart-diagnose", "/v1/register", "/v1/report", "/v1/license", "/v1/shop",
                    "/v1/interest", "/v1/admin/login")
ENDPOINTS = ["/health", "/v1/register", "/v1/quota", "/v1/diagnose", "/v1/diagnose/stream", "/v1/smart-diagnose",
             "/v1/smart-diagnose/stream", "/v1/vision-diagnose", "/v1/chat", "/v1/chat/stream", "/v1/metrics", "/v1/report",
             "/v1/license/activate", "/v1/license/refresh", "/v1/license/deactivate", "/v1/shop/lemonsqueezy/webhook", "/v1/shop/download",
             "/v1/shop/download-link", "/v1/shop/resend", "/v1/interest", "/v1/app/update"]
# Browser access for the website forms only (the apps are not browsers and are unaffected). Empty = no CORS at all.
CORS_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
                       allow_credentials=False, expose_headers=["X-License-Error"], max_age=3600)
_request_times: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()

STORE = make_store()
# Licences, purchases and mails: strict persistent store (no silent memory fallback), see licensing.StrictStore.
LIC_STORE = licensing_mod.StrictStore(STORE)
MAILER = mailer_mod.Mailer(LIC_STORE)
QUOTA = quota_mod.QuotaManager(STORE)
LICENSES = licensing_mod.LicenseService(LIC_STORE, QUOTA.signer)


def _resolve_ai_subject(iid):
    try:
        return LICENSES.resolve_ai_subject(iid)
    except licensing_mod.LicenseError:
        return None   # storage outage: deny this AI request (no cost), the licence itself is untouched


QUOTA.subject_resolver = _resolve_ai_subject
SHOP = shop_mod.Shop(LIC_STORE, LICENSES, MAILER)
INTEREST = interest_mod.InterestList(LIC_STORE, MAILER)
ADMIN_AUTH = admin_auth.AdminAuth(LIC_STORE)
VISITS = analytics.Visits(STORE)
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
            "daily_limit": QUOTA.daily_limit, "store": STORE.kind, "streaming": True, "endpoints": ENDPOINTS,
            # 3.0: the commercial apps use AI only when the operator switched it on (AI_PUBLIC=1 after billing approval)
            "ai_public": os.getenv("AI_PUBLIC", "0").strip() == "1", "license_server": LICENSES.lease.available}


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


# ----------------------------------------------------------------------------- AI content reports (Google Play AI policy)
REPORT_TTL = 90 * 86_400
REPORTS_PER_IP_PER_DAY = 20


class ReportRequest(BaseModel):
    """In-app report of an AI answer (Google Play 'AI-Generated Content' policy). No photo, no user identity is stored."""
    kind: Literal["chat", "vision", "smart", "diagnose"] = "chat"
    language: Literal["hu", "en", "de"] = "hu"
    reason: Literal["offensive", "harmful", "incorrect", "other"]
    comment: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=2000)   # the reported AI answer text only


def _admin_ok(token):
    admin = os.getenv("ADMIN_TOKEN", "").strip()
    return bool(admin) and token == admin


@app.post("/v1/report")
def report_ai_content(req: ReportRequest, request: Request):
    day = quota_mod.today()
    if STORE.incr(f"rep_ip:{day}:{quota_mod.ip_hash(client_ip(request))}", 86_400 + 3600) > REPORTS_PER_IP_PER_DAY:
        raise HTTPException(status_code=429, detail="Too many reports today", headers={"Retry-After": str(quota_mod.seconds_to_midnight())})
    n = STORE.incr(f"rep_n:{day}", REPORT_TTL)
    STORE.set(f"rep:{day}:{n}", {"kind": req.kind, "language": req.language, "reason": req.reason,
                                 "comment": req.comment.strip()[:500], "excerpt": req.excerpt.strip()[:2000], "t": int(time.time())}, REPORT_TTL)
    logger.warning("AI content report day=%s n=%s kind=%s reason=%s", day, n, req.kind, req.reason)
    return {"ok": True, "id": f"{day}-{n}"}


@app.get("/v1/reports")
def reports_view(day: Optional[str] = None, x_admin_token: Optional[str] = Header(default=None)):
    if not _admin_ok(x_admin_token):
        raise HTTPException(status_code=404, detail="Not Found")
    day = day if day and day.isdigit() and len(day) == 8 else quota_mod.today()
    count = int(STORE.get(f"rep_n:{day}") or 0)
    items = [STORE.get(f"rep:{day}:{i}") for i in range(max(1, count - 199), count + 1)] if count else []
    return {"day": day, "count": count, "reports": [x for x in items if x]}


# ----------------------------------------------------------------------------- quota helpers
QUOTA_MESSAGES = {
    "daily_quota": "Daily AI question limit reached",
    "ip_limit": "Daily AI limit for this network reached",
    "global_limit": "AI service daily capacity reached",
    "cost_limit": "AI service budget reached",
    "license_invalid": "Licence not active on this device",
    "license_required": "An activated licence is required for AI",
    "token_required": "App registration required for AI",
}


def consume_quota(token, ip):
    d = QUOTA.check_and_consume(token, ip)
    if not d.allowed:
        status = 503 if d.reason in ("global_limit", "cost_limit") else 401 if d.reason in ("license_invalid", "license_required", "token_required") else 429
        raise HTTPException(status_code=status, detail=QUOTA_MESSAGES.get(d.reason, "Limit reached"),
                            headers={"Retry-After": str(d.retry_after), "X-Quota-Reason": d.reason, "X-Quota-Remaining": "0",
                                     "X-Quota-Limit": str(d.limit)})
    return d


def access_gate(token, request):
    """Licence / token gate BEFORE the answer cache: in token/license mode a cached answer must not reach an unlicensed client.
    Does not consume quota (consume_quota does that once the request really goes to the provider)."""
    _subject, reason = QUOTA.subject_for(token, client_ip(request))
    if reason:
        raise HTTPException(status_code=401, detail=QUOTA_MESSAGES.get(reason, "Not allowed"), headers={"X-Quota-Reason": reason})


def quota_headers(d):
    return {"X-Quota-Remaining": str(d.remaining), "X-Quota-Limit": str(d.limit)} if d else {}


def provider_http_error(exc):
    if exc.status == 429:
        return HTTPException(status_code=429, detail="AI provider quota reached; try again later", headers={"Retry-After": "60", "X-Quota-Reason": "provider"})
    if exc.status == 503:
        return HTTPException(status_code=503, detail="AI provider is not configured on the server")
    if exc.status in (402, 403):
        # prepaid balance used up / billing disabled at the provider -> polite, non-technical message in the apps
        logger.error("AI provider billing problem status=%s", exc.status)
        return HTTPException(status_code=503, detail="AI service temporarily unavailable (provider balance)", headers={"X-Quota-Reason": "provider_billing", "Retry-After": "3600"})
    return HTTPException(status_code=502, detail="AI provider request failed; check server logs and provider quota")


# ----------------------------------------------------------------------------- core execution
def _call_openai(prompt: str) -> str:
    from openai import OpenAI  # optional fallback for existing deployments
    c = OpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=1)
    return c.responses.create(model=OPENAI_MODEL, input=prompt).output_text


def run_text(endpoint, request, token, prompt_parts, tier, max_tokens, cache_key=None):
    """Non-streaming execution with cache + quota + metrics. Returns JSONResponse (2.0-compatible body)."""
    t0 = time.monotonic()
    access_gate(token, request)
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
    QUOTA.add_cost(metrics_mod.estimate_cost_usd(r["usage"]), _tokens(r["usage"]))
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
    access_gate(token, request)
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
                    QUOTA.add_cost(metrics_mod.estimate_cost_usd(payload.get("usage")), _tokens(payload.get("usage")))
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


# ============================================================================= 3.0 commercial: licences, shop, updates, admin
def _tokens(usage):
    try:
        return int((usage or {}).get("totalTokenCount") or (usage or {}).get("total_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _cost_warning(period, level, spent, cap):
    logger.warning("COST WARNING %s reached %s%% (%.4f / %.2f USD)", period, level, spent, cap)
    to = os.getenv("ADMIN_ALERT_EMAIL", "").strip()
    MAILER.send(to, f"E-Bike Doctor AI cost warning {level}% ({period})",
                f"AI spend {spent:.4f} USD of cap {cap:.2f} USD ({period}). At 100% the server stops new AI requests until the period resets.", kind="cost")


QUOTA.on_cost_warning = _cost_warning


def _lic_error(exc):
    return HTTPException(status_code=exc.status, detail=exc.message, headers={"X-License-Error": exc.code})


class LicenseActivateRequest(BaseModel):
    license_key: str = Field(min_length=10, max_length=60)
    product: Literal["EBD-WIN", "EBD-AND"]
    device_id: str = Field(min_length=64, max_length=64)        # salted SHA-256 computed on the device
    device_name: str = Field(default="", max_length=80)
    app_version: str = Field(default="", max_length=20)
    transfer: bool = False


class LicenseKeyRequest(BaseModel):
    license_key: str = Field(min_length=10, max_length=60)
    product: Literal["EBD-WIN", "EBD-AND"] = "EBD-WIN"
    device_id: str = Field(default="", max_length=64)
    app_version: str = Field(default="", max_length=20)


@app.post("/v1/license/activate")
def license_activate(req: LicenseActivateRequest):
    try:
        return LICENSES.activate(req.license_key, req.product, req.device_id, req.device_name, req.app_version, req.transfer)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


@app.post("/v1/license/refresh")
def license_refresh(req: LicenseKeyRequest):
    try:
        return LICENSES.refresh(req.license_key, req.product, req.device_id, req.app_version)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


@app.post("/v1/license/deactivate")
def license_deactivate(req: LicenseKeyRequest):
    try:
        return LICENSES.deactivate(req.license_key, req.device_id)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


@app.post("/v1/shop/lemonsqueezy/webhook")
async def lemonsqueezy_webhook(request: Request):
    raw = await request.body()
    try:
        return SHOP.handle_webhook(raw, request.headers.get("x-signature", ""))
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


@app.get("/v1/shop/download")
def shop_download(t: str = ""):
    try:
        url = SHOP.resolve_download(t)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None
    return RedirectResponse(url, status_code=302)


@app.post("/v1/shop/download-link")
def shop_download_link(req: LicenseKeyRequest):
    try:
        return {"url": SHOP.new_download_link_for_key(req.license_key)}
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


class ResendRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    lang: Literal["hu", "de", "en"] = "en"


@app.post("/v1/shop/resend")
def shop_resend(req: ResendRequest, request: Request):
    """Website form 'I lost my key / download link': mails the licences to the purchase address only (generic answer)."""
    try:
        return SHOP.resend(req.email, req.lang, quota_mod.ip_hash(client_ip(request)))
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


# ---------------------------------------------------------------------------- Smart Hardware interest list (double opt-in)
class InterestRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    name: str = Field(default="", max_length=80)
    lang: Literal["hu", "de", "en"] = "en"
    role: str = Field(default="", max_length=20)
    interests: list[str] = Field(default_factory=list, max_length=10)
    consent: bool = False
    website: str = Field(default="", max_length=200)      # honeypot – must stay empty


PAGE_CSP = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}


@app.post("/v1/interest")
def interest_subscribe(req: InterestRequest, request: Request):
    try:
        return INTEREST.subscribe(req.email, req.lang, req.interests, req.role, req.consent, quota_mod.ip_hash(client_ip(request)),
                                  honeypot=req.website, name=req.name)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None


def _interest_page(kind, lang, status=200, form_action=None):
    return HTMLResponse(interest_mod.page(kind, lang, form_action), status_code=status, headers=PAGE_CSP)


@app.get("/v1/interest/confirm")
def interest_confirm(t: str = ""):
    try:
        lang, result = INTEREST.confirm(t)
    except licensing_mod.LicenseError:
        return _interest_page("invalid", "en", 503)
    return _interest_page(result, lang, 200 if result == "confirmed" else 404)


@app.get("/v1/interest/unsubscribe")
def interest_unsubscribe_page(t: str = "", lang: str = "en"):
    """GET only shows a confirmation button (mail scanners prefetch links); the POST below does the deletion."""
    return _interest_page("unsub_ask", lang, form_action="/v1/interest/unsubscribe?t=" + t)


@app.post("/v1/interest/unsubscribe")
def interest_unsubscribe(t: str = ""):
    """Also the RFC 8058 one-click target (List-Unsubscribe-Post)."""
    try:
        lang, result = INTEREST.unsubscribe(t)
    except licensing_mod.LicenseError:
        return _interest_page("invalid", "en", 503)
    return _interest_page(result, lang or "en", 200 if result == "unsubscribed" else 404)


UPDATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "updates")


@app.get("/v1/app/update")
def app_update(product: str = "EBD-WIN"):
    """Signed update manifest (signature made offline with the update-signing key; the server only serves the file)."""
    name = {"EBD-WIN": "windows.json", "EBD-AND": "android-web.json"}.get(product)
    path = os.path.join(UPDATE_DIR, name) if name else ""
    if not name or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No update channel")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------- admin (X-Admin-Token)
def _admin(token, request=None):
    """Header token (automation) or the 2FA web session cookie (+ CSRF header for state changes). Unknown -> 404."""
    cookie = request.cookies.get(admin_auth.COOKIE) if request is not None and hasattr(request, "cookies") else None
    method = getattr(request, "method", "GET") if request is not None else "GET"
    csrf = request.headers.get("x-ebd-admin", "") if request is not None else ""
    if not ADMIN_AUTH.allowed(token, cookie, method, csrf):
        raise HTTPException(status_code=404, detail="Not Found")


class AdminCreateRequest(BaseModel):
    product: Literal["EBD-WIN", "EBD-AND"]
    email: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=120)
    owner: bool = False
    test: bool = False
    note: str = Field(default="", max_length=120)


class AdminActionRequest(BaseModel):
    reason: str = Field(default="admin", max_length=120)


@app.get("/admin")
def admin_page():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html"), encoding="utf-8") as f:
        page = f.read()
    import hashlib as _hl
    import re as _re
    hashes = " ".join("'sha256-" + base64.b64encode(_hl.sha256(s.encode()).digest()).decode() + "'"
                      for s in _re.findall(r"<script>(.*?)</script>", page, _re.S))
    return HTMLResponse(page, headers={
        "Content-Security-Policy": f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; img-src 'self' blob: data:; "
                                   "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Robots-Tag": "noindex, nofollow"})


@app.get("/v1/admin/licenses")
def admin_licenses(q: str = "", x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    return {"licenses": [licensing_mod.public_view(r) for r in LICENSES.search(q)]}


@app.post("/v1/admin/licenses")
def admin_create(req: AdminCreateRequest, x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    try:
        key, rec = LICENSES.create(req.product, email=req.email, name=req.name, owner=req.owner, test=req.test, source="admin:" + req.note)
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None
    return {"license_key": key, "license": licensing_mod.public_view(rec)}


@app.post("/v1/admin/licenses/{lid}/{action}")
def admin_action(lid: str, action: str, req: AdminActionRequest, x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    try:
        if action == "revoke":
            return {"license": licensing_mod.public_view(LICENSES.revoke(lid, req.reason))}
        if action == "reinstate":
            return {"license": licensing_mod.public_view(LICENSES.reinstate(lid))}
        if action == "reset-device":
            return {"license": licensing_mod.public_view(LICENSES.reset_device(lid))}
        if action == "reissue-key":
            key, rec = LICENSES.reissue_key(lid)
            return {"license_key": key, "license": licensing_mod.public_view(rec)}
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None
    raise HTTPException(status_code=404, detail="Unknown action")


@app.get("/v1/admin/stats")
def admin_stats(x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    try:
        interest_stats = INTEREST.stats()
    except licensing_mod.LicenseError:
        interest_stats = {"error": "store_unavailable"}
    return {"licenses": LICENSES.stats(), "shop": SHOP.stats(), "ai_usage": QUOTA.usage(), "ai_access_mode": QUOTA.access_mode(),
            "lease_signing_configured": LICENSES.lease.available, "mail_configured": MAILER.configured, "mail_provider": MAILER.provider,
            "download_bucket_configured": s3presign.configured(), "interest": interest_stats, "cors_origins": CORS_ORIGINS,
            "outbox": STORE.get("mail:outbox") or [], "version": ai_core.BACKEND_VERSION}


@app.get("/v1/admin/interest")
def admin_interest(format: str = "json", x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    rows = INTEREST.entries()
    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "email", "name", "lang", "role", "interests", "status", "created_utc", "confirmed_utc"])
        ts = lambda v: time.strftime("%Y-%m-%d %H:%M", time.gmtime(v)) if v else ""
        for r in rows:
            w.writerow([r["id"], r["email"], r.get("name") or "", r["lang"], r.get("role") or "", " ".join(r.get("interests") or []),
                        r["status"], ts(r.get("created")), ts(r.get("confirmed_at"))])
        return HTMLResponse(buf.getvalue(), headers={"Content-Type": "text/csv; charset=utf-8",
                                                     "Content-Disposition": 'attachment; filename="smart-hardware-interest.csv"'})
    return {"stats": INTEREST.stats(), "entries": rows}


@app.post("/v1/admin/interest/{short_id}/delete")
def admin_interest_delete(short_id: str, x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    return {"deleted": INTEREST.delete(short_id)}


# ============================================================================= 3.2: owner admin (2FA), dashboard, visits
class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=6, max_length=8)


def _cookie(value, max_age):
    return f"{admin_auth.COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; Secure; SameSite=Strict"


@app.post("/v1/admin/login")
def admin_login(req: AdminLoginRequest, request: Request):
    try:
        cookie, exp = ADMIN_AUTH.login(req.password, req.code, quota_mod.ip_hash(client_ip(request)))
    except licensing_mod.LicenseError as exc:
        raise _lic_error(exc) from None
    return JSONResponse({"ok": True, "expires": exp}, headers={"Set-Cookie": _cookie(cookie, exp - int(time.time()))})


@app.post("/v1/admin/logout")
def admin_logout(request: Request):
    cookie = request.cookies.get(admin_auth.COOKIE) if hasattr(request, "cookies") else None
    ADMIN_AUTH.logout(cookie, quota_mod.ip_hash(client_ip(request)))
    return JSONResponse({"ok": True}, headers={"Set-Cookie": _cookie("", 0)})


@app.get("/v1/admin/session")
def admin_session(request: Request = None):
    cookie = request.cookies.get(admin_auth.COOKIE) if request is not None and hasattr(request, "cookies") else None
    return {"configured": ADMIN_AUTH.configured, "ok": bool(cookie and ADMIN_AUTH.session_valid(cookie))}


@app.get("/v1/admin/dashboard")
def admin_dashboard(mode: str = "live", x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    mode = "test" if mode == "test" else "live"
    try:
        interest_stats = INTEREST.stats()
    except licensing_mod.LicenseError:
        interest_stats = {"error": "store_unavailable"}
    shop = SHOP.stats()
    outbox = STORE.get("mail:outbox") or []
    return {"mode": mode, "version": ai_core.BACKEND_VERSION, "generated": int(time.time()),
            "environment": "staging" if os.getenv("STORE_KEY_PREFIX", "").strip() else "production",
            "licenses": LICENSES.stats(mode), "shop": shop, "orders": SHOP.orders(mode)[:150],
            "downloads_daily": SHOP.downloads_daily(30), "visits": VISITS.report(30),
            "ai": {**QUOTA.usage(31), "access_mode": QUOTA.access_mode(), "ai_public": os.getenv("AI_PUBLIC", "0").strip() == "1",
                   "daily_limit_per_licence": QUOTA.daily_limit, "provider": provider_name()},
            "interest": interest_stats,
            "config": {"shop_mode": SHOP.mode, "mail_provider": MAILER.provider, "mail_configured": MAILER.configured,
                       "download_bucket": s3presign.configured(), "lease_signing": LICENSES.lease.available,
                       "mail_health": mailer_mod.health(STORE),
                       "lemonsqueezy_map": bool(shop_mod.product_map()), "webhook_secret": bool(os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "").strip()),
                       "cors_origins": CORS_ORIGINS, "admin_2fa": ADMIN_AUTH.configured,
                       "header_token_enabled": os.getenv("ADMIN_HEADER_TOKEN", "1").strip() != "0", "store": STORE.kind},
            "outbox": list(reversed(outbox if isinstance(outbox, list) else []))[:30], "audit": ADMIN_AUTH.audit_log()[:40]}


class PurgeRequest(BaseModel):
    confirm: str = Field(default="", max_length=60)


@app.post("/v1/admin/test-data/purge")
def admin_purge_test(req: PurgeRequest, x_admin_token: Optional[str] = Header(default=None), request: Request = None):
    _admin(x_admin_token, request)
    if req.confirm != "TESZTADATOK TORLESE":
        raise HTTPException(status_code=422, detail="Confirmation text required")
    r = SHOP.purge_test_data()
    ADMIN_AUTH.audit("purge_test_data", "", f"{len(r['purged_licences'])} licences")
    return r


@app.post("/v1/stats/hit")
async def stats_hit(request: Request):
    """Cookieless page-view beacon (text/plain JSON via navigator.sendBeacon, no preflight). Stores aggregates only."""
    try:
        raw = (await request.body())[:600]
        d = json.loads(raw.decode("utf-8") or "{}")
        VISITS.hit(str(d.get("p", "/")), str(d.get("r", "")), request.headers.get("user-agent", ""),
                   quota_mod.ip_hash(client_ip(request)), os.getenv("SITE_HOST", "").strip().lower())
    except Exception:
        pass                                  # statistics must never produce errors for visitors
    return JSONResponse({}, status_code=202)
