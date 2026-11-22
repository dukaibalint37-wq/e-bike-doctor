"""Free 48-hour trial – campaign EBIKE-TRIAL2 (owner, 2026-11-22). No payment card, no payment provider involved.

Flow (double opt-in, then the ordinary licence system):
1. Website "48-hour free trial" button (or the promotion-code field with EBIKE-TRIAL2): POST /v1/trial/request
   {email, product, lang, code, consent}. The answer is always "check your e-mail" (no e-mail enumeration).
2. E-mail with a signed link (24 h). GET only shows a "Start the trial" button (mail scanners prefetch links); the POST
   issues ONE trial licence for that e-mail address (licensing.create(..., trial_hours=48)) and shows + mails the key and
   the installer link (Windows setup / Android APK from the private download bucket).
3. The 48 hours start at the first activation in the app and are enforced by the licence server (licensing.py): lease
   exp = trial end, refresh/AI/downloads refused afterwards, one trial per device, no transfer.

One trial per user = per confirmed e-mail address (trial:email:<hash>) AND per device (trial:dev:<product>:<hash>).
Unlimited different users. Switched off unless TRIAL_ENABLED=1 (deploying the code changes nothing on its own).

Storage (strict persistent store):
  trial:pending:<email_hash>  {email, product, lang, created}            24 h
  trial:email:<email_hash>    licence id of the e-mail's trial             20 years
  trial:claim:<email_hash>    atomic claim counter (no double licence on parallel clicks)
"""
import base64
import hashlib
import hmac
import html as _h
import logging
import os
import re
import time

import mail_templates
from licensing import TTL, LicenseError, email_hash, trial_of

logger = logging.getLogger("ebike-doctor.trial")
EMAIL_RE = re.compile(r"[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}")
PRODUCTS = {"windows": "EBD-WIN", "android": "EBD-AND", "EBD-WIN": "EBD-WIN", "EBD-AND": "EBD-AND"}
PENDING_TTL = 24 * 3600


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def trial_code():
    return re.sub(r"\s+", "", os.getenv("TRIAL_CODE", "EBIKE-TRIAL2")).upper()


def normalize_code(code):
    return re.sub(r"\s+", "", str(code or "")).upper()[:40]


class TrialService:
    def __init__(self, store, licenses, shop, mailer, clock=time.time):
        self.store, self.licenses, self.shop, self.mailer, self.clock = store, licenses, shop, mailer, clock
        self.stats_sink = None                    # stats.Stats (3.5)

    def _ev(self, kind, **fields):
        if self.stats_sink is not None:
            try:
                self.stats_sink.record(kind, **fields)
            except Exception:                # noqa: BLE001 - statistics never break the trial
                logger.exception("statistics event failed")

    @property
    def enabled(self):
        return os.getenv("TRIAL_ENABLED", "0").strip() == "1"

    @property
    def hours(self):
        """48 in production. A fraction (e.g. 0.05 = 3 minutes) is allowed so the staging server can test the expiry live."""
        try:
            h = float(os.getenv("TRIAL_HOURS", "48"))
        except ValueError:
            h = 48.0
        h = min(max(h, 0.01), 24 * 30)
        return int(h) if h == int(h) else h

    def _secret(self):
        return (os.getenv("TRIAL_SECRET", "").strip() or os.getenv("INTEREST_SECRET", "").strip()
                or os.getenv("DOWNLOAD_SECRET", "").strip() or os.getenv("QUOTA_SECRET", "").strip())

    def _base(self):
        return os.getenv("PUBLIC_BASE_URL", "https://e-bike-doctor.onrender.com").rstrip("/")

    # ---- signed link token: base64(eh.product.exp).sig
    def token(self, eh, product, ttl=PENDING_TTL):
        payload = f"{eh}.{product}.{int(self.clock()) + ttl}"
        sig = hmac.new(self._secret().encode(), ("trial|" + payload).encode(), hashlib.sha256).digest()[:20]
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def _resolve(self, token):
        """(email_hash, product) of a valid, unexpired token, else None."""
        try:
            body, sig = (token or "").split(".", 1)
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            eh, product, exp = payload.split(".")
            exp = int(exp)
        except (ValueError, UnicodeDecodeError):
            return None
        if not self._secret() or not re.fullmatch(r"[a-f0-9]{64}", eh) or product not in ("EBD-WIN", "EBD-AND"):
            return None
        expected = base64.urlsafe_b64encode(hmac.new(self._secret().encode(), ("trial|" + payload).encode(), hashlib.sha256).digest()[:20]).decode().rstrip("=")
        if not hmac.compare_digest(expected, sig) or exp < self.clock():
            return None
        return eh, product

    # ---- 1. request
    def request(self, email, product, lang, code, consent, ip_hash, honeypot=""):
        if not self.enabled:
            raise LicenseError("trial_unavailable", 503, "The free trial is not available right now")
        if honeypot:                                   # bots fill the hidden field: pretend success, do nothing
            return {"ok": True, "status": "check_email"}
        c = normalize_code(code)
        if c and c != trial_code():
            raise LicenseError("invalid_code", 422, "Unknown trial code")
        if not consent:
            raise LicenseError("consent_required", 422, "Consent is required")
        email = (email or "").strip()
        if not EMAIL_RE.fullmatch(email) or len(email) > 200:
            raise LicenseError("bad_email", 422, "Invalid e-mail address")
        prod = PRODUCTS.get(str(product or "").strip()) or PRODUCTS.get(str(product or "").strip().lower())
        if not prod:
            raise LicenseError("bad_product", 422, "Unknown product")
        lang = mail_templates.lang_of(lang)
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        if self.store.incr(f"trial:ip:{day}:{ip_hash}", 2 * 86_400) > _int("TRIAL_PER_IP_PER_DAY", 5):
            raise LicenseError("rate_limited", 429, "Too many trial requests from this network today")
        eh = email_hash(email)
        if self.store.incr(f"trial:em:{day}:{eh}", 2 * 86_400) > _int("TRIAL_MAILS_PER_EMAIL_PER_DAY", 3):
            return {"ok": True, "status": "check_email"}           # silently: no mail flood to one address
        self.store.incr("trial:stat:requested", TTL)
        lid = self.store.get("trial:email:" + eh)
        md = "test" if self.shop.mode == "test" else "live"
        if lid:
            self._mail_existing(lid, email, lang)
            self._ev("trq", p=prod, m=md, new=False)
            return {"ok": True, "status": "check_email"}
        self._ev("trq", p=prod, m=md, new=True)
        self.store.set("trial:pending:" + eh, {"email": email, "product": prod, "lang": lang, "created": int(self.clock())}, PENDING_TTL)
        url = f"{self._base()}/v1/trial/confirm?t={self.token(eh, prod)}"
        subject, text, html = mail_templates.trial_confirm(prod, lang, url)
        self.mailer.send(email, subject, text, kind="trial-confirm", html=html)
        return {"ok": True, "status": "check_email"}

    def _mail_existing(self, lid, email, lang):
        """Already had a trial: a still usable one is mailed again (lost key), an expired or used-up one gets the buy link."""
        try:
            rec = self.licenses.get(lid)
        except LicenseError:
            rec = None
        if rec and rec.get("status") == "active" and not self.licenses.trial_expired(rec):
            key = self.licenses.recover_key(rec)
            if key:
                subject, text, html = mail_templates.trial_key(rec["product"], lang, key, self.shop.download_link(lid, rec["product"]), bool(rec.get("test")))
                self.mailer.send(email, subject, text, kind="trial-key", html=html)
                return
        subject, text, html = mail_templates.trial_used(lang)
        self.mailer.send(email, subject, text, kind="trial-used", html=html)

    # ---- 2. confirm (POST from the confirmation page)
    def confirm(self, token):
        """Returns (lang, kind, data). kind: issued | used | invalid | unavailable."""
        if not self.enabled:
            return "en", "unavailable", {}
        r = self._resolve(token)
        if not r:
            return "en", "invalid", {}
        eh, prod = r
        existing = self.store.get("trial:email:" + eh)
        pend = self.store.get("trial:pending:" + eh)
        lang = (pend or {}).get("lang", "en") if isinstance(pend, dict) else "en"
        if existing:
            return self._show_existing(existing, lang)
        if not isinstance(pend, dict) or not pend.get("email"):
            return lang, "invalid", {}
        if self.store.incr("trial:claim:" + eh, TTL) != 1:            # a parallel click is creating it right now
            existing = self.store.get("trial:email:" + eh)
            return self._show_existing(existing, lang) if existing else (lang, "invalid", {})
        try:
            key, rec = self.licenses.create(pend["product"], pend["email"], source="trial", test=self.shop.mode == "test",
                                            trial_hours=self.hours, campaign=trial_code())
            self.store.set("trial:email:" + eh, rec["lid"], TTL)
        except LicenseError:
            self.store.set("trial:claim:" + eh, 0, 60)          # storage outage: the user can simply click again
            raise
        self.store.set("trial:pending:" + eh, {"status": "done"}, 60)
        self.store.incr("trial:stat:issued", TTL)
        self._ev("tri", p=rec["product"], lid=rec["lid"], m="test" if rec.get("test") else "live")
        link =self.shop.download_link(rec["lid"], rec["product"])
        subject, text, html = mail_templates.trial_key(rec["product"], lang, key, link, bool(rec.get("test")))
        self.mailer.send(pend["email"], subject, text, kind="trial-key", html=html)
        logger.info("trial issued %s product=%s", rec["lid"], rec["product"])
        return lang, "issued", {"key": key, "link": link, "product": rec["product"]}

    def _show_existing(self, lid, lang):
        try:
            rec = self.licenses.get(lid)
        except LicenseError:
            return lang, "used", {}
        if rec.get("status") == "active" and not self.licenses.trial_expired(rec):
            key = self.licenses.recover_key(rec)
            if key:
                return lang, "issued", {"key": key, "link": self.shop.download_link(lid, rec["product"]), "product": rec["product"]}
        return lang, "used", {}

    def check(self, token):
        """GET of the e-mail link: only tells the page what to show (never issues anything)."""
        if not self.enabled:
            return "en", "unavailable"
        r = self._resolve(token)
        if not r:
            return "en", "invalid"
        pend = self.store.get("trial:pending:" + r[0])
        lang = pend.get("lang", "en") if isinstance(pend, dict) else "en"
        return lang, "ask"

    def stats(self):
        recs = [r for r in self.licenses.all() if trial_of(r)]
        return {"enabled": self.enabled, "hours": self.hours, "code": trial_code(),
                "requested": int(self.store.get("trial:stat:requested") or 0), "issued": int(self.store.get("trial:stat:issued") or 0),
                "started": sum(1 for r in recs if trial_of(r).get("started")),
                "expired": sum(1 for r in recs if self.licenses.trial_expired(r))}


PAGE = {
    "ask": {"hu": ("48 órás ingyenes próba", "Kattints a gombra a próbalicenc kiadásához. A 48 óra az alkalmazásban történő első aktiválással indul."),
            "de": ("Kostenloser 48-Stunden-Test", "Klicke auf die Schaltfläche, um die Testlizenz zu erhalten. Die 48 Stunden beginnen mit der ersten Aktivierung in der App."),
            "en": ("Free 48-hour trial", "Click the button to get your trial licence. The 48 hours start with the first activation in the app.")},
    "issued": {"hu": ("Kész a próbalicenced", "A kulcsot e-mailben is elküldtük. Töltsd le a programot, és aktiváld a kulccsal (Licenc és vásárlás → Aktiválás)."),
               "de": ("Deine Testlizenz ist fertig", "Wir haben den Schlüssel auch per E-Mail gesendet. Lade die App herunter und aktiviere sie mit dem Schlüssel (Lizenz und Kauf → Aktivieren)."),
               "en": ("Your trial licence is ready", "We also sent the key by e-mail. Download the app and activate it with the key (Licence and purchase → Activate).")},
    "used": {"hu": ("A próbát már igénybe vetted", "Ehhez az e-mail-címhez már tartozik ingyenes próba. A teljes PRO licencet a webáruházban vásárolhatod meg."),
             "de": ("Test bereits genutzt", "Für diese E-Mail-Adresse wurde der kostenlose Test bereits genutzt. Die volle PRO-Lizenz gibt es im Shop."),
             "en": ("Trial already used", "A free trial has already been used with this e-mail address. The full PRO licence is available in the shop.")},
    "invalid": {"hu": ("A link érvénytelen vagy lejárt", "A megerősítő link 24 óráig érvényes. Kérj új próbát a weboldalon."),
                "de": ("Link ungültig oder abgelaufen", "Der Bestätigungslink ist 24 Stunden gültig. Bitte fordere den Test auf der Website erneut an."),
                "en": ("Link invalid or expired", "The confirmation link is valid for 24 hours. Please request the trial again on the website.")},
    "unavailable": {"hu": ("A próba most nem érhető el", "Az ingyenes próba jelenleg szünetel. Próbáld újra később."),
                    "de": ("Test derzeit nicht verfügbar", "Der kostenlose Test ist derzeit pausiert. Bitte später erneut versuchen."),
                    "en": ("Trial not available", "The free trial is paused at the moment. Please try again later.")},
}
LBL = {"hu": {"btn": "Próba indítása", "key": "Próbakulcs", "dl": "Program letöltése", "shop": "Webáruház"},
       "de": {"btn": "Test starten", "key": "Testschlüssel", "dl": "App herunterladen", "shop": "Shop"},
       "en": {"btn": "Start the trial", "key": "Trial key", "dl": "Download the app", "shop": "Shop"}}


def page(kind, lang, data=None, form_action=None):
    lang = mail_templates.lang_of(lang)
    title, text = PAGE[kind][lang]
    lb, e, data = LBL[lang], _h.escape, data or {}
    body = f"<h1>{e(title)}</h1><p>{e(text)}</p>"
    if kind == "ask" and form_action:
        body += f'<form method="post" action="{e(form_action)}"><button type="submit">{e(lb["btn"])}</button></form>'
    if kind == "issued" and data.get("key"):
        body += (f'<p class="l">{e(lb["key"])}</p><p class="k">{e(data["key"])}</p>'
                 f'<p><a class="b" href="{e(data.get("link", ""))}">{e(lb["dl"])}</a></p>')
    if kind == "used":
        body += f'<p><a class="b" href="{e(mail_templates.site_url(lang, "shop"))}">{e(lb["shop"])}</a></p>'
    body += f'<p><a href="{e(mail_templates.site_url(lang))}">E-Bike Doctor PRO</a></p>'
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{e(title)} – E-Bike Doctor PRO</title><style>'
            'body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05080f;color:#e6f6fb;font:16px/1.6 Segoe UI,Arial,sans-serif;padding:16px}'
            '.c{max-width:540px;background:#0b1622;border:1px solid #1d3346;border-radius:14px;padding:28px}h1{font-size:22px;margin:0 0 10px;color:#22d3ee}'
            'button,.b{display:inline-block;background:#06b6d4;color:#04131a;border:0;border-radius:8px;padding:12px 22px;font-weight:700;font-size:15px;cursor:pointer;text-decoration:none}'
            'a{color:#22d3ee}.l{margin:16px 0 4px;font-size:13px;color:#9fb6c3}.k{font:700 18px Consolas,monospace;background:#062530;border:1px solid #155e75;'
            'padding:12px;border-radius:8px;word-break:break-all;user-select:all}'
            f'</style></head><body><div class="c">{body}</div></body></html>')
