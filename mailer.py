"""Outgoing e-mail (purchase / licence mails, interest-list confirmations, cost alerts).

Providers (chosen by MAIL_PROVIDER, or automatically by which credentials exist):
- resend : HTTPS API  (RESEND_API_KEY)          – free tier, needs a verified sending domain
- brevo  : HTTPS API  (BREVO_API_KEY)           – free tier, verified sender address or domain
- gmail  : Gmail API over HTTPS (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN) – sends AS the owner's own Gmail
           address: works WITHOUT an own domain and is fully authenticated (SPF/DKIM/DMARC aligned by Google). Consumer Gmail
           sending limit applies (about 500 recipients/day). tools/gmail_oauth_setup.py creates the refresh token.
- smtp   : SMTP_HOST/SMTP_USER/SMTP_PASSWORD     – NOTE: Render free instances block outbound SMTP ports, use an API provider
Sender: MAIL_FROM ("E-Bike Doctor PRO <info@example.com>"), optional MAIL_REPLY_TO. Legacy SMTP_FROM is still accepted.
Own-domain providers use RESEND_FROM / BREVO_FROM (e.g. "E-Bike Doctor PRO <licenc@mail.ebikedoctorpro.com>").
FAILOVER (3.2.x): MAIL_PROVIDER = primary, MAIL_FALLBACK = comma list (e.g. "resend"). Without MAIL_PROVIDER the order is
gmail -> resend -> brevo -> smtp among the configured ones, so Gmail stays primary and Resend is the alternative sender; when the
primary fails (token revoked, quota, HTTP error) the same message is sent by the next provider, and the outbox shows which one.
No paid service is created by this code. Without a provider the message is kept in a short server-side outbox (admin page)
with licence keys and link tokens REDACTED, so nothing secret is ever logged.
"""
import base64
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

logger = logging.getLogger("ebike-doctor.mail")
RESEND_URL = "https://api.resend.com/emails"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
PROVIDERS = ("gmail", "resend", "brevo", "smtp")
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _env(k):
    return os.getenv(k, "").strip()


def _int_env(k, default):
    try:
        return int(_env(k) or default)
    except ValueError:
        return default


class DefiniteFailure(Exception):
    """The provider certainly did NOT accept the message (nothing left our side / refused before sending)."""


def _uncertain(exc):
    """True when we cannot know whether the provider accepted the message (timeout, connection dropped after sending)."""
    if isinstance(exc, (DefiniteFailure, RuntimeError)):            # RuntimeError = Gmail token refresh failed (before sending)
        return False
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        r = exc.reason
        return not isinstance(r, (ConnectionRefusedError, socket.gaierror))   # refused / DNS -> nothing was sent
    return True


class Mailer:
    def __init__(self, store):
        self.store = store

    @property
    def sender(self):
        return _env("MAIL_FROM") or _env("SMTP_FROM")

    def sender_for(self, p):
        """Resend/Brevo send from the own domain (RESEND_FROM / BREVO_FROM, e.g. "E-Bike Doctor PRO <licenc@mail.ebikedoctorpro.com>");
        Gmail always sends as the authorised Gmail account (MAIL_FROM)."""
        own = {"resend": "RESEND_FROM", "brevo": "BREVO_FROM"}.get(p)
        s = (_env(own) if own else "") or self.sender
        if p in ("resend", "brevo") and re.search(r"@(gmail|googlemail)\.com>?\s*$", s, re.I):
            return ""                       # a domain provider cannot send as a gmail.com address
        return s

    @staticmethod
    def _has_creds(p):
        return {"resend": bool(_env("RESEND_API_KEY")), "brevo": bool(_env("BREVO_API_KEY")),
                "gmail": all(_env(k) for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")),
                "smtp": all(_env(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))}.get(p, False)

    def chain(self):
        """Ordered providers: MAIL_PROVIDER (primary) then MAIL_FALLBACK (comma list). Without MAIL_PROVIDER the order is
        gmail → resend → brevo → smtp, so adding a Resend key never silently replaces a working Gmail sender: Resend becomes the
        fallback. Only providers with credentials AND a usable sender address are listed."""
        primary = _env("MAIL_PROVIDER").lower()
        fb = [x.strip().lower() for x in _env("MAIL_FALLBACK").split(",") if x.strip()]
        head = [primary] if primary in PROVIDERS else []
        order = head + (fb if fb else list(PROVIDERS))
        out = []
        for p in order:
            if p in PROVIDERS and p not in out and self._has_creds(p) and "@" in self.sender_for(p):
                out.append(p)
        return out

    @property
    def provider(self):
        c = self.chain()
        if c:
            return c[0]
        p = _env("MAIL_PROVIDER").lower()
        return p if p in PROVIDERS else "none"

    @property
    def configured(self):
        return bool(self.chain())

    # ------------------------------------------------------------------ transport
    def _http_post(self, url, headers, payload, form=False):
        """Single place for provider HTTP calls (tests replace it). Returns (status, body_text)."""
        data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        ctype = "application/x-www-form-urlencoded" if form else "application/json"
        # an explicit User-Agent is required: the Resend API answers 403 to the default "Python-urllib" agent (seen on staging)
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype, "Accept": "application/json",
                                                                             "User-Agent": "EBikeDoctorPRO-Backend/3.2 (+https://ebikedoctorpro.com)", **headers})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")[:500]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:500]

    def _send_resend(self, to, subject, text, html, headers, idem=None):
        payload = {"from": self.sender_for("resend"), "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        if _env("MAIL_REPLY_TO"):
            payload["reply_to"] = _env("MAIL_REPLY_TO")
        if headers:
            payload["headers"] = headers
        h = {"Authorization": "Bearer " + _env("RESEND_API_KEY")}
        if idem:
            h["Idempotency-Key"] = idem[:256]     # Resend de-duplicates the same key for 24 h -> a safe single retry
        try:
            status, body = self._http_post(RESEND_URL, h, payload)
        except Exception as exc:
            if not idem or not _uncertain(exc):
                raise
            time.sleep(1)                         # same Idempotency-Key: Resend sends it at most once
            return self._http_post(RESEND_URL, h, payload)
        if idem and status >= 500:
            time.sleep(1)
            status, body = self._http_post(RESEND_URL, h, payload)
        return status, body

    # ------------------------------------------------------------------ quota (free plans) + traffic counters
    def _quota_block(self, p):
        """Free-plan guard: Resend free = 100/day, 3000/month (RESEND_DAILY_LIMIT / RESEND_MONTHLY_LIMIT override).
        Counts sent + uncertain attempts. Returns an error text when the provider must not be used now."""
        if p != "resend":
            return ""
        day, month = time.strftime("%Y%m%d", time.gmtime()), time.strftime("%Y%m", time.gmtime())
        used_d = sum(int(self.store.get(f"mail:stat:{day}:resend:{o}") or 0) for o in ("sent", "uncertain"))
        used_m = sum(int(self.store.get(f"mail:statm:{month}:resend:{o}") or 0) for o in ("sent", "uncertain"))
        if used_d >= _int_env("RESEND_DAILY_LIMIT", 100):
            return "quota_daily"
        if used_m >= _int_env("RESEND_MONTHLY_LIMIT", 3000):
            return "quota_monthly"
        return ""

    def _count(self, p, outcome, err=""):
        day, month = time.strftime("%Y%m%d", time.gmtime()), time.strftime("%Y%m", time.gmtime())
        try:
            self.store.incr(f"mail:stat:{day}:{p}:{outcome}", 400 * 86_400)
            self.store.incr(f"mail:statm:{month}:{p}:{outcome}", 800 * 86_400)
            self.store.set(f"mail:last:{p}:{'ok' if outcome == 'sent' else 'fail'}", {"t": int(time.time()), "outcome": outcome, "error": err[:60]}, 400 * 86_400)
        except Exception:
            pass

    def _send_brevo(self, to, subject, text, html, headers):
        name, addr = parseaddr(self.sender_for("brevo"))
        payload = {"sender": {"email": addr, **({"name": name} if name else {})}, "to": [{"email": to}], "subject": subject,
                   "textContent": text}
        if html:
            payload["htmlContent"] = html
        if _env("MAIL_REPLY_TO"):
            payload["replyTo"] = {"email": parseaddr(_env("MAIL_REPLY_TO"))[1]}
        if headers:
            payload["headers"] = headers
        return self._http_post(BREVO_URL, {"api-key": _env("BREVO_API_KEY")}, payload)

    _gmail_token = (None, 0.0)

    def _gmail_access_token(self):
        tok, exp = Mailer._gmail_token
        if tok and time.time() < exp - 60:
            return tok
        status, body = self._http_post(GOOGLE_TOKEN_URL, {}, {"client_id": _env("GMAIL_CLIENT_ID"), "client_secret": _env("GMAIL_CLIENT_SECRET"),
                                                              "refresh_token": _env("GMAIL_REFRESH_TOKEN"), "grant_type": "refresh_token"}, form=True)
        if status != 200:
            # invalid_grant = the owner revoked access, the consent screen is still in "Testing" (7-day tokens) or the token expired
            raise RuntimeError("gmail_token_revoked" if "invalid_grant" in (body or "") else f"gmail_token_http_{status}")
        d = json.loads(body)
        Mailer._gmail_token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return d["access_token"]

    def _send_gmail(self, to, subject, text, html, headers):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender_for("gmail"), to, subject
        if _env("MAIL_REPLY_TO"):
            msg["Reply-To"] = _env("MAIL_REPLY_TO")
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self._http_post(GMAIL_SEND_URL, {"Authorization": "Bearer " + self._gmail_access_token()}, {"raw": raw})

    def _send_smtp(self, to, subject, text, html, headers):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        if _env("MAIL_REPLY_TO"):
            msg["Reply-To"] = _env("MAIL_REPLY_TO")
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(_env("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
            s.send_message(msg)
        return 250, ""

    # ------------------------------------------------------------------ public
    def simulated_failures(self, extra=None):
        """TEST MODE ONLY: providers that must fail on purpose (MAIL_SIMULATE_FAILURE env or an admin test request), to test the
        failover without touching real credentials. Ignored when SHOP_MODE=live."""
        if _env("SHOP_MODE").lower() == "live":
            return set()
        return {x.strip().lower() for x in (_env("MAIL_SIMULATE_FAILURE").split(",") + list(extra or [])) if x.strip()}

    def send(self, to, subject, body, kind="info", html=None, headers=None, idempotency_key=None, simulate_fail=None):
        """Never raises (a mail problem must not break a webhook). Returns the outbox entry.
        sent=True: accepted by a provider. uncertain=True: we cannot know (timeout / 5xx after sending) -> NO fallback and NO
        automatic retry (no duplicate licence mail); the owner decides in the admin. Otherwise the next provider is tried."""
        chain = self.chain()
        sim = self.simulated_failures(simulate_fail)
        entry = {"t": int(time.time()), "to": _mask(to), "subject": subject[:160], "kind": kind, "sent": False, "provider": self.provider}
        if chain and to and "@" in to:
            failed = []
            for i, p in enumerate(chain):       # primary first; on a DEFINITE failure the next provider (e.g. Gmail -> Resend)
                uncertain = False
                try:
                    if p in sim:
                        raise DefiniteFailure("simulated_" + p)
                    q = self._quota_block(p)
                    if q:
                        raise DefiniteFailure(q)
                    args = (to, subject, body, html, headers) + ((idempotency_key,) if p == "resend" else ())
                    status, resp = getattr(self, "_send_" + p)(*args)
                    if 200 <= status < 300:
                        entry.update(sent=True, provider=p)
                        self._count(p, "sent")
                        if i:
                            entry["fallback_from"] = failed
                            logger.warning("mail sent by FALLBACK provider=%s after %s kind=%s", p, failed, kind)
                        entry.pop("error", None)
                        break
                    err = f"HTTP {status}"
                    uncertain = status >= 500      # 5xx: maybe accepted (Resend: already retried once with the same idempotency key)
                    logger.error("mail rejected kind=%s provider=%s status=%s detail=%s", kind, p, status, _redact(resp)[:200])
                except Exception as exc:   # never break the webhook because of mail
                    err = str(exc)[:40] if isinstance(exc, (RuntimeError, DefiniteFailure)) else type(exc).__name__
                    uncertain = _uncertain(exc)
                    logger.error("mail failed kind=%s provider=%s error=%s uncertain=%s", kind, p, err, uncertain)
                failed.append(f"{p}:{err}")
                entry["error"] = err
                self._count(p, "uncertain" if uncertain else "failed", err)
                if uncertain:
                    entry["uncertain"] = True
                    entry["provider"] = p
                    logger.error("mail delivery UNCERTAIN kind=%s provider=%s - no automatic retry/fallback (no duplicate mail)", kind, p)
                    break
            if not entry["sent"] and len(failed) > 1:
                entry["error"] = "; ".join(failed)[:120]
        else:
            entry["error"] = "not_configured"
            logger.warning("mail not sent (no mail provider configured) kind=%s to=%s", kind, _mask(to))
        try:
            if entry.get("error") != "not_configured":
                self.store.set("mail:health", {"t": entry["t"], "ok": entry["sent"], "error": entry.get("error", ""), "provider": entry["provider"]}, 400 * 86_400)
            box = self.store.get("mail:outbox") or []
            box = (box if isinstance(box, list) else [])[-49:] + [entry | {"body_preview": _redact(body)[:400]}]
            self.store.set("mail:outbox", box, 90 * 86_400)
        except Exception:
            pass
        return entry


def health(store):
    h = store.get("mail:health")
    return h if isinstance(h, dict) else {}


def stats(store, mailer=None, days=30):
    """Admin: per provider daily (30 days) and monthly sent / failed / uncertain counts, last success / failure, free-plan usage."""
    from store import mget
    now = time.time()
    dlist = [time.strftime("%Y%m%d", time.gmtime(now - i * 86_400)) for i in range(days)]
    month = time.strftime("%Y%m", time.gmtime(now))
    outs = ("sent", "failed", "uncertain")
    keys = [f"mail:stat:{d}:{p}:{o}" for d in dlist for p in PROVIDERS for o in outs]
    keys += [f"mail:statm:{month}:{p}:{o}" for p in PROVIDERS for o in outs]
    keys += [f"mail:last:{p}:{w}" for p in PROVIDERS for w in ("ok", "fail")]
    vals = mget(store, keys)
    it = iter(vals)
    daily = []
    for d in dlist:
        row = {"day": d}
        for p in PROVIDERS:
            for o in outs:
                v = int(next(it) or 0)
                if v:
                    row[f"{p}:{o}"] = v
        daily.append(row)
    monthly = {}
    for p in PROVIDERS:
        for o in outs:
            v = int(next(it) or 0)
            if v:
                monthly[f"{p}:{o}"] = v
    last = {}
    for p in PROVIDERS:
        for w in ("ok", "fail"):
            v = next(it)
            if isinstance(v, dict):
                last[f"{p}:{w}"] = v
    today = daily[0]
    used_d = today.get("resend:sent", 0) + today.get("resend:uncertain", 0)
    used_m = monthly.get("resend:sent", 0) + monthly.get("resend:uncertain", 0)
    m = mailer or Mailer(store)
    return {"chain": m.chain(), "month": month, "daily": daily, "monthly": monthly, "last": last,
            "resend": {"configured": m._has_creds("resend") and "@" in m.sender_for("resend"), "from": m.sender_for("resend"),
                       "daily_used": used_d, "daily_limit": _int_env("RESEND_DAILY_LIMIT", 100),
                       "monthly_used": used_m, "monthly_limit": _int_env("RESEND_MONTHLY_LIMIT", 3000)},
            "simulated_failures": sorted(m.simulated_failures())}


def _mask(e):
    e = (e or "").strip()
    if "@" not in e:
        return ""
    user, dom = e.split("@", 1)
    return user[:2] + "***@" + dom


def _redact(body):
    body = re.sub(r"EBD[WA]-[0-9A-Z-]{20,}", "EBD?-*****-*****-*****-*****", body or "")
    return re.sub(r"([?&](t|token)=)[A-Za-z0-9_\-.%]+", r"\1*****", body)
