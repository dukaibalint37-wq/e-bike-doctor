"""Outgoing e-mail (purchase / licence mails, interest-list confirmations, cost alerts).

Providers (chosen by MAIL_PROVIDER, or automatically by which credentials exist):
- resend : HTTPS API  (RESEND_API_KEY)          – free tier, needs a verified sending domain
- brevo  : HTTPS API  (BREVO_API_KEY)           – free tier, verified sender address or domain
- gmail  : Gmail API over HTTPS (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN) – sends AS the owner's own Gmail
           address: works WITHOUT an own domain and is fully authenticated (SPF/DKIM/DMARC aligned by Google). Consumer Gmail
           sending limit applies (about 500 recipients/day). tools/gmail_oauth_setup.py creates the refresh token.
- smtp   : SMTP_HOST/SMTP_USER/SMTP_PASSWORD     – NOTE: Render free instances block outbound SMTP ports, use an API provider
Sender: MAIL_FROM ("E-Bike Doctor PRO <info@example.com>"), optional MAIL_REPLY_TO. Legacy SMTP_FROM is still accepted.
No paid service is created by this code. Without a provider the message is kept in a short server-side outbox (admin page)
with licence keys and link tokens REDACTED, so nothing secret is ever logged.
"""
import base64
import json
import logging
import os
import re
import smtplib
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
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _env(k):
    return os.getenv(k, "").strip()


class Mailer:
    def __init__(self, store):
        self.store = store

    @property
    def sender(self):
        return _env("MAIL_FROM") or _env("SMTP_FROM")

    @property
    def provider(self):
        p = _env("MAIL_PROVIDER").lower()
        if p in ("resend", "brevo", "smtp", "gmail"):
            return p
        if _env("RESEND_API_KEY"):
            return "resend"
        if _env("BREVO_API_KEY"):
            return "brevo"
        if _env("GMAIL_REFRESH_TOKEN"):
            return "gmail"
        if all(_env(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")):
            return "smtp"
        return "none"

    @property
    def configured(self):
        p = self.provider
        creds = {"resend": _env("RESEND_API_KEY"), "brevo": _env("BREVO_API_KEY"),
                 "gmail": all(_env(k) for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")),
                 "smtp": all(_env(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))}.get(p)
        return bool(creds) and "@" in self.sender

    # ------------------------------------------------------------------ transport
    def _http_post(self, url, headers, payload, form=False):
        """Single place for provider HTTP calls (tests replace it). Returns (status, body_text)."""
        data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        ctype = "application/x-www-form-urlencoded" if form else "application/json"
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype, "Accept": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")[:500]
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:500]

    def _send_resend(self, to, subject, text, html, headers):
        payload = {"from": self.sender, "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        if _env("MAIL_REPLY_TO"):
            payload["reply_to"] = _env("MAIL_REPLY_TO")
        if headers:
            payload["headers"] = headers
        return self._http_post(RESEND_URL, {"Authorization": "Bearer " + _env("RESEND_API_KEY")}, payload)

    def _send_brevo(self, to, subject, text, html, headers):
        name, addr = parseaddr(self.sender)
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
            raise RuntimeError(f"gmail token HTTP {status}")
        d = json.loads(body)
        Mailer._gmail_token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return d["access_token"]

    def _send_gmail(self, to, subject, text, html, headers):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
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
    def send(self, to, subject, body, kind="info", html=None, headers=None):
        """Never raises (a mail problem must not break a webhook). Returns the outbox entry (sent True/False)."""
        entry = {"t": int(time.time()), "to": _mask(to), "subject": subject[:160], "kind": kind, "sent": False, "provider": self.provider}
        if self.configured and to and "@" in to:
            try:
                status, resp = getattr(self, "_send_" + self.provider)(to, subject, body, html, headers)
                entry["sent"] = 200 <= status < 300
                if not entry["sent"]:
                    entry["error"] = f"HTTP {status}"
                    logger.error("mail rejected kind=%s provider=%s status=%s detail=%s", kind, self.provider, status, _redact(resp)[:200])
            except Exception as exc:   # never break the webhook because of mail
                entry["error"] = type(exc).__name__
                logger.error("mail failed kind=%s provider=%s error=%s", kind, self.provider, type(exc).__name__)
        else:
            entry["error"] = "not_configured"
            logger.warning("mail not sent (no mail provider configured) kind=%s to=%s", kind, _mask(to))
        try:
            box = self.store.get("mail:outbox") or []
            box = (box if isinstance(box, list) else [])[-49:] + [entry | {"body_preview": _redact(body)[:400]}]
            self.store.set("mail:outbox", box, 90 * 86_400)
        except Exception:
            pass
        return entry


def _mask(e):
    e = (e or "").strip()
    if "@" not in e:
        return ""
    user, dom = e.split("@", 1)
    return user[:2] + "***@" + dom


def _redact(body):
    body = re.sub(r"EBD[WA]-[0-9A-Z-]{20,}", "EBD?-*****-*****-*****-*****", body or "")
    return re.sub(r"([?&](t|token)=)[A-Za-z0-9_\-.%]+", r"\1*****", body)
