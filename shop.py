"""Own-website shop integration – SHOP_MODE=test by default.

Stripe Checkout (3.3, primary): one-time payments (mode=payment, never a subscription).
- POST /v1/shop/stripe/checkout {product, lang} → hosted Checkout Session URL. Prices: STRIPE_PRICE_MAP
  '{"EBD-WIN":"price_...","EBD-AND":"price_..."}' (one-time Prices in the Stripe dashboard) or, without it, the shop
  prices below as inline price_data. The key mode must match SHOP_MODE (sk_test_ ↔ test, sk_live_ ↔ live), else 503.
- POST /v1/shop/stripe/webhook: Stripe-Signature verified with STRIPE_WEBHOOK_SECRET (timestamp tolerance, constant time).
  checkout.session.completed / .async_payment_succeeded → the session is RE-READ from the Stripe API and a licence is
  issued only when it is paid, a one-time payment, in the expected mode and for a known product (else nothing is issued);
  charge.refunded (full) → licence revoked; partial refund → recorded only; charge.refund.updated (failed/canceled) and
  charge.dispute.created/.closed → owner notice (a LOST dispute revokes like a full refund).
- Manual invoicing: Checkout requires the billing address (and offers a tax ID field); the owner (OWNER_NOTIFY_EMAIL, else
  MAIL_REPLY_TO) gets one notice per sale with the invoicing data and one per refund (storno invoice).
- Duplicates: every event id is processed once, a per-session lock stops concurrent deliveries, and licensing.create is
  idempotent per (session, product) → never two licences or two licence e-mails for one payment.

Lemon Squeezy (legacy, store closing; kept until removed) – webhook: POST /v1/shop/lemonsqueezy/webhook
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
import stripe_pay
from licensing import PRODUCTS, LicenseError, decrypt_key, email_hash, encrypt_key

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


PRODUCT_ALIASES = {"windows": "EBD-WIN", "win": "EBD-WIN", "ebd-win": "EBD-WIN", "android": "EBD-AND", "and": "EBD-AND", "ebd-and": "EBD-AND"}
STRIPE_PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")


def price_cents(product):
    from decimal import Decimal
    return int(Decimal(PRICES[product][0]) * 100)


def stripe_price_map():
    """Optional one-time Prices created in the Stripe dashboard: STRIPE_PRICE_MAP='{"EBD-WIN":"price_...","EBD-AND":"price_..."}'.
    Without it the shop prices (PRICES) are sent as inline price_data - the same amounts as on the website."""
    try:
        m = json.loads(os.getenv("STRIPE_PRICE_MAP", "{}") or "{}")
        return {k: str(v) for k, v in m.items() if k in PRICES and str(v).startswith("price_")}
    except (ValueError, AttributeError):
        return {}


class Shop:
    def __init__(self, store, licenses, mailer, clock=time.time, stripe=None):
        self.store, self.licenses, self.mailer, self.clock = store, licenses, mailer, clock
        self.stripe = stripe or stripe_pay.StripeClient()

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
        return self._fulfil(order_id, products, attrs.get("user_email", ""), attrs.get("user_name", ""), order_money(attrs),
                            test_mode, lang, "lemonsqueezy")

    def _fulfil(self, order_id, products, email, name, money, test_mode, lang, source, buyer=None):
        """Licence + download entitlement + ONE purchase e-mail per product of a VERIFIED paid order. Idempotent per
        (order, product): licensing.create returns key=None for an order that already has its licence.
        3.3: the owner gets ONE sale notice per order with the buyer's invoicing data (manual invoicing)."""
        issued, sold, recovered = [], [], []
        md = "test" if test_mode else "live"
        for prod in products:
            key, rec = self.licenses.create(prod, email=email, name=name, test=test_mode, source=source, order_id=order_id)
            lid = rec["lid"]
            if key is not None:
                issued.append(lid)
            # crash-safe (3.3): every step below is guarded by its own marker, so a retry after an interruption (server
            # asleep / timeout / restart between the steps) completes what is missing and never repeats what is done
            if self.store.incr(f"shop:booked:{order_id}:{prod}", 800 * 86_400) == 1:
                month = time.strftime("%Y%m", time.gmtime(self.clock()))
                self.store.incr(f"shop:sales:{month}:{prod}:{md}", 800 * 86_400)
                self.store.incr(f"shop:revenue_cents:{month}:{money['currency'] or 'EUR'}:{md}", 800 * 86_400, money["total"])
                if money["tax"]:
                    self.store.incr(f"shop:tax_cents:{month}:{money['currency'] or 'EUR'}:{md}", 800 * 86_400, money["tax"])
            if self.store.get(f"mail:once:purchase:{lid}"):
                if not any(o.get("lid") == lid for o in self.orders()):   # attempt started but its result was never recorded
                    self._log_order({"t": int(self.clock()), "order": order_id, "product": prod, "mode": md, **money, "lid": lid,
                                     "email": rec.get("email_masked", ""), "lang": lang, "mail_sent": False, "mail_state": "interrupted",
                                     "refunded": False, "provider": source})
                    self._mail_list_put("mail:uncertain", {"id": f"purchase-{lid}", "kind": "purchase", "lid": lid, "product": prod, "lang": lang,
                                                           "order": str(order_id), "test": bool(test_mode), "to_enc": encrypt_key(email, "mailq:" + lid),
                                                           "to": rec.get("email_masked", ""), "t": int(self.clock()), "attempts": 1, "error": "interrupted"})
                    self._alert_delivery(order_id, lid, prod, email, "interrupted", test_mode)
                continue                                       # licence AND its mail attempt already done → a plain retry
            if key is None:
                recovered.append(lid)                          # licence existed but its e-mail was never attempted
                logger.warning("order %s: licence %s existed without a purchase e-mail - completing now", order_id, lid)
            state = self._send_once("purchase", lid, prod, lang, email, order_id, test_mode, key=key, name=name)
            self._log_order({"t": int(self.clock()), "order": order_id, "product": prod, "mode": md, **money, "lid": lid,
                             "email": rec.get("email_masked", ""), "lang": lang, "mail_sent": state == "sent", "mail_state": state,
                             "refunded": False, "provider": source})
            if state != "sent":
                logger.error("order %s: licence %s created, purchase e-mail state=%s", order_id, lid, state)
                self._alert_delivery(order_id, lid, prod, email, state, test_mode)
            sold.append((prod, lid, rec.get("key_hint", ""), state))
        if sold:
            self._notify_owner_sale(order_id, sold, email, name, money, test_mode, lang, source, buyer or {})
        self.process_mail_queue(limit=5)          # opportunistic: queued mails go out as soon as a provider works again
        return {"ok": True, "licences": issued, **({"recovered": recovered} if recovered else {})}

    # ------------------------------------------------------------------ owner notices (3.3: manual invoicing)
    @staticmethod
    def owner_address():
        return (os.getenv("OWNER_NOTIFY_EMAIL", "").strip() or os.getenv("MAIL_REPLY_TO", "").strip())

    def _owner_mail(self, once_key, subject, lines):
        """Owner notice: at most once per event (never blocks or fails a webhook); text only, never a licence key."""
        to = self.owner_address()
        if not to or not EMAIL_RE.fullmatch(to):
            logger.warning("owner notice not sent (OWNER_NOTIFY_EMAIL / MAIL_REPLY_TO missing): %s", subject)
            return "no_owner_address"
        if self.store.incr("mail:once:" + once_key, 800 * 86_400) != 1:
            return "duplicate_blocked"
        r = self.mailer.send(to, subject, "\n".join(lines), kind="owner", idempotency_key="ebd-" + once_key.replace(":", "-")[:200])
        if not r.get("sent"):
            logger.error("owner notice not delivered (%s): %s", r.get("error", "?"), subject)
            if not r.get("uncertain"):        # definite failure (provider down / quota): kept and re-sent with the mail queue
                self._mail_list_put("mail:owner_pending", {"id": once_key, "subject": subject, "text": "\n".join(lines),
                                                           "t": int(self.clock()), "attempts": 1, "error": r.get("error", "")})
        return "sent" if r.get("sent") else "failed"

    def _retry_owner_mails(self, limit=10):
        lst = self.store.get("mail:owner_pending") or []
        lst = lst if isinstance(lst, list) else []
        to, keep = self.owner_address(), []
        for i, item in enumerate(lst):
            if i >= limit or not to or item.get("attempts", 0) >= 50:
                keep.append(item); continue
            r = self.mailer.send(to, item["subject"], item["text"], kind="owner-queued", idempotency_key="ebd-" + item["id"].replace(":", "-")[:200])
            if not r.get("sent") and not r.get("uncertain"):
                item.update(attempts=item.get("attempts", 0) + 1, error=r.get("error", "")); keep.append(item)
        if lst:
            self.store.set("mail:owner_pending", keep, 30 * 86_400)

    def _alert_delivery(self, order_id, lid, prod, email, state, test_mode):
        """3.3: the owner is told about EVERY licence e-mail that did not certainly reach the buyer."""
        tag = "[TESZT] " if test_mode else ""
        why = {"queued": "a levélküldő most nem fogadta el (szolgáltatói hiba / kvóta). Automatikusan újrapróbáljuk, amint működik (sorban áll).",
               "uncertain": "BIZONYTALAN: a küldés időtúllépéssel vagy szerverhibával zárult – lehet, hogy megérkezett. Automatikusan NEM küldjük újra (nehogy kétszer menjen).",
               "interrupted": "a feldolgozás a küldés közben megszakadt (szerver-újraindulás / alvás). Lehet, hogy megérkezett – automatikusan NEM küldjük újra.",
               "no_email": "a vásárlónak nincs érvényes e-mail-címe.", "no_key": "a licenckulcs nem állítható vissza a levélhez.",
               "gave_up": "14 nap / 30 próbálkozás után sem sikerült elküldeni – automatikusan már nem próbáljuk."}.get(state, state)
        lines = [f"{tag}A licenclevél NEM biztosan érkezett meg a vásárlóhoz.", "", f"Ok: {why}", "",
                 f"Rendelés: {order_id}", f"Licenc: {lid} ({prod})", f"Vásárló e-mail: {email or '-'}", "",
                 "Teendő:",
                 "- Admin → Levelezés: a sorban álló / bizonytalan levelek; bizonytalan levélnél nézd meg a Resend naplóját, és ha nem ment ki: „Újraküldés”.",
                 "- Vagy a vásárló a weboldalon az „Elveszett licenckulcs” űrlappal maga is újrakérheti (a vásárlási e-mail-címére megy).",
                 "", "A licenc maga rendben létrejött és aktiválható; csak a levél kézbesítése kérdéses."]
        return self._owner_mail(f"owner-delivery:{lid}:{state}", f"{tag}Licenclevél kézbesítése sikertelen ({state}): {lid}", lines)

    @staticmethod
    def _amount(cents, cur):
        return f"{cents / 100:.2f} {cur}".strip()

    def _notify_owner_sale(self, order_id, sold, email, name, money, test_mode, lang, source, buyer):
        cur = money.get("currency") or "EUR"
        names = ", ".join(mail_templates.NAMES[p]["hu"] for p, _, _, _ in sold)
        tag = "[TESZT] " if test_mode else ""
        addr = buyer.get("address") or {}
        addr_line = ", ".join(str(addr.get(k)) for k in ("line1", "line2", "postal_code", "city", "state", "country") if addr.get(k))
        tax_ids = ", ".join(f"{t.get('value', '')} ({t.get('type', '')})" for t in (buyer.get("tax_ids") or []) if isinstance(t, dict))
        pi = buyer.get("payment_intent") or ""
        dash = (f"https://dashboard.stripe.com/{'test/' if test_mode else ''}payments/{pi}" if source == "stripe" and pi else "")
        lines = ([tag + "TESZTVÁSÁRLÁS – nem valódi fizetés, számlát NEM kell kiállítani.", ""] if test_mode else
                 ["Új sikeres vásárlás. A számlát kézzel kell kiállítani és e-mailben elküldeni a vásárlónak.", ""])
        lines += [f"Termék: {names}",
                  f"Fizetett összeg (bruttó): {self._amount(money.get('total', 0), cur)}"
                  + (f" · a vevő {money['paid_currency']}-ban fizetett: {self._amount(money.get('paid_total', 0), money['paid_currency'])}"
                     " (Stripe-átváltás; a számla pénzneméről a könyvelővel egyeztess)" if money.get("paid_currency") else "")
                  + (f" · ebből adó: {self._amount(money['tax'], cur)}" if money.get("tax") else "")
                  + (f" · kedvezmény: {self._amount(money['discount'], cur)}" if money.get("discount") else ""),
                  f"Időpont: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(self.clock()))}",
                  f"Fizetési szolgáltató: {source} · rendelés/munkamenet: {order_id}" + (f" · fizetés: {pi}" if pi else ""),
                  "", "SZÁMLÁZÁSI ADATOK",
                  f"Név: {buyer.get('name') or name or '-'}"]
        if buyer.get("business_name"):
            lines.append(f"Cégnév: {buyer['business_name']}")
        lines += [f"E-mail: {email}", f"Számlázási cím: {addr_line or '-'}", f"Adószám: {tax_ids or '- (magánszemély)'}",
                  f"Vásárló nyelve: {lang or 'en'}", "", "LICENC"]
        lines += [f"{lid} · {p} · kulcs vége …{hint} · licenclevél: {st}" for p, lid, hint, st in sold]
        if dash:
            lines += ["", "Stripe: " + dash]
        lines += ["", "(Automatikus értesítés – a licenckulcsot ez a levél nem tartalmazza.)"]
        subject = f"{tag}Új vásárlás: {names} – {self._amount(money.get('total', 0), cur)}" + ("" if test_mode else " – számla kiállítandó")
        return self._owner_mail(f"owner-sale:{order_id}", subject, lines)

    # ------------------------------------------------------------------ Stripe Checkout (3.3)
    def stripe_status(self):
        """Admin view - booleans and modes only, never a key."""
        pk = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
        return {"secret_key": self.stripe.configured, "key_mode": self.stripe.mode, "publishable_key": bool(pk),
                "publishable_mode": stripe_pay.key_mode(pk), "webhook_secret": bool(os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()),
                "price_map": sorted(stripe_price_map()), "mode_matches_shop": bool(self.stripe.mode) and self.stripe.mode == self.mode,
                "checkout_ready": self.stripe.configured and self.stripe.mode == self.mode}

    def _stripe_ready(self):
        """A key whose mode differs from SHOP_MODE never takes a payment (no live charge from the test shop and vice versa)."""
        if not self.stripe.configured:
            raise LicenseError("not_configured", 503, "Online payment is not available yet")
        if self.stripe.mode != self.mode:
            logger.error("Stripe key mode %r does not match SHOP_MODE=%s - checkout refused", self.stripe.mode, self.mode)
            raise LicenseError("not_configured", 503, "Online payment is not available yet")

    def _return_base(self, origin):
        """Where Stripe sends the buyer back: the website the purchase started on, if it is an allowed CORS origin (e.g. the
        local test site http://localhost:8099 on staging); otherwise SITE_URL. Live mode: https origins only."""
        origin = (origin or "").strip().rstrip("/")
        allowed = [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
        if origin and origin in allowed and (origin.startswith("https://") or self.mode == "test"):
            return origin
        return ""

    def _site_page(self, base, lang, page):
        return (base + mail_templates.SITE_PAGES[page][lang]) if base else mail_templates.site_url(lang, page)

    def stripe_checkout(self, product, lang, consent=False, origin=""):
        prod = PRODUCT_ALIASES.get(str(product or "").strip().lower())
        if prod not in PRICES:
            raise LicenseError("bad_product", 422, "Unknown product")
        if consent is not True:
            # EU digital content: express consent to immediate delivery + acknowledgement of losing the withdrawal right
            # (asked on the website before the redirect); recorded in the session metadata as evidence
            raise LicenseError("consent_required", 422, "Consent to immediate delivery is required")
        lang = mail_templates.lang_of(lang)
        self._stripe_ready()
        meta = {"product": prod, "lang": lang, "shop": "ebd-website",
                "withdrawal_waiver": "accepted " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))}
        price_id = stripe_price_map().get(prod)
        if price_id:
            item = {"price": price_id, "quantity": 1}
        else:
            item = {"price_data": {"currency": PRICES[prod][1].lower(), "unit_amount": price_cents(prod), "tax_behavior": "inclusive",
                                   "product_data": {"name": mail_templates.NAMES[prod]["en"], "metadata": {"product": prod}}},
                    "quantity": 1}
        # manual invoicing (3.3): Stripe asks for the full billing address (name included) and - optionally, for businesses -
        # the tax ID; a Customer object keeps them in the Stripe dashboard as well
        params = {"mode": "payment", "line_items": [item], "locale": lang, "submit_type": "pay",
                  "billing_address_collection": "required", "tax_id_collection": {"enabled": True}, "customer_creation": "always",
                  "success_url": self._site_page(self._return_base(origin), lang, "thanks") + "?session_id={CHECKOUT_SESSION_ID}",
                  "cancel_url": self._site_page(self._return_base(origin), lang, "shop"),
                  "metadata": meta, "payment_intent_data": {"metadata": meta}}
        if os.getenv("STRIPE_AUTOMATIC_TAX", "0") == "1":       # only after Stripe Tax is set up in the dashboard
            params["automatic_tax"] = {"enabled": True}
        if os.getenv("STRIPE_INVOICE", "0") == "1":             # Stripe-hosted invoice PDF (extra Stripe fee)
            params["invoice_creation"] = {"enabled": True}
        if os.getenv("STRIPE_REQUIRE_TERMS", "0") == "1":       # needs the terms URL set in Stripe → Settings → Public details
            params["consent_collection"] = {"terms_of_service": "required"}
        try:
            s = self.stripe.create_checkout_session(params)
        except stripe_pay.StripeError as exc:
            logger.error("Stripe checkout session failed: status=%s code=%s", exc.status, exc.code)
            raise LicenseError("payment_unavailable", 503, "The payment page could not be opened - please try again later") from None
        if not s.get("url"):
            raise LicenseError("payment_unavailable", 503, "The payment page could not be opened - please try again later")
        day = time.strftime("%Y%m%d", time.gmtime(self.clock()))
        self.store.incr(f"shop:checkouts:{day}:{prod}:{self.mode}", 400 * 86_400)
        return {"url": s["url"], "id": s.get("id", "")}

    def handle_stripe_webhook(self, raw: bytes, sig_header: str):
        if not stripe_pay.verify_signature(raw, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET", "").strip(), now=self.clock()):
            self.store.incr("shop:rejected_sig", 400 * 86_400)
            raise LicenseError("bad_signature", 401, "Invalid webhook signature")
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise LicenseError("bad_payload", 400, "Invalid JSON") from None
        ev_id, etype, live = str(ev.get("id", "")), str(ev.get("type", "")), bool(ev.get("livemode"))
        obj = (ev.get("data") or {}).get("object") or {}
        if not ev_id or not isinstance(obj, dict):
            return {"ok": True, "ignored": "malformed event"}
        if self.mode == "test" and live:
            logger.warning("LIVE Stripe event ignored because SHOP_MODE=test event=%s", ev_id)
            return {"ok": True, "ignored": "live event while SHOP_MODE=test"}
        if self.mode == "live" and not live and os.getenv("SHOP_ACCEPT_TEST_ORDERS", "0") != "1":
            logger.warning("TEST Stripe event ignored because SHOP_MODE=live event=%s", ev_id)
            return {"ok": True, "ignored": "test event while SHOP_MODE=live"}
        done_key = "shop:stripe_evt:" + ev_id
        if self.store.get(done_key):
            return {"ok": True, "duplicate": True}
        if etype in STRIPE_PAID_EVENTS:
            result = self._stripe_session_paid(str(obj.get("id", "")), live)      # raises 503/409 → not marked done, Stripe retries
        elif etype == "charge.refunded":
            result = self._stripe_refunded(obj, live)
        elif etype == "charge.refund.updated":
            result = self._stripe_refund_updated(obj, live)
        elif etype in ("charge.dispute.created", "charge.dispute.closed"):
            result = self._stripe_dispute(obj, live, etype)
        elif etype == "checkout.session.async_payment_failed":
            logger.warning("Stripe delayed payment failed for session %s - no licence", obj.get("id", ""))
            result = {"ok": True, "ignored": "payment failed"}
        else:
            result = {"ok": True, "ignored": etype}
        self.store.set(done_key, int(self.clock()), 400 * 86_400)
        return result

    def _stripe_reject(self, sid, why):
        logger.error("Stripe session %s NOT fulfilled: %s", sid, why)
        self.store.incr("shop:stripe_rejected", 800 * 86_400)
        tag = "[TESZT] " if sid.startswith("cs_test_") else ""
        self._owner_mail(f"owner-rejected:{sid}", f"{tag}Fizetés LICENC NÉLKÜL – kézi ellenőrzés kell: {sid[-12:]}",
                         [f"{tag}A Stripe-munkamenetet a szerver nem teljesítette automatikusan: {why}.", f"Munkamenet: {sid}", "",
                          "Teendő: nézd meg a fizetést a Stripe Dashboardon. Ha valódi, kifizetett vásárlás: adj ki licencet az adminban "
                          "(Licencek → Új licenc) és küldd el a vásárlónak, vagy térítsd vissza a Stripe-ban."])
        return {"ok": True, "ignored": why}

    def _stripe_session_paid(self, sid, live):
        if not sid.startswith("cs_"):
            return {"ok": True, "ignored": "not a checkout session"}
        lock = "shop:stripe_lock:" + sid
        if self.store.incr(lock, 120) != 1:            # the same session is being processed right now (parallel delivery)
            raise LicenseError("busy", 409, "Session is being processed")
        try:
            # never trust the event body alone: re-read the session with the secret key
            try:
                s = self.stripe.get_checkout_session(sid)
            except stripe_pay.StripeError as exc:
                logger.error("Stripe session %s could not be verified (status=%s code=%s) - Stripe will retry", sid, exc.status, exc.code)
                raise LicenseError("verification_unavailable", 503, "Payment could not be verified yet") from None
            if s.get("id") != sid:
                return self._stripe_reject(sid, "session id mismatch")
            if s.get("payment_status") != "paid":
                return {"ok": True, "ignored": "not paid (" + str(s.get("payment_status")) + ")"}   # async methods: a later event
            if s.get("mode") != "payment":
                return self._stripe_reject(sid, "not a one-time payment")
            if bool(s.get("livemode")) != live:
                return self._stripe_reject(sid, "livemode mismatch")
            meta = s.get("metadata") or {}
            prod = meta.get("product")
            if prod not in PRICES or meta.get("shop") != "ebd-website":
                return self._stripe_reject(sid, "unknown product")
            # Adaptive Pricing: the buyer may pay in a local currency (e.g. HUF). The shop price and the bookkeeping amounts are
            # then in currency_conversion (source currency = EUR); the session amounts are the converted presentment amounts.
            cc = s.get("currency_conversion") if isinstance(s.get("currency_conversion"), dict) else None
            paid_currency, paid_total = str(s.get("currency", "") or "").upper(), _cents(s.get("amount_total"))
            src = cc or s
            currency = str((cc or {}).get("source_currency") or paid_currency).upper()
            if prod not in stripe_price_map():          # inline price: the amount must be exactly the shop price
                if currency != PRICES[prod][1] or int(src.get("amount_subtotal") or 0) != price_cents(prod):
                    return self._stripe_reject(sid, f"amount mismatch {src.get('amount_subtotal')} {currency}")
            cd = s.get("customer_details") or {}
            email = (cd.get("email") or s.get("customer_email") or "").strip()
            if not EMAIL_RE.fullmatch(email):
                return self._stripe_reject(sid, "no valid buyer e-mail")
            td = s.get("total_details") or {}
            total = _cents(src.get("amount_total"))
            scale = (lambda v: round(_cents(v) * total / paid_total)) if cc and paid_total else _cents   # presentment → source currency
            money = {"total": total, "subtotal": _cents(src.get("amount_subtotal")), "tax": scale(td.get("amount_tax")),
                     "discount": scale(td.get("amount_discount")), "currency": currency[:3], "tax_name": "", "tax_rate": 0.0,
                     "tax_inclusive": True, "total_usd": 0, "tax_usd": 0}
            if cc:
                money.update(paid_currency=paid_currency[:3], paid_total=paid_total)
            pi = s.get("payment_intent")
            pi = pi.get("id") if isinstance(pi, dict) else pi
            if pi:
                self.store.set("shop:stripe_pi:" + str(pi), sid, 800 * 86_400)
            if pi and not cc:
                # Adaptive Pricing variant seen on the account (staging test 2026-11-18): the session stays in EUR and the buyer's
                # local amount (e.g. 5 747,38 Ft) is only on the charge (presentment_details) - needed for the manual invoice.
                # Best effort: a failure here never blocks the licence.
                try:
                    ch = (self.stripe.request("GET", "/payment_intents/" + str(pi), {"expand": ["latest_charge"]}).get("latest_charge") or {})
                    pd = ch.get("presentment_details") if isinstance(ch, dict) else None
                    if isinstance(pd, dict) and str(pd.get("presentment_currency", "")).upper() not in ("", currency):
                        money.update(paid_currency=str(pd["presentment_currency"]).upper()[:3], paid_total=_cents(pd.get("presentment_amount")))
                except stripe_pay.StripeError:
                    pass
            buyer = {"name": (cd.get("name") or "")[:200], "business_name": (cd.get("business_name") or "")[:200],
                     "address": cd.get("address") if isinstance(cd.get("address"), dict) else {},
                     "tax_ids": cd.get("tax_ids") if isinstance(cd.get("tax_ids"), list) else [], "payment_intent": pi or ""}
            return self._fulfil(sid, [prod], email, (cd.get("name") or "")[:120], money, not live,
                                mail_templates.lang_of(meta.get("lang")), "stripe", buyer)
        finally:
            self.store.set(lock, 0, 5)

    # ------------------------------------------------------------------ safety nets: nothing paid is ever left without a licence
    SESSION_RE = re.compile(r"cs_(test|live)_[A-Za-z0-9]{10,250}")

    def stripe_confirm(self, sid):
        """Thank-you page (Stripe success_url ?session_id=…): wakes a sleeping server and fulfils the paid session at once,
        independent of the webhook. Idempotent; answers only a status (no personal data)."""
        sid = (sid or "").strip()
        if not self.SESSION_RE.fullmatch(sid):
            raise LicenseError("bad_session", 422, "Invalid session")
        live = self.mode == "live"
        if not self.stripe.configured or sid.startswith("cs_live_") != live:
            return {"status": "pending"}
        try:
            r = self._stripe_session_paid(sid, live)
        except LicenseError as exc:
            if exc.code in ("busy", "verification_unavailable"):
                return {"status": "pending"}                 # the webhook / the next reconcile completes it
            raise
        ign = str(r.get("ignored", ""))
        return {"status": "pending" if ign.startswith("not paid") else ("check" if ign else "ok")}

    def reconcile_stripe(self, force=False, days=7):
        """Re-checks the paid Checkout Sessions of the last `days` days at Stripe and completes every one that has no licence
        or whose licence e-mail was never attempted (webhook lost, delivered while asleep and given up, wrong secret …).
        Runs at most every 15 minutes (startup, webhook, checkout, thank-you page, admin); `force` from the admin."""
        if not self.stripe.configured or self.stripe.mode != self.mode:
            return {"skipped": "stripe not configured for this shop mode"}
        if self.store.incr("shop:reconcile:lock", 900) != 1 and not force:     # every run (also a forced one) starts the 15 min window
            return {"skipped": "ran recently"}
        live, now = self.mode == "live", int(self.clock())
        params = {"limit": 100, "status": "complete", "created": {"gte": now - days * 86_400}}
        checked, fixed, errors = 0, [], 0
        for _ in range(10):
            try:
                page = self.stripe.request("GET", "/checkout/sessions", params)
            except stripe_pay.StripeError as exc:
                logger.error("reconcile: Stripe list failed status=%s code=%s", exc.status, exc.code)
                errors += 1
                break
            data = page.get("data") or []
            for x in data:
                meta = x.get("metadata") or {}
                if meta.get("shop") != "ebd-website" or x.get("payment_status") != "paid" or bool(x.get("livemode")) != live:
                    continue
                checked += 1
                sid, prod = str(x.get("id", "")), meta.get("product")
                lid = self.store.get(f"lic:order:{sid}:{prod}")
                if (lid and self.store.get(f"mail:once:purchase:{lid}")) or self.store.get(f"mail:once:owner-rejected:{sid}"):
                    continue                                   # complete already / already reported to the owner
                try:
                    r = self._stripe_session_paid(sid, live)
                except LicenseError:
                    errors += 1
                    continue
                if r.get("licences") or r.get("recovered"):
                    fixed.append(sid)
                    tag = "" if live else "[TESZT] "
                    self._owner_mail(f"owner-reconciled:{sid}", f"{tag}Pótolt licenc (a webhook nem érkezett meg): {sid[-12:]}",
                                     [f"{tag}Egy kifizetett vásárlás licencét / licenclevelét az automatikus egyeztetés pótolta.",
                                      f"Munkamenet: {sid}", "Licenc: " + ", ".join(r.get("licences") or r.get("recovered") or []), "",
                                      "Nincs teendő; érdemes a Stripe Dashboardon (Developers → Webhooks) megnézni, miért nem ment át a webhook."])
            if not page.get("has_more") or not data:
                break
            params["starting_after"] = data[-1]["id"]
        result = {"t": now, "checked": checked, "fixed": len(fixed), "errors": errors}
        self.store.set("shop:reconcile:last", result, 30 * 86_400)
        if fixed:
            logger.warning("reconcile: completed %d paid session(s) without webhook: %s", len(fixed), fixed)
        return result

    def _stripe_refunded(self, charge, live):
        pi = charge.get("payment_intent")
        pi = pi.get("id") if isinstance(pi, dict) else pi
        sid = self.store.get("shop:stripe_pi:" + str(pi)) if pi else None
        if not sid:
            logger.warning("Stripe refund for unknown payment %s - nothing to revoke", pi)
            return {"ok": True, "ignored": "unknown payment"}
        amount, refunded = _cents(charge.get("amount")), _cents(charge.get("amount_refunded"))
        full = bool(charge.get("refunded")) or (amount and refunded >= amount)
        email = ((charge.get("billing_details") or {}).get("email") or charge.get("receipt_email") or "").strip()
        if not email:
            try:
                email = ((self.stripe.get_checkout_session(sid).get("customer_details") or {}).get("email") or "").strip()
            except stripe_pay.StripeError:
                email = ""                 # the licence is still revoked; the notice is then not sent (logged)
        cur = str(charge.get("currency", "") or "").upper()
        # bookkeeping in the order currency: a charge paid in a converted currency (Adaptive Pricing, e.g. HUF) is scaled back
        mine = [o for o in (self.store.get("shop:orders") or []) if isinstance(o, dict) and o.get("order") == sid]
        o_cur, o_total = (mine[0].get("currency") or cur, _cents(mine[0].get("total"))) if mine else (cur, amount)
        conv = (lambda v: round(v * o_total / amount)) if (o_cur != cur and amount and o_total) else (lambda v: v)
        attrs = {"refunded_amount": conv(refunded), "status": "refunded" if full else "partial_refund", "total": conv(amount),
                 "currency": o_cur, "user_email": email}
        result = self._order_refunded(sid, attrs, not live)
        # owner: a refund needs a corrective (storno) invoice for the manually issued invoice
        tag = "" if live else "[TESZT] "
        kind = "TELJES visszatérítés" if full else "Részleges visszatérítés"
        lines = [f"{tag}{kind} történt a Stripe-ban.", "",
                 f"Visszatérített összeg (összesen): {self._amount(refunded, cur)} / {self._amount(amount, cur)}",
                 f"Munkamenet: {sid} · fizetés: {pi}", f"Vásárló e-mail: {email or '-'}",
                 ("Licenc visszavonva: " + (", ".join(result.get("revoked") or []) or "-")) if full
                 else "A licenc aktív marad (részleges visszatérítés).", "",
                 "Teendő: helyesbítő (sztornó) számla a kiállított számlához." if live else "Tesztfizetés – nincs számlateendő."]
        self._owner_mail(f"owner-refund:{sid}:{refunded}", f"{tag}{kind}: {self._amount(refunded, cur)} – {sid[-12:]}", lines)
        return result

    def _session_of_pi(self, pi):
        pi = pi.get("id") if isinstance(pi, dict) else pi
        return (self.store.get("shop:stripe_pi:" + str(pi)) if pi else None), pi

    def _stripe_refund_updated(self, refund, live):
        """A refund that FAILED / was CANCELED: the money stayed with us, but the licence was already revoked → the owner
        decides (reinstate in the admin, or refund again). Nothing is changed automatically."""
        status = str(refund.get("status", ""))
        if status not in ("failed", "canceled"):
            return {"ok": True, "ignored": "refund " + status}
        sid, pi = self._session_of_pi(refund.get("payment_intent"))
        cur = str(refund.get("currency", "") or "").upper()
        tag = "" if live else "[TESZT] "
        self._owner_mail(f"owner-refund-{status}:{refund.get('id', '')}", f"{tag}SIKERTELEN visszatérítés ({status}) – {self._amount(_cents(refund.get('amount')), cur)}",
                         [f"{tag}A Stripe-visszatérítés állapota: {status} (ok: {refund.get('failure_reason') or '-'}).",
                          f"Összeg: {self._amount(_cents(refund.get('amount')), cur)} · munkamenet: {sid or '-'} · fizetés: {pi or '-'}", "",
                          "A licencet a visszatérítéskor visszavontuk. Teendő: a Stripe-ban ismételd meg a visszatérítést, vagy állítsd vissza a licencet az adminban."])
        return {"ok": True, "refund_failed": True}

    def _stripe_dispute(self, dispute, live, etype):
        """Chargeback: opened → owner notice (the licence stays until the outcome); closed as LOST → like a full refund."""
        sid, pi = self._session_of_pi(dispute.get("payment_intent"))
        cur, amount = str(dispute.get("currency", "") or "").upper(), _cents(dispute.get("amount"))
        status, tag = str(dispute.get("status", "")), ("" if live else "[TESZT] ")
        result = {"ok": True, "dispute": status}
        if etype == "charge.dispute.closed" and status == "lost" and sid:
            result = self._order_refunded(sid, {"refunded_amount": amount, "status": "refunded", "total": amount, "currency": cur,
                                                "user_email": ""}, not live)   # no buyer notice: the bank handled it
        if etype == "charge.dispute.created" or status in ("lost", "won"):
            self._owner_mail(f"owner-{etype}:{dispute.get('id', '')}", f"{tag}Chargeback ({status}) – {self._amount(amount, cur)}",
                             [f"{tag}Vitatott terhelés (chargeback): {etype}, állapot: {status}, ok: {dispute.get('reason') or '-'}.",
                              f"Összeg: {self._amount(amount, cur)} · munkamenet: {sid or '-'} · fizetés: {pi or '-'}",
                              ("Licenc visszavonva: " + (", ".join(result.get("revoked") or []) or "-") + " · teendő: sztornó számla") if status == "lost"
                              else "A vitát megnyerted, a licenc aktív marad." if status == "won"
                              else "Teendő: a Stripe-ban válaszolj a vitára a határidőn belül (a licenc addig aktív)."])
        return result

    # ------------------------------------------------------------------ e-mail: once per (kind, licence) + safe queue
    def _render_mail(self, kind, lid, prod, lang, order_id, test_mode, key=None, name=""):
        if kind == "purchase":
            rec = self.licenses.get(lid)
            key = key or self.licenses.recover_key(rec)
            if not key:
                return None
            return mail_templates.purchase(prod, lang, name, key, self.download_link(lid, prod), test_mode)
        return mail_templates.refund(prod, lang, order_id, self.licenses.get(lid).get("key_hint", ""), test_mode)

    def _send_once(self, kind, lid, prod, lang, email, order_id, test_mode, key=None, name=""):
        """At most ONE automatic send per (kind, licence) - a webhook retry or a race can never mail the licence twice.
        sent -> done · uncertain -> recorded for the owner, NO automatic retry · definite failure -> encrypted queue."""
        if not email:
            return "no_email"
        if self.store.incr(f"mail:once:{kind}:{lid}", 800 * 86_400) != 1:
            logger.warning("%s e-mail for %s already attempted - not sent again", kind, lid)
            return "duplicate_blocked"
        msg = self._render_mail(kind, lid, prod, lang, order_id, test_mode, key=key, name=name)
        if not msg:
            return "no_key"
        subject, text, html = msg
        r = self.mailer.send(email, subject, text, kind=kind, html=html, idempotency_key=f"ebd-{kind}-{lid}")
        if r.get("sent"):
            return "sent"
        item = {"id": f"{kind}-{lid}", "kind": kind, "lid": lid, "product": prod, "lang": lang, "order": str(order_id), "test": bool(test_mode),
                "to_enc": encrypt_key(email, "mailq:" + lid), "to": r.get("to", ""), "t": int(self.clock()), "attempts": 1,
                "error": r.get("error", "")}
        if r.get("uncertain"):
            self._mail_list_put("mail:uncertain", item)
            return "uncertain"
        if r.get("error") == "not_configured":
            item["error"] = "not_configured"
        self._mail_list_put("mail:queue", item)
        return "queued"

    def _mail_list_put(self, name, item):
        lst = self.store.get(name) or []
        lst = [x for x in (lst if isinstance(lst, list) else []) if x.get("id") != item["id"]][-199:] + [item]
        self.store.set(name, lst, 30 * 86_400)

    def mail_lists(self):
        out = {}
        for name in ("mail:queue", "mail:uncertain"):
            lst = self.store.get(name) or []
            out[name.split(":")[1]] = [{k: x.get(k) for k in ("id", "kind", "lid", "product", "lang", "order", "test", "to", "t", "attempts", "error")}
                                       for x in (lst if isinstance(lst, list) else [])]
        return out

    def process_mail_queue(self, limit=20):
        """Re-sends DEFINITELY failed mails (quota reached, provider down/refused). Never touches 'uncertain' items."""
        lst = self.store.get("mail:queue") or []
        lst = lst if isinstance(lst, list) else []
        if not self.mailer.configured:
            return {"processed": 0, "sent": 0, "left": len(lst)}
        self._retry_owner_mails()
        if not lst:
            return {"processed": 0, "sent": 0, "left": 0}
        keep, sent, done = [], 0, 0
        for item in lst:
            if item.get("attempts", 0) >= 30 or self.clock() - item.get("t", 0) > 14 * 86_400:
                if item.get("kind") == "purchase" and not item.get("gave_up"):
                    item["gave_up"] = True
                    self._alert_delivery(item.get("order", ""), item["lid"], item.get("product", ""), item.get("to", ""), "gave_up", item.get("test"))
                keep.append(item); continue
            if done >= limit:
                keep.append(item); continue
            done += 1
            email = decrypt_key(item.get("to_enc", ""), "mailq:" + item["lid"])
            msg = self._render_mail(item["kind"], item["lid"], item["product"], item["lang"], item["order"], item.get("test"))
            if not email or not msg:
                item["error"] = "cannot_render"; keep.append(item); continue
            subject, text, html = msg
            r = self.mailer.send(email, subject, text, kind=item["kind"] + "-queued", html=html, idempotency_key=f"ebd-{item['kind']}-{item['lid']}")
            if r.get("sent"):
                sent += 1
                if item["kind"] == "purchase":
                    self._mark_order_mail(item["lid"], "sent")
                    self._owner_mail(f"owner-delivered:{item['lid']}", ("[TESZT] " if item.get("test") else "") + f"Licenclevél utólag kézbesítve: {item['lid']}",
                                     [f"A korábban sikertelen licenclevél most kiment ({item['lid']}, rendelés {item.get('order', '')}). Nincs teendő."])
            elif r.get("uncertain"):
                item.update(error=r.get("error", ""), attempts=item.get("attempts", 0) + 1)
                self._mail_list_put("mail:uncertain", item)
            else:
                item.update(error=r.get("error", ""), attempts=item.get("attempts", 0) + 1)
                keep.append(item)
        self.store.set("mail:queue", keep, 30 * 86_400)
        return {"processed": done, "sent": sent, "left": len(keep)}

    def _mark_order_mail(self, lid, state):
        orders = self.store.get("shop:orders") or []
        if isinstance(orders, list):
            for o in orders:
                if o.get("lid") == lid:
                    o["mail_state"], o["mail_sent"] = state, state == "sent"
            self.store.set("shop:orders", orders, 800 * 86_400)

    def requeue_uncertain(self, item_id):
        """Owner decision after checking the provider dashboard / the buyer: the uncertain mail did NOT arrive -> send again."""
        unc = self.store.get("mail:uncertain") or []
        unc = unc if isinstance(unc, list) else []
        item = next((x for x in unc if x.get("id") == item_id), None)
        if not item:
            raise LicenseError("not_found", 404, "No such uncertain mail")
        self.store.set("mail:uncertain", [x for x in unc if x.get("id") != item_id], 30 * 86_400)
        item["error"] = "requeued by owner"
        self._mail_list_put("mail:queue", item)
        return self.process_mail_queue(limit=5)

    def _order_refunded(self, order_id, attrs, test_mode=False):
        orders = self.store.get("shop:orders") or []
        orders = orders if isinstance(orders, list) else []
        month = time.strftime("%Y%m", time.gmtime(self.clock()))
        # refunded_amount is cumulative in Lemon Squeezy: count only the part not yet recorded for this order
        refunded = _cents(attrs.get("refunded_amount"))
        mine = [o for o in orders if o.get("order") == order_id]
        # LS test 2026-11-14: a full refund of a 19.99 EUR order reported refunded_amount=2000 (formatted "€19.99", the
        # USD round trip rounds). A full refund is the order total; a refund never exceeds the order total.
        total = _cents(mine[0].get("total")) if mine else _cents(attrs.get("total"))
        if total and (attrs.get("status") == "refunded" or refunded > total):
            refunded = total
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
            state = self._send_once("refund", lid, prod, lang, email, order_id, mode == "test")
            if state != "sent":
                logger.error("order %s refunded, licence %s revoked, notice e-mail state=%s", order_id, lid, state)
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
        orders = [o for o in (orders if isinstance(orders, list) else [])
                  if not (o.get("order") == entry.get("order") and o.get("product") == entry.get("product"))][-499:] + [entry]
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
        for name in ("mail:queue", "mail:uncertain"):          # test mails waiting in the queue / uncertain list
            lst = self.store.get(name) or []
            if isinstance(lst, list):
                self.store.set(name, [x for x in lst if not x.get("test")], 30 * 86_400)
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
