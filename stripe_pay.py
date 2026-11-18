"""Stripe Checkout (one-time payments) - dependency-free helpers (stdlib only, like the other provider calls).

- verify_signature(): the Stripe-Signature scheme (https://docs.stripe.com/webhooks#verify-manually):
  header "t=<unix>,v1=<hex>[,v1=...]", expected = hex(HMAC-SHA256(f"{t}.{raw body}", STRIPE_WEBHOOK_SECRET)),
  constant-time compare against every v1 entry, timestamp within STRIPE_WEBHOOK_TOLERANCE seconds (default 300).
- StripeClient: form-encoded REST calls to https://api.stripe.com/v1 with the secret key (never logged).
Environment: STRIPE_SECRET_KEY (sk_test_... / sk_live_... or restricted rk_...), STRIPE_PUBLISHABLE_KEY (not needed by the
hosted Checkout redirect flow - reported in the admin only), STRIPE_WEBHOOK_SECRET (whsec_...).
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.stripe.com/v1"
USER_AGENT = "EBikeDoctorPRO-backend/3.3 (+https://ebikedoctorpro.com)"


class StripeError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status, self.code = status, code


def verify_signature(raw: bytes, header: str, secret: str, now=None, tolerance=None) -> bool:
    if not raw or not header or not secret:
        return False
    if tolerance is None:
        try:
            tolerance = int(os.getenv("STRIPE_WEBHOOK_TOLERANCE", "300"))
        except ValueError:
            tolerance = 300
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1" and v:
            sigs.append(v.strip().lower())
    if not ts or not sigs:
        return False
    try:
        t = int(ts)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if tolerance > 0 and abs(now - t) > tolerance:
        return False                       # replayed / stale event
    expected = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def form_encode(params, prefix=""):
    """Stripe's bracket notation: {"a": {"b": 1}, "l": [{"x": 2}]} -> a[b]=1&l[0][x]=2 (booleans as true/false)."""
    out = []

    def walk(val, key):
        if isinstance(val, dict):
            for k, v in val.items():
                walk(v, f"{key}[{k}]" if key else str(k))
        elif isinstance(val, (list, tuple)):
            for i, v in enumerate(val):
                walk(v, f"{key}[{i}]")
        elif val is None:
            return
        elif isinstance(val, bool):
            out.append((key, "true" if val else "false"))
        else:
            out.append((key, str(val)))

    walk(params, prefix)
    return urllib.parse.urlencode(out)


def key_mode(key: str) -> str:
    """'test' / 'live' / '' from a Stripe key prefix (sk_test_, rk_live_, pk_test_ ...)."""
    key = (key or "").strip()
    for p in ("sk_", "rk_", "pk_"):
        if key.startswith(p + "test_"):
            return "test"
        if key.startswith(p + "live_"):
            return "live"
    return ""


class StripeClient:
    def __init__(self, secret_key=None, timeout=15):
        self._key = secret_key
        self.timeout = timeout

    @property
    def key(self):
        return (self._key if self._key is not None else os.getenv("STRIPE_SECRET_KEY", "")).strip()

    @property
    def configured(self):
        return bool(self.key)

    @property
    def mode(self):
        return key_mode(self.key)

    def _http(self, method, url, body, headers):            # separated for tests
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def request(self, method, path, params=None, idempotency_key=None):
        if not self.key:
            raise StripeError(503, "not_configured", "STRIPE_SECRET_KEY missing")
        headers = {"Authorization": "Bearer " + self.key, "User-Agent": USER_AGENT,
                   "Content-Type": "application/x-www-form-urlencoded"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url, body = API + path, None
        enc = form_encode(params or {})
        if method == "GET":
            url += ("?" + enc) if enc else ""
        else:
            body = enc.encode()
        try:
            status, text = self._http(method, url, body, headers)
        except (OSError, ValueError) as exc:          # network error / timeout: the caller decides (webhook -> Stripe retries)
            raise StripeError(502, "network", type(exc).__name__) from None
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        if status >= 400:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            raise StripeError(status, str(err.get("code") or err.get("type") or "stripe_error"), str(err.get("message", ""))[:300])
        return data

    def create_checkout_session(self, params, idempotency_key=None):
        return self.request("POST", "/checkout/sessions", params, idempotency_key)

    def get_checkout_session(self, session_id):
        return self.request("GET", "/checkout/sessions/" + urllib.parse.quote(session_id, safe=""))
