"""Own-website shop integration (Lemon Squeezy as Merchant of Record) – SHOP_MODE=test by default.

Webhook: POST /v1/shop/lemonsqueezy/webhook
- Signature: X-Signature = hex(HMAC-SHA256(raw body, LEMONSQUEEZY_WEBHOOK_SECRET)) – constant-time compare; unsigned/forged → 401.
- Events: order_created (paid) → licence + download entitlement + e-mail; order_refunded → licence revoked.
- Idempotent: each (event, order) is processed once (Lemon Squeezy retries webhooks).
- SHOP_MODE=test: only test_mode webhooks are processed (licences flagged test=True); live orders are ignored and logged.
  SHOP_MODE=live is set only after explicit approval.
Product mapping: LS_PRODUCT_MAP='{"<variant or product id>":"EBD-WIN", "<id>":"EBD-AND"}'.
Downloads: short-lived HMAC-signed tokens bound to an ACTIVE licence → 302 to DOWNLOAD_URL_EBD_WIN / DOWNLOAD_URL_EBD_AND.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

import mail_templates
import s3presign
from licensing import PRODUCTS, LicenseError, email_hash

logger = logging.getLogger("ebike-doctor.shop")
PRICES = {"EBD-WIN": ("19.99", "EUR"), "EBD-AND": ("14.99", "EUR")}   # one EUR store, one-time purchases
EMAIL_RE = re.compile(r"[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}")


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def verify_signature(raw: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature or not raw:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _cents(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def order_money(attrs):
    """Amounts of a Lemon Squeezy order (integer cents, as LS sends them). Lemon Squeezy is the Merchant of Record:
    it collects and remits the VAT; its own fee is not part of the order object (only on the LS payout pages)."""
    rate = attrs.get("tax_rate", 0)
    try:
        rate = float(rate or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return {"total": _cents(attrs.get("total")), "subtotal": _cents(attrs.get("subtotal")), "tax": _cents(attrs.get("tax")),
            "discount": _cents(attrs.get("discount_total")), "currency": str(attrs.get("currency", "") or "")[:3],
            "tax_name": str(attrs.get("tax_name", "") or "")[:40], "tax_rate": rate, "tax_inclusive": bool(attrs.get("tax_inclusive")),
            "total_usd": _cents(attrs.get("total_usd")), "tax_usd": _cents(attrs.get("tax_usd"))}


def product_map():
    try:
        m = json.loads(os.getenv("LS_PRODUCT_MAP", "{}") or "{}")
        return {str(k): v for k, v in m.items() if v in PRODUCTS}
    except ValueError:
        return {}


class Shop:
    def __init__(self, store, licenses, mailer, clock=time.time):
        self.store, self.licenses, self.mailer, self.clock = store, licenses, mailer, clock

    @property
    def mode(self):
        return "live" if os.getenv("SHOP_MODE", "test").strip().lower() == "live" else "test"

    def _secret(self):
        return os.getenv("DOWNLOAD_SECRET", "").strip() or os.getenv("QUOTA_SECRET", "").strip()

    # ------------------------------------------------------------------ webhook
    def handle_webhook(self, raw: bytes, signature: str):
        if not verify_signature(raw, signature, os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "").strip()):
            self.store.incr("shop:rejected_sig", 400 * 86_400)
            raise LicenseError("bad_signature", 401, "Invalid webhook signature")
        try:
            ev = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise LicenseError("bad_payload", 400, "Invalid JSON") from None
        meta, data = ev.get("meta") or {}, ev.get("data") or {}
        name, test_mode = meta.get("event_name", ""), bool(meta.get("test_mode"))
        order_id = str(data.get("id", ""))
        if data.get("type") != "orders" or not order_id:
            return {"ok": True, "ignored": "not an order event"}
        if self.mode == "test" and not test_mode:
            logger.warning("LIVE order webhook ignored because SHOP_MODE=test order=%s", order_id)
            return {"ok": True, "ignored": "live order while SHOP_MODE=test"}
        if self.mode == "live" and test_mode and os.getenv("SHOP_ACCEPT_TEST_ORDERS", "0") != "1":
            # 3.2: test purchases never create licences on the LIVE shop (test data stays in the staging environment)
            logger.warning("TEST order webhook ignored because SHOP_MODE=live order=%s", order_id)
            return {"ok": True, "ignored": "test order while SHOP_MODE=live"}
        attrs = data.get("attributes") or {}
        done_key = f"shop:evt:{name}:{order_id}"
        if name == "order_refunded":      # a partial refund and a later full refund of the same order are two different events
            done_key += ":" + str(attrs.get("status", ""))
        if self.store.get(done_key):
            return {"ok": True, "duplicate": True}
        custom = meta.get("custom_data") or {}
        lang = mail_templates.lang_of(custom.get("lang") if isinstance(custom, dict) else "")
        if name == "order_created":
            result = self._order_created(order_id, attrs, test_mode, lang)
        elif name == "order_refunded":
            result = self._order_refunded(order_id, attrs, test_mode)
        else:
            result = {"ok": True, "ignored": name}
        self.store.set(done_key, int(self.clock()), 400 * 86_400)
        return result

    def _products_of(self, attrs):
        item = attrs.get("first_order_item") or {}
        m = product_map()
        p = m.get(str(item.get("variant_id", ""))) or m.get(str(item.get("product_id", "")))
        return [p] if p else []

    def _order_created(self, order_id, attrs, test_mode, lang=""):
        if attrs.get("status") != "paid":
            return {"ok": True, "ignored": "status " + str(attrs.get("status"))}
        products = self._products_of(attrs)
        if not products:
            logger.error("order %s: unknown product/variant – check LS_PRODUCT_MAP", order_id)
            return {"ok": True, "ignored": "unknown product"}
        email, name = attrs.get("user_email", ""), attrs.get("user_name", "")
        money = order_money(attrs)
        issued = []
        for prod in products:
            key, rec = self.licenses.create(prod, email=email, name=name, test=test_mode, source="lemonsqueezy", order_id=order_id)
            if key is None:          # already issued for this order (retry)
                continue
            month = time.strftime("%Y%m", time.gmtime(self.clock()))
            md = "test" if test_mode else "live"
            self.store.incr(f"shop:sales:{month}:{prod}:{md}", 800 * 86_400)
            self.store.incr(f"shop:revenue_cents:{month}:{money['currency'] or 'EUR'}:{md}", 800 * 86_400, money["total"])
            if money["tax"]:
                self.store.incr(f"shop:tax_cents:{month}:{money['currency'] or 'EUR'}:{md}", 800 * 86_400, money["tax"])
            link = self.download_link(rec["lid"], prod)
            subject, text, html = mail_templates.purchase(prod, lang, name, key, link, test_mode)
            sent = self.mailer.send(email, subject, text, kind="purchase", html=html)
            issued.append(rec["lid"])
            self._log_order({"t": int(self.clock()), "order": order_id, "product": prod, "mode": md, **money, "lid": rec["lid"],
                             "email": rec.get("email_masked", ""), "lang": lang, "mail_sent": bool(sent.get("sent")), "refunded": False})
            if not sent.get("sent"):
                logger.error("order %s: licence %s created but the e-mail was not sent (%s) - buyer can use the re-send form",
                             order_id, rec["lid"], sent.get("error"))
        return {"ok": True, "licences": issued}

    def _order_refunded(self, order_id, attrs, test_mode=False):
        orders = self.store.get("shop:orders") or []
        orders = orders if isinstance(orders, list) else []
        month = time.strftime("%Y%m", time.gmtime(self.clock()))
        # refunded_amount is cumulative in Lemon Squeezy: count only the part not yet recorded for this order
        refunded = _cents(attrs.get("refunded_amount"))
        mine = [o for o in orders if o.get("order") == order_id]
        before = max([_cents(o.get("refunded_cents")) for o in mine] + [0])
        delta = refunded - before if refunded > before else 0
        cur = (mine[0].get("currency") if mine else "") or str(attrs.get("currency", "") or "EUR")[:3]
        md = (mine[0].get("mode") if mine else "") or ("test" if test_mode else "live")
        if delta:
            self.store.incr(f"shop:refunded_cents:{month}:{cur}:{md}", 800 * 86_400, delta)
        for o in mine:
            o["refunded_cents"] = max(before, refunded)
        if attrs.get("status") == "partial_refund":
            # partial refund (e.g. goodwill discount): the buyer keeps the licence; only recorded
            for o in mine:
                o["partial_refund_cents"] = refunded
            self.store.set("shop:orders", orders, 800 * 86_400)
            return {"ok": True, "partial_refund": True, "revoked": []}
        revoked = []
        for prod in PRODUCTS:
            lid = self.store.get(f"lic:order:{order_id}:{prod}")
            if lid and self.licenses.get(lid).get("status") == "active":
                self.licenses.revoke(lid, reason="refund " + order_id)
                revoked.append((lid, prod))
        mode = "test" if test_mode or any(self.licenses.get(l).get("test") for l, _ in revoked) else "live"
        self.store.incr(f"shop:refunds:{month}" + (":test" if mode == "test" else ""), 800 * 86_400)
        lang = ""
        for o in orders:
            if o.get("order") == order_id:
                o["refunded"] = True
                lang = o.get("lang", "") or lang
        self.store.set("shop:orders", orders, 800 * 86_400)
        email = attrs.get("user_email", "")
        for lid, prod in revoked:      # buyer notice: licence revoked (the money is returned by Lemon Squeezy)
            if not email:
                break
            subject, text, html = mail_templates.refund(prod, lang, order_id, self.licenses.get(lid).get("key_hint", ""), mode == "test")
            sent = self.mailer.send(email, subject, text, kind="refund", html=html)
            if not sent.get("sent"):
                logger.error("order %s refunded, licence %s revoked, notice e-mail not sent (%s)", order_id, lid, sent.get("error"))
        return {"ok": True, "revoked": [l for l, _ in revoked]}

    # ------------------------------------------------------------------ downloads
    def download_token(self, lid, product, ttl=48 * 3600):
        exp = int(self.clock()) + ttl
        payload = f"{lid}.{product}.{exp}"
        sig = hmac.new(self._secret().encode(), payload.encode(), hashlib.sha256).digest()[:20]
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def download_link(self, lid, product):
        base = os.getenv("PUBLIC_BASE_URL", "https://e-bike-doctor.onrender.com").rstrip("/")
        return f"{base}/v1/shop/download?t={self.download_token(lid, product)}"

    def resolve_download(self, token):
        """Returns the redirect URL for a valid token of an ACTIVE licence; raises otherwise."""
        try:
            body, sig = token.split(".", 1)
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            lid, product, exp = payload.split(".")
            expected = base64.urlsafe_b64encode(hmac.new(self._secret().encode(), payload.encode(), hashlib.sha256).digest()[:20]).decode().rstrip("=")
        except (ValueError, UnicodeDecodeError):
            raise LicenseError("bad_token", 403, "Invalid download link") from None
        if not self._secret() or not hmac.compare_digest(expected, sig):
            raise LicenseError("bad_token", 403, "Invalid download link")
        if int(exp) < self.clock():
            raise LicenseError("expired", 403, "Download link expired – request a new one with your licence key")
        rec = self.licenses.get(lid)
        if rec["status"] != "active" or rec["product"] != product:
            raise LicenseError("revoked", 403, "Licence is not active")
        # private bucket (pre-signed, a few minutes) preferred; a plain DOWNLOAD_URL_* is the legacy fallback
        url = s3presign.object_url(product) or os.getenv("DOWNLOAD_URL_" + product.replace("-", "_"), "").strip()
        if not url:
            raise LicenseError("not_configured", 503, "Download location not configured")
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        if self.store.incr(f"shop:dl:{day}:{lid}", 2 * 86_400) > _int("DOWNLOADS_PER_LICENCE_PER_DAY", 10):
            raise LicenseError("download_limit", 429, "Too many downloads for this licence today - try again tomorrow")
        self.store.incr(f"shop:downloads:{product}", 800 * 86_400)
        self.store.incr(f"shop:dlday:{day}:{product}:{'test' if rec.get('test') else 'live'}", 400 * 86_400)
        return url

    def new_download_link_for_key(self, key):
        rec = self.licenses.find_by_key(key)
        if rec["status"] != "active":
            raise LicenseError("revoked", 403, "Licence is not active")
        return self.download_link(rec["lid"], rec["product"])

    # ------------------------------------------------------------------ "lost my key / link" (website form)
    def resend(self, email, lang, ip_hash):
        """Mails the active licences of `email` to that same address only. The response never reveals whether the address
        is known (no enumeration); rate-limited per address and per network."""
        email = (email or "").strip()
        if not EMAIL_RE.fullmatch(email):
            raise LicenseError("bad_email", 422, "Invalid e-mail address")
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        if self.store.incr(f"shop:resend_ip:{day}:{ip_hash}", 2 * 86_400) > _int("RESEND_PER_IP_PER_DAY", 10):
            raise LicenseError("rate_limited", 429, "Too many requests today")
        if self.store.incr(f"shop:resend_em:{day}:{email_hash(email)}", 2 * 86_400) > _int("RESEND_PER_EMAIL_PER_DAY", 3):
            return {"ok": True}
        items = []
        for rec in self.licenses.by_email(email):
            if rec.get("status") != "active":
                continue
            items.append((rec["product"], self.licenses.recover_key(rec), self.download_link(rec["lid"], rec["product"])))
        if items or os.getenv("RESEND_REPLY_WHEN_UNKNOWN", "1") == "1":
            subject, text, html = mail_templates.resend(lang, items)
            self.mailer.send(email, subject, text, kind="resend", html=html)
        return {"ok": True}

    # ------------------------------------------------------------------ orders log / test-data separation (3.2)
    def _log_order(self, entry):
        orders = self.store.get("shop:orders") or []
        orders = (orders if isinstance(orders, list) else [])[-499:] + [entry]
        self.store.set("shop:orders", orders, 800 * 86_400)

    def orders(self, mode=None):
        o = self.store.get("shop:orders") or []
        o = [x for x in (o if isinstance(o, list) else []) if mode in (None, x.get("mode"))]
        return list(reversed(o))

    def downloads_daily(self, days=30):
        from store import mget
        dlist = [time.strftime("%Y%m%d", time.gmtime(self.clock() - i * 86_400)) for i in range(days)]
        cols = [f"{p}:{m}" for p in PRODUCTS for m in ("live", "test")]
        vals = mget(self.store, [f"shop:dlday:{d}:{c}" for d in dlist for c in cols])
        return [{"day": d, **{c: int(vals[i * len(cols) + j] or 0) for j, c in enumerate(cols)}} for i, d in enumerate(dlist)]

    def purge_test_data(self):
        """Removes TEST purchases from the live view: test licences are revoked + flagged 'purged' (they stop working
        immediately, the audit trail stays), test orders leave the order log, test counters are zeroed."""
        purged = []
        for rec in self.licenses.all():
            if rec.get("test") and not rec.get("purged"):
                if rec.get("status") == "active":
                    self.licenses.revoke(rec["lid"], reason="test data purge")
                rec = self.licenses.get(rec["lid"])
                rec["purged"] = True
                self.licenses._put(rec)
                purged.append(rec["lid"])
        orders = self.store.get("shop:orders") or []
        if isinstance(orders, list):
            self.store.set("shop:orders", [o for o in orders if o.get("mode") != "test"], 800 * 86_400)
        t = time.gmtime(self.clock())
        y, m = t.tm_year, t.tm_mon
        for _ in range(24):
            key = f"{y}{m:02d}"
            for p in PRODUCTS:
                self.store.set(f"shop:sales:{key}:{p}:test", 0, 60)
            for cur in ("EUR", "HUF", "USD"):
                for kind in ("revenue", "tax", "refunded"):
                    self.store.set(f"shop:{kind}_cents:{key}:{cur}:test", 0, 60)
            self.store.set(f"shop:refunds:{key}:test", 0, 60)
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        return {"purged_licences": purged}

    # ------------------------------------------------------------------ stats
    def stats(self, months=12):
        from store import mget
        t = time.gmtime(self.clock())
        y, m, mlist = t.tm_year, t.tm_mon, []
        for _ in range(months):
            mlist.append(f"{y}{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        fields = [(f"{p}:{mode}", f"shop:sales:{{k}}:{p}:{mode}") for p in PRODUCTS for mode in ("live", "test")]
        fields += [("refunds", "shop:refunds:{k}"), ("refunds_test", "shop:refunds:{k}:test")]
        money_kinds = ("revenue", "tax", "refunded")
        fields += [(("" if mode == "live" else "test_") + kind + "_" + cur, f"shop:{kind}_cents:{{k}}:{cur}:{mode}")
                   for kind in money_kinds for cur in ("EUR", "HUF", "USD") for mode in ("live", "test")]
        vals = mget(self.store, [tpl.format(k=k) for k in mlist for _, tpl in fields])
        out = []
        for i, k in enumerate(mlist):
            row = {"month": k}
            for j, (name, _) in enumerate(fields):
                v = int(vals[i * len(fields) + j] or 0)
                if name.split("_")[0] in money_kinds or name.startswith(tuple("test_" + m for m in money_kinds)):
                    if v:
                        row[name] = v / 100
                else:
                    row[name] = v
            out.append(row)
        return {"mode": self.mode, "months": out, "rejected_signatures": int(self.store.get("shop:rejected_sig") or 0),
                "downloads": {p: int(self.store.get(f"shop:downloads:{p}") or 0) for p in PRODUCTS}}
