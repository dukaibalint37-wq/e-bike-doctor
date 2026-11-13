"""Smart Hardware interest list (3.1): double opt-in, one-click unsubscribe, admin export. No pre-order, no payment.

Storage (strict persistent store, see licensing.StrictStore):
  int:r:<email_hash>  {email, name, lang, interests, role, status pending|confirmed, created, confirmed_at, consent}
  int:index           [email_hash, ...]  (for the admin list)
- pending entries expire after 7 days if not confirmed; confirmed entries are kept until unsubscribe (or 3 years without contact)
- unsubscribe DELETES the record (data minimisation) – only an anonymous counter remains
- tokens in the e-mail links are HMAC(email_hash, purpose) with INTEREST_SECRET (fallback DOWNLOAD_SECRET/QUOTA_SECRET)
"""
import hashlib
import hmac
import html as _h
import logging
import os
import re
import time

import mail_templates
from licensing import LicenseError, email_hash

logger = logging.getLogger("ebike-doctor.interest")
EMAIL_RE = re.compile(r"[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}")
INTERESTS = ("battery-bms", "sensors", "drive-measure", "connect-eval")     # the four planned feature groups on the website
ROLES = ("workshop", "rider", "dealer")
PENDING_TTL = 7 * 86_400
KEEP_TTL = 3 * 365 * 86_400
CONSENT_VERSION = "2026-11-hw1"


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class InterestList:
    def __init__(self, store, mailer, clock=time.time):
        self.store, self.mailer, self.clock = store, mailer, clock

    # ---- tokens
    def _secret(self):
        return (os.getenv("INTEREST_SECRET", "").strip() or os.getenv("DOWNLOAD_SECRET", "").strip()
                or os.getenv("QUOTA_SECRET", "").strip())

    def token(self, eh, purpose):
        sig = hmac.new(self._secret().encode(), f"{purpose}|{eh}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{eh[:40]}.{sig}"

    def _resolve(self, token, purpose):
        try:
            prefix, sig = (token or "").split(".", 1)
        except ValueError:
            return None
        if not self._secret() or not re.fullmatch(r"[a-f0-9]{40}", prefix or ""):
            return None
        idx = self.store.get("int:index") or []
        for eh in (idx if isinstance(idx, list) else []):
            if eh.startswith(prefix) and hmac.compare_digest(self.token(eh, purpose), token):
                return eh
        return None

    def _base(self):
        return os.getenv("PUBLIC_BASE_URL", "https://e-bike-doctor.onrender.com").rstrip("/")

    def _index_add(self, eh):
        idx = self.store.get("int:index") or []
        idx = idx if isinstance(idx, list) else []
        if eh not in idx:
            self.store.set("int:index", idx + [eh], KEEP_TTL)

    def _index_remove(self, eh):
        idx = self.store.get("int:index") or []
        if isinstance(idx, list) and eh in idx:
            self.store.set("int:index", [x for x in idx if x != eh], KEEP_TTL)

    # ---- public API
    def subscribe(self, email, lang, interests, role, consent, ip_hash, honeypot="", name=""):
        email = (email or "").strip()
        if honeypot:                              # bots fill the hidden field: pretend success, store nothing
            return {"ok": True, "status": "check_email"}
        if not consent:
            raise LicenseError("consent_required", 422, "Consent is required")
        if not EMAIL_RE.fullmatch(email) or len(email) > 200:
            raise LicenseError("bad_email", 422, "Invalid e-mail address")
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        if self.store.incr(f"int:ip:{day}:{ip_hash}", 2 * 86_400) > _int("INTEREST_PER_IP_PER_DAY", 10):
            raise LicenseError("rate_limited", 429, "Too many sign-ups from this network today")
        eh = email_hash(email)
        lang = mail_templates.lang_of(lang)
        clean = [i for i in (interests or []) if i in INTERESTS][:len(INTERESTS)]
        role = role if role in ROLES else ""
        rec = self.store.get("int:r:" + eh)
        if isinstance(rec, dict) and rec.get("status") == "confirmed":
            return {"ok": True, "status": "check_email"}         # same answer: does not reveal who is on the list
        if self.store.incr(f"int:em:{day}:{eh}", 2 * 86_400) > _int("INTEREST_MAILS_PER_EMAIL_PER_DAY", 2):
            return {"ok": True, "status": "check_email"}
        now = int(self.clock())
        rec = {"email": email, "name": (name or "").strip()[:80], "lang": lang, "interests": clean, "role": role, "status": "pending", "created": now,
               "consent": {"v": CONSENT_VERSION, "t": now}}
        self.store.set("int:r:" + eh, rec, PENDING_TTL)
        self._index_add(eh)
        confirm_url = f"{self._base()}/v1/interest/confirm?t={self.token(eh, 'confirm')}"
        unsub_url = f"{self._base()}/v1/interest/unsubscribe?t={self.token(eh, 'unsub')}"
        subject, text, html = mail_templates.confirm(lang, confirm_url, unsub_url)
        self.mailer.send(email, subject, text, kind="interest-confirm", html=html, headers=self.unsub_headers(eh))
        self.store.incr("int:stat:signups", KEEP_TTL)
        return {"ok": True, "status": "check_email"}

    def unsub_headers(self, eh):
        url = f"{self._base()}/v1/interest/unsubscribe?t={self.token(eh, 'unsub')}"
        return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

    def confirm(self, token):
        eh = self._resolve(token, "confirm")
        rec = self.store.get("int:r:" + eh) if eh else None
        if not isinstance(rec, dict):
            return None, "invalid"
        if rec.get("status") != "confirmed":
            rec["status"], rec["confirmed_at"] = "confirmed", int(self.clock())
            self.store.set("int:r:" + eh, rec, KEEP_TTL)
            self.store.incr("int:stat:confirmed", KEEP_TTL)
        return rec.get("lang", "en"), "confirmed"

    def unsubscribe(self, token):
        eh = self._resolve(token, "unsub")
        rec = self.store.get("int:r:" + eh) if eh else None
        if not eh:
            return None, "invalid"
        if isinstance(rec, dict):
            self.store.set("int:r:" + eh, {"status": "deleted"}, 60)
            self.store.incr("int:stat:unsubscribed", KEEP_TTL)
        self._index_remove(eh)
        return (rec or {}).get("lang", "en"), "unsubscribed"

    # ---- admin
    def entries(self):
        out = []
        idx = self.store.get("int:index") or []
        for eh in (idx if isinstance(idx, list) else []):
            rec = self.store.get("int:r:" + eh)
            if isinstance(rec, dict) and rec.get("email"):
                out.append({"id": eh[:12], **{k: rec.get(k) for k in ("email", "name", "lang", "interests", "role", "status", "created", "confirmed_at")}})
        return out

    def delete(self, short_id):
        idx = self.store.get("int:index") or []
        for eh in (idx if isinstance(idx, list) else []):
            if eh.startswith(short_id) and len(short_id) >= 12:
                self.store.set("int:r:" + eh, {"status": "deleted"}, 60)
                self._index_remove(eh)
                return True
        return False

    def stats(self):
        e = self.entries()
        return {"confirmed": sum(1 for x in e if x["status"] == "confirmed"), "pending": sum(1 for x in e if x["status"] == "pending"),
                "signups_total": int(self.store.get("int:stat:signups") or 0),
                "unsubscribed_total": int(self.store.get("int:stat:unsubscribed") or 0)}


PAGE = {
    "confirmed": {"hu": ("Feliratkozás megerősítve", "Köszönjük! Értesítünk a Smart Hardware fejlesztéséről és indulásáról. A levelek alján bármikor leiratkozhatsz."),
                  "de": ("Anmeldung bestätigt", "Danke! Wir informieren Sie über Entwicklung und Start der Smart Hardware. Abmeldung jederzeit über den Link in jeder E-Mail."),
                  "en": ("Sign-up confirmed", "Thank you! We will let you know about the development and launch of the Smart Hardware. You can unsubscribe at any time from any e-mail.")},
    "unsub_ask": {"hu": ("Leiratkozás", "Biztosan leiratkozol a Smart Hardware értesítőről? Az e-mail-címedet töröljük."),
                  "de": ("Abmelden", "Möchten Sie sich von den Smart-Hardware-Informationen abmelden? Ihre E-Mail-Adresse wird gelöscht."),
                  "en": ("Unsubscribe", "Do you want to unsubscribe from the Smart Hardware updates? Your e-mail address will be deleted.")},
    "unsubscribed": {"hu": ("Leiratkoztál", "Az e-mail-címedet töröltük az érdeklődői listáról. Több levelet nem küldünk."),
                     "de": ("Abgemeldet", "Ihre E-Mail-Adresse wurde aus der Interessentenliste gelöscht. Sie erhalten keine weiteren E-Mails."),
                     "en": ("Unsubscribed", "Your e-mail address has been deleted from the interest list. You will not receive further e-mails.")},
    "invalid": {"hu": ("A link érvénytelen vagy lejárt", "A megerősítő link 7 napig érvényes. Iratkozz fel újra a weboldalon."),
                "de": ("Link ungültig oder abgelaufen", "Der Bestätigungslink ist 7 Tage gültig. Bitte melden Sie sich auf der Website erneut an."),
                "en": ("Link invalid or expired", "The confirmation link is valid for 7 days. Please sign up again on the website.")},
}
BTN = {"hu": "Leiratkozás", "de": "Abmelden", "en": "Unsubscribe"}


def page(kind, lang, form_action=None):
    lang = mail_templates.lang_of(lang)
    title, text = PAGE[kind][lang]
    site = os.getenv("SITE_URL", "").strip()
    form = (f'<form method="post" action="{_h.escape(form_action)}"><button type="submit">{BTN[lang]}</button></form>' if form_action else "")
    back = f'<p><a href="{_h.escape(site)}">E-Bike Doctor PRO</a></p>' if site else ""
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{_h.escape(title)} – E-Bike Doctor</title><style>'
            'body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05080f;color:#e6f6fb;font:16px/1.6 Segoe UI,Arial,sans-serif;padding:16px}'
            '.c{max-width:520px;background:#0b1622;border:1px solid #1d3346;border-radius:14px;padding:28px}h1{font-size:22px;margin:0 0 10px;color:#22d3ee}'
            'button{background:#06b6d4;color:#04131a;border:0;border-radius:8px;padding:12px 22px;font-weight:700;font-size:15px;cursor:pointer}a{color:#22d3ee}'
            f'</style></head><body><div class="c"><h1>{_h.escape(title)}</h1><p>{_h.escape(text)}</p>{form}{back}</div></body></html>')
