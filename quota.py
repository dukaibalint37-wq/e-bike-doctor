"""Server-side daily AI quota (business model: one-time purchase, 50 AI questions / user / day).

Identity: the app registers a random install ID once and receives an HMAC-signed token
(QUOTA_SECRET). The quota is keyed by the token's install ID, so it cannot be raised by editing
the app: the token cannot be forged without the server secret.
Abuse controls (all server-side):
- per install: DAILY_AI_LIMIT (default 50)
- per IP: DAILY_IP_LIMIT (default 300 – workshops share an IP) and MAX_REGISTRATIONS_PER_IP (default 5/day),
  so reinstalling the app to get a fresh 50 is capped
- requests without a token use an IP-based anonymous bucket with the same 50 limit (older app versions)
- global: DAILY_GLOBAL_LIMIT requests and DAILY_COST_LIMIT_USD estimated spend -> protects the API bill
Stronger proof of a genuine, purchased install = Google Play Integrity API (documented next step).
"""
import base64
import datetime
import hashlib
import hmac
import os
import re
import secrets
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DAY = 86_400


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def today(now=None):
    tz = os.getenv("QUOTA_TZ", "Europe/Budapest")
    t = datetime.datetime.fromtimestamp(now or time.time(), ZoneInfo(tz) if ZoneInfo else datetime.timezone.utc)
    return t.strftime("%Y%m%d")


def month(now=None):
    return today(now)[:6]


def seconds_to_midnight(now=None):
    tz = ZoneInfo(os.getenv("QUOTA_TZ", "Europe/Budapest")) if ZoneInfo else datetime.timezone.utc
    t = datetime.datetime.fromtimestamp(now or time.time(), tz)
    nxt = (t + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((nxt - t).total_seconds()))


class TokenSigner:
    def __init__(self, secret=None):
        s = secret or os.getenv("QUOTA_SECRET", "").strip()
        self.ephemeral = not s
        # Without QUOTA_SECRET a random secret is used: tokens stay valid until the next restart (documented).
        self.secret = (s or secrets.token_hex(32)).encode()

    def issue(self, install_id):
        iid = sanitize_install_id(install_id)
        payload = f"{iid}.{int(time.time())}"
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()[:18]
        return payload + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def verify(self, token):
        """Returns the install ID or None."""
        if not token or len(token) > 200:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[0] + "." + parts[1]
        expected = base64.urlsafe_b64encode(hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")
        if not hmac.compare_digest(expected, parts[2]):
            return None
        return parts[0]


def sanitize_install_id(v):
    v = re.sub(r"[^A-Za-z0-9-]", "", (v or ""))[:64]
    if len(v) < 16:
        raise ValueError("invalid install id")
    return v


def ip_hash(ip):
    """IPs are stored only as salted hashes (privacy)."""
    salt = os.getenv("QUOTA_SECRET", "ebd")
    return hashlib.sha256((salt + "|" + (ip or "?")).encode()).hexdigest()[:20]


class Decision:
    def __init__(self, allowed, remaining, limit, reason="", retry_after=0, subject=""):
        self.allowed, self.remaining, self.limit = allowed, remaining, limit
        self.reason, self.retry_after, self.subject = reason, retry_after, subject


class QuotaManager:
    def __init__(self, store, signer=None, subject_resolver=None, on_cost_warning=None):
        self.store = store
        self.signer = signer or TokenSigner()
        # 3.0: licence-bound tokens ('lic-<lid>-<dev>') are checked against the licence server on every AI call
        self.subject_resolver = subject_resolver
        self.on_cost_warning = on_cost_warning

    @staticmethod
    def access_mode():
        """open = install tokens + anonymous (backward compatible, default) | token = any signed token | license = licence tokens only."""
        m = os.getenv("AI_ACCESS_MODE", "open").strip().lower()
        return m if m in ("open", "token", "license") else "open"

    def subject_for(self, token, ip):
        """Returns (subject, deny_reason)."""
        iid = self.signer.verify(token) if token else None
        mode = self.access_mode()
        if iid and iid.startswith("lic-"):
            subj = self.subject_resolver(iid) if self.subject_resolver else None
            return (subj, "") if subj else (None, "license_invalid")
        if iid:
            return (None, "license_required") if mode == "license" else (f"u:{iid}", "")
        if mode != "open":
            return None, ("license_required" if mode == "license" else "token_required")
        return f"anon:{ip_hash(ip)}", ""

    @property
    def daily_limit(self):
        return _int("DAILY_AI_LIMIT", 50)

    def register(self, install_id, ip):
        """Issues a token; limits new registrations per IP per day."""
        day = today()
        n = self.store.incr(f"reg:{day}:{ip_hash(ip)}", DAY + 3600)
        if n > _int("MAX_REGISTRATIONS_PER_IP", 5):
            return None
        return self.signer.issue(install_id)

    def check_and_consume(self, token, ip, cost_estimate_usd=0.0):
        day = today()
        limit = self.daily_limit
        ttl = DAY + 3600
        retry = seconds_to_midnight()
        subject, deny = self.subject_for(token, ip)
        if subject is None:
            return Decision(False, 0, limit, deny, 0, "")
        # global protections first (they do not consume the user's quota)
        if self.store.incr(f"global:{day}", ttl) > _int("DAILY_GLOBAL_LIMIT", 5000):
            self.store.incr(f"global:{day}", ttl, -1)
            return Decision(False, 0, limit, "global_limit", retry, subject)
        cost_cap = _float("DAILY_COST_LIMIT_USD", 0.0)
        if cost_cap > 0:
            spent = (self.store.get(f"cost:{day}") or 0) / 1_000_000
            if spent + cost_estimate_usd > cost_cap:
                self.store.incr(f"global:{day}", ttl, -1)
                return Decision(False, 0, limit, "cost_limit", retry, subject)
        month_cap = _float("MONTHLY_COST_LIMIT_USD", 0.0)
        if month_cap > 0:
            spent_m = (self.store.get(f"cost-m:{month()}") or 0) / 1_000_000
            if spent_m + cost_estimate_usd > month_cap:
                self.store.incr(f"global:{day}", ttl, -1)
                return Decision(False, 0, limit, "cost_limit", retry, subject)
        ip_count = self.store.incr(f"ip:{day}:{ip_hash(ip)}", ttl)
        if ip_count > _int("DAILY_IP_LIMIT", 300):
            self.store.incr(f"ip:{day}:{ip_hash(ip)}", ttl, -1)
            self.store.incr(f"global:{day}", ttl, -1)
            return Decision(False, 0, limit, "ip_limit", retry, subject)
        used = self.store.incr(f"q:{day}:{subject}", ttl)
        if used > limit:
            self.store.incr(f"q:{day}:{subject}", ttl, -1)
            self.store.incr(f"ip:{day}:{ip_hash(ip)}", ttl, -1)
            self.store.incr(f"global:{day}", ttl, -1)
            return Decision(False, 0, limit, "daily_quota", retry, subject)
        return Decision(True, limit - used, limit, "", 0, subject)

    def refund(self, decision, ip):
        """Provider failure -> the question is not counted."""
        if not decision or not decision.allowed:
            return
        day = today()
        ttl = DAY + 3600
        self.store.incr(f"q:{day}:{decision.subject}", ttl, -1)
        self.store.incr(f"ip:{day}:{ip_hash(ip)}", ttl, -1)
        self.store.incr(f"global:{day}", ttl, -1)

    def add_cost(self, usd, tokens=0):
        """Logs API consumption (daily + monthly, kept 400 days); one-time warnings at COST_WARN_LEVELS % of the caps."""
        day, mon = today(), month()
        self.store.incr(f"usage:{day}:calls", DAY * 400)
        if tokens:
            self.store.incr(f"usage:{day}:tokens", DAY * 400, int(tokens))
        if usd <= 0:
            return
        micro = int(usd * 1_000_000)
        spent_d = self.store.incr(f"cost:{day}", DAY * 400, micro) / 1_000_000
        spent_m = self.store.incr(f"cost-m:{mon}", DAY * 400, micro) / 1_000_000
        levels = [int(x) for x in os.getenv("COST_WARN_LEVELS", "50,80,100").split(",") if x.strip().isdigit()]
        for period, spent, cap in (("day:" + day, spent_d, _float("DAILY_COST_LIMIT_USD", 0.0)), ("month:" + mon, spent_m, _float("MONTHLY_COST_LIMIT_USD", 0.0))):
            if cap <= 0:
                continue
            for lvl in levels:
                if spent >= cap * lvl / 100 and self.store.incr(f"costwarn:{period}:{lvl}", DAY * 40) == 1 and self.on_cost_warning:
                    self.on_cost_warning(period, lvl, spent, cap)

    def usage(self, days=31):
        from store import mget
        now = time.time()
        dlist = [today(now - i * DAY) for i in range(days)]
        vals = mget(self.store, [k for d in dlist for k in (f"usage:{d}:calls", f"usage:{d}:tokens", f"cost:{d}")])
        out = [{"day": d, "calls": int(vals[3 * i] or 0), "tokens": int(vals[3 * i + 1] or 0), "cost_usd": round((vals[3 * i + 2] or 0) / 1_000_000, 4)}
               for i, d in enumerate(dlist)]
        return {"days": out, "month": month(), "month_cost_usd": round((self.store.get(f"cost-m:{month()}") or 0) / 1_000_000, 4),
                "daily_cap_usd": _float("DAILY_COST_LIMIT_USD", 0.0), "monthly_cap_usd": _float("MONTHLY_COST_LIMIT_USD", 0.0)}

    def remaining(self, token, ip):
        subject, _ = self.subject_for(token, ip)
        if subject is None:
            return 0
        used = self.store.get(f"q:{today()}:{subject}") or 0
        return max(0, self.daily_limit - int(used))
