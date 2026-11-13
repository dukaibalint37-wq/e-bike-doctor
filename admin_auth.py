"""Owner admin authentication (3.2): password + TOTP two-factor login, signed session cookie, brute-force lockout, audit log.

Env (server-side only, never in the website or the apps):
  ADMIN_PASSWORD_HASH   pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>   (tools/admin_setup.py creates it)
  ADMIN_TOTP_SECRET     base32 secret of the authenticator app (RFC 6238, SHA-1, 6 digits, 30 s)
  ADMIN_SESSION_SECRET  HMAC key of the session cookie (fallback: derived from ADMIN_TOKEN)
  ADMIN_SESSION_HOURS   session lifetime (default 8)
  ADMIN_HEADER_TOKEN    1 (default) = the X-Admin-Token header (automation, scripts) is still accepted; 0 = 2FA web login only
The browser UI never sees ADMIN_TOKEN. Login needs BOTH the password and a fresh 6-digit code; a code is accepted once only.
"""
import base64
import hashlib
import hmac
import os
import struct
import time

from licensing import LicenseError

COOKIE = "ebd_admin"
MAX_FAILS_PER_IP = 5          # per 15 minutes
MAX_FAILS_GLOBAL = 30         # per hour (distributed guessing)
WINDOW = 900


def _env(k):
    return os.getenv(k, "").strip()


# --------------------------------------------------------------------------------------------- primitives
def hash_password(password, iterations=310_000, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password, stored):
    try:
        algo, it, salt, h = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(dk, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def totp(secret_b32, step, digits=6):
    key = base64.b32decode(secret_b32.upper().replace(" ", "") + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


class AdminAuth:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    @property
    def configured(self):
        return bool(_env("ADMIN_PASSWORD_HASH") and _env("ADMIN_TOTP_SECRET"))

    def _session_key(self):
        k = _env("ADMIN_SESSION_SECRET") or _env("ADMIN_TOKEN")
        return hashlib.sha256(("ebd-admin-session|" + k).encode()).digest() if k else b""

    # ---- login
    def _locked(self, ip_hash):
        now = int(self.clock())
        return (int(self.store.get(f"adm:fail:{ip_hash}:{now // WINDOW}") or 0) >= MAX_FAILS_PER_IP
                or int(self.store.get(f"adm:failg:{now // 3600}") or 0) >= MAX_FAILS_GLOBAL)

    def _fail(self, ip_hash, why):
        now = int(self.clock())
        self.store.incr(f"adm:fail:{ip_hash}:{now // WINDOW}", WINDOW * 2)
        self.store.incr(f"adm:failg:{now // 3600}", 7200)
        self.audit("login_failed", ip_hash, why)

    def check_code(self, code):
        """Valid for the current ±1 time step, and each step only once (replay protection)."""
        secret = _env("ADMIN_TOTP_SECRET")
        code = (code or "").strip().replace(" ", "")
        if not secret or len(code) != 6 or not code.isdigit():
            return False
        now_step = int(self.clock()) // 30
        last = int(self.store.get("adm:totp_last") or 0)
        for step in (now_step - 1, now_step, now_step + 1):
            if step > last and hmac.compare_digest(totp(secret, step), code):
                self.store.set("adm:totp_last", step, 3600)
                return True
        return False

    def login(self, password, code, ip_hash):
        if not self.configured or not self._session_key():
            raise LicenseError("not_configured", 503, "Admin login is not configured")
        if self._locked(ip_hash):
            raise LicenseError("locked", 429, "Too many failed attempts - wait 15 minutes")
        pw_ok = verify_password(password, _env("ADMIN_PASSWORD_HASH"))
        code_ok = pw_ok and self.check_code(code)          # the code is only consumed after a correct password
        if not (pw_ok and code_ok):
            self._fail(ip_hash, "password" if not pw_ok else "code")
            raise LicenseError("bad_credentials", 401, "Wrong password or code")
        hours = max(1, min(24, int(_env("ADMIN_SESSION_HOURS") or 8)))
        exp = int(self.clock()) + hours * 3600
        sid = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
        self.store.set(f"adm:sess:{sid}", {"exp": exp, "ip": ip_hash[:12]}, hours * 3600)
        self.audit("login", ip_hash)
        return self._sign(f"{sid}.{exp}"), exp

    def _sign(self, payload):
        sig = hmac.new(self._session_key(), payload.encode(), hashlib.sha256).hexdigest()[:40]
        return f"{payload}.{sig}"

    def session_valid(self, cookie):
        try:
            sid, exp, sig = (cookie or "").split(".")
        except ValueError:
            return False
        if not self._session_key() or not hmac.compare_digest(self._sign(f"{sid}.{exp}").rsplit(".", 1)[1], sig):
            return False
        if int(exp) < self.clock():
            return False
        return bool(self.store.get(f"adm:sess:{sid}"))        # server-side record: logout really ends the session

    def logout(self, cookie, ip_hash):
        sid = (cookie or "").split(".")[0]
        if sid:
            self.store.set(f"adm:sess:{sid}", None, 1)
        self.audit("logout", ip_hash)

    # ---- request check
    def allowed(self, header_token, cookie, method="GET", csrf_header=""):
        admin = _env("ADMIN_TOKEN")
        if header_token and admin and _env("ADMIN_HEADER_TOKEN") != "0" and hmac.compare_digest(header_token, admin):
            return True
        if cookie and self.session_valid(cookie):
            return method == "GET" or csrf_header == "ebd-admin"      # CSRF: state changes need the custom header
        return False

    # ---- audit
    def audit(self, action, ip_hash="", detail=""):
        try:
            log = self.store.get("adm:audit") or []
            log = (log if isinstance(log, list) else [])[-199:] + [{"t": int(self.clock()), "a": action[:40], "ip": (ip_hash or "")[:10],
                                                                    "d": str(detail)[:80]}]
            self.store.set("adm:audit", log, 400 * 86_400)
        except Exception:
            pass

    def audit_log(self):
        log = self.store.get("adm:audit") or []
        return list(reversed(log if isinstance(log, list) else []))
