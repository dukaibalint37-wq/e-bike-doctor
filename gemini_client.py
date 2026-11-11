"""Gemini REST client with persistent (keep-alive) HTTPS connections and SSE streaming.

Design goals (speed):
- One reusable TLS connection per worker thread -> no TCP/TLS handshake per request.
- streamGenerateContent?alt=sse -> the first words reach the app while Gemini is still generating.
- Two model tiers: FAST (chat, smart-diagnose) and QUALITY (diagnose, vision).
- Gemini 3.x: thinkingLevel (low by default; "minimal" is rejected by 3.8 Flash), no temperature.
  Gemini 2.x: thinkingBudget 0 + temperature.
- HTTP 404 (retired/renamed model) -> next fallback model. Stale keep-alive socket -> one silent reconnect.
Stdlib only (http.client), so it runs on Render without extra dependencies and is unit-testable.
"""
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time

logger = logging.getLogger("ebike-doctor")

HOST = "generativelanguage.googleapis.com"
API_PREFIX = "/v1beta/models/"


class ProviderError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api_key():
    return os.getenv("GEMINI_API_KEY", "").strip()


def _csv(name, default):
    return [m.strip() for m in os.getenv(name, default).split(",") if m.strip()]


def models_for(tier):
    """Ordered candidate list for a tier. Env overrides keep the backend future-proof."""
    if tier == "fast":
        primary = os.getenv("GEMINI_MODEL_FAST", "gemini-3.5-flash-lite").strip()
        # 2.2: last-resort fallback = the model the live (pre-2.0) server uses according to the owner. Only used when
        # every model before it returns 404 (retired / unknown name), so a wrong model name can never take the AI down.
        fallbacks = _csv("GEMINI_FALLBACK_MODELS_FAST", "gemini-3.8-flash,gemini-2.5-flash")
    else:
        primary = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        fallbacks = _csv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-2.5-flash")
    out = []
    for m in [primary] + fallbacks:
        if m and m not in out:
            out.append(m)
    return out


def generation_config(model, max_tokens, thinking=True):
    """Model-family specific config. thinking=False drops thinkingConfig (used after a 400 retry)."""
    cfg = {"maxOutputTokens": int(max_tokens)}
    if model.startswith("gemini-2"):
        cfg["temperature"] = 0.2
        if thinking and "flash" in model:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
    elif thinking:
        level = os.getenv("GEMINI_THINKING_LEVEL_FAST" if "lite" in model else "GEMINI_THINKING_LEVEL", "").strip().lower()
        if not level:
            level = "minimal" if "lite" in model else "low"
        cfg["thinkingConfig"] = {"thinkingLevel": level}
    return cfg


class GeminiClient:
    """Thread-safe: each thread keeps its own persistent HTTPSConnection."""

    def __init__(self, connect_timeout=10.0, read_timeout=60.0, conn_factory=None):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._local = threading.local()
        self._ctx = ssl.create_default_context()
        self._factory = conn_factory or (lambda: http.client.HTTPSConnection(HOST, timeout=self.connect_timeout, context=self._ctx))
        # 2.2: models that answered 404 are skipped for DEAD_MODEL_TTL seconds, so a retired/unknown primary model
        # does not cost an extra Gemini round trip on EVERY request. The last candidate is always tried.
        self._dead = {}

    DEAD_MODEL_TTL = 3600.0

    def _candidates(self, tier):
        models = models_for(tier)
        now = time.monotonic()
        alive = [m for m in models if self._dead.get(m, 0.0) <= now]
        return alive or models[-1:]

    def _mark_dead(self, model):
        self._dead[model] = time.monotonic() + self.DEAD_MODEL_TTL
        logger.warning("Gemini model %s returned 404 - skipped for %ss", model, int(self.DEAD_MODEL_TTL))

    # ---------------- connection handling
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._factory()
            self._local.conn = c
        return c

    def _drop(self):
        c = getattr(self._local, "conn", None)
        self._local.conn = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _request(self, path, body, key):
        """Sends a request, reconnecting once if the kept-alive socket was closed by the server."""
        headers = {"Content-Type": "application/json", "x-goog-api-key": key, "Connection": "keep-alive"}
        data = json.dumps(body).encode("utf-8")
        for attempt in (1, 2):
            conn = self._conn()
            try:
                conn.request("POST", path, body=data, headers=headers)
                if getattr(conn, "sock", None) is not None:
                    try:
                        conn.sock.settimeout(self.read_timeout)
                    except Exception:
                        pass
                return conn.getresponse()
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest, http.client.BadStatusLine,
                    BrokenPipeError, ConnectionResetError) as exc:
                self._drop()
                if attempt == 2:
                    raise ProviderError(f"Gemini connection failed: {type(exc).__name__}", 504) from None
            except (socket.timeout, TimeoutError):
                self._drop()
                raise ProviderError("Gemini connection timed out", 504) from None
            except OSError as exc:
                self._drop()
                raise ProviderError(f"Gemini connection error: {type(exc).__name__}", 504) from None
        raise ProviderError("unreachable", 504)

    @staticmethod
    def _error_detail(resp):
        try:
            return resp.read().decode("utf-8", errors="replace")[:600]
        except Exception:
            return ""

    # ---------------- non-streaming
    def generate(self, parts, tier="quality", max_tokens=900):
        """Returns dict(text, model, usage, ttfb_ms, total_ms)."""
        key = api_key()
        if not key:
            raise ProviderError("Gemini is not configured", 503)
        last = None
        for model in self._candidates(tier):
            thinking = True
            err = None
            for _retry in (1, 2):
                t0 = time.monotonic()
                body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation_config(model, max_tokens, thinking)}
                resp = self._request(API_PREFIX + model + ":generateContent", body, key)
                ttfb = (time.monotonic() - t0) * 1000
                if resp.status == 200:
                    raw = resp.read()
                    result = json.loads(raw.decode("utf-8"))
                    text = _text_of(result)
                    if not text:
                        raise ProviderError("Gemini returned empty text (blocked or no candidates)", 502)
                    return {"text": text, "model": model, "usage": result.get("usageMetadata") or {},
                            "ttfb_ms": ttfb, "total_ms": (time.monotonic() - t0) * 1000}
                detail = self._error_detail(resp)
                logger.error("Gemini HTTP error model=%s status=%s detail=%s", model, resp.status, detail[:300])
                if resp.status == 400 and thinking and "thinking" in detail.lower():
                    thinking = False  # model rejects this thinking config -> retry without it
                    continue
                err = ProviderError(f"Gemini returned HTTP {resp.status}", resp.status)
                break
            if err is None:
                continue
            last = err
            if err.status == 404:
                self._mark_dead(model)
                continue
            if err.status == 429:
                raise ProviderError("Gemini quota/rate limit reached", 429)
            raise err
        raise last or ProviderError("No Gemini model available", 502)

    # ---------------- streaming
    def stream(self, parts, tier="fast", max_tokens=900):
        """Generator of events: ("start", {model, ttfb_ms}), ("delta", text), ("done", {usage, model, total_ms, ttfb_ms})."""
        key = api_key()
        if not key:
            raise ProviderError("Gemini is not configured", 503)
        last = None
        for model in self._candidates(tier):
            thinking = True
            resp = None
            err = None
            t0 = time.monotonic()
            for _retry in (1, 2):
                t0 = time.monotonic()
                body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation_config(model, max_tokens, thinking)}
                resp = self._request(API_PREFIX + model + ":streamGenerateContent?alt=sse", body, key)
                if resp.status == 200:
                    break
                detail = self._error_detail(resp)
                logger.error("Gemini stream HTTP error model=%s status=%s detail=%s", model, resp.status, detail[:300])
                if resp.status == 400 and thinking and "thinking" in detail.lower():
                    thinking = False
                    resp = None
                    continue
                err = ProviderError(f"Gemini returned HTTP {resp.status}", resp.status)
                resp = None
                break
            if resp is None:
                last = err or last
                if err is not None and err.status == 404:
                    self._mark_dead(model)
                    continue
                if err is not None and err.status == 429:
                    raise ProviderError("Gemini quota/rate limit reached", 429)
                raise err or ProviderError("Gemini stream failed", 502)
            yield from self._read_sse(resp, model, t0)
            return
        raise last or ProviderError("No Gemini model available", 502)

    def _read_sse(self, resp, model, t0):
        usage = {}
        first = None
        got_text = False
        finished = False
        try:
            for raw_line in _iter_lines(resp):
                if not raw_line.startswith("data:"):
                    continue
                payload = raw_line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("usageMetadata"):
                    usage = chunk["usageMetadata"]
                text = _text_of(chunk)
                if text:
                    if first is None:
                        first = (time.monotonic() - t0) * 1000
                        yield ("start", {"model": model, "ttfb_ms": first})
                    got_text = True
                    yield ("delta", text)
            finished = True
        except (socket.timeout, TimeoutError):
            raise ProviderError("Gemini stream timed out", 504) from None
        except OSError:
            raise ProviderError("Gemini stream interrupted", 504) from None
        finally:
            if not finished:
                self._drop()  # client cancelled or error: unread data on the socket -> never reuse it
        if not got_text:
            raise ProviderError("Gemini returned empty text (blocked or no candidates)", 502)
        yield ("done", {"model": model, "usage": usage, "ttfb_ms": first, "total_ms": (time.monotonic() - t0) * 1000})


def _iter_lines(resp):
    """Yields decoded lines as soon as they arrive (works for chunked SSE responses)."""
    buf = b""
    while True:
        chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buf:
        yield buf.decode("utf-8", errors="replace").rstrip("\r")


def _text_of(result):
    cands = result.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    # Gemini 3.x may return thought summaries flagged "thought": true -> never shown to users.
    return "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))


_client = None
_client_lock = threading.Lock()


def client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = GeminiClient(read_timeout=float(os.getenv("GEMINI_READ_TIMEOUT", "60")))
    return _client
