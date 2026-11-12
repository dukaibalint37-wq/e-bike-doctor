"""Outgoing e-mail (purchase confirmation, cost alerts).

Sends via SMTP only when SMTP_HOST/SMTP_USER/SMTP_PASSWORD/SMTP_FROM are configured (any provider; no paid service is
created by this code). Otherwise the message is kept in a short server-side outbox (visible in the admin page) and logged
WITHOUT the licence key, so nothing is lost while the shop runs in test mode.
"""
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

logger = logging.getLogger("ebike-doctor.mail")


class Mailer:
    def __init__(self, store):
        self.store = store

    @property
    def configured(self):
        return all(os.getenv(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"))

    def send(self, to, subject, body, kind="info"):
        entry = {"t": int(time.time()), "to": _mask(to), "subject": subject[:160], "kind": kind, "sent": False}
        if self.configured and to:
            try:
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = os.getenv("SMTP_FROM"), to, subject
                msg.set_content(body)
                with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
                    s.send_message(msg)
                entry["sent"] = True
            except Exception as exc:   # never break the webhook because of mail
                entry["error"] = type(exc).__name__
                logger.error("mail failed kind=%s error=%s", kind, type(exc).__name__)
        else:
            logger.warning("mail not sent (SMTP not configured) kind=%s to=%s", kind, _mask(to))
        box = self.store.get("mail:outbox") or []
        box = (box if isinstance(box, list) else [])[-49:] + [entry | {"body_preview": _redact(body)[:400]}]
        self.store.set("mail:outbox", box, 90 * 86_400)
        return entry


def _mask(e):
    e = (e or "").strip()
    return (e[:2] + "***@" + e.split("@", 1)[1]) if "@" in e else ""


def _redact(body):
    import re
    return re.sub(r"EBD[WA]-[0-9A-Z-]{20,}", "EBD?-*****-*****-*****-*****", body)
