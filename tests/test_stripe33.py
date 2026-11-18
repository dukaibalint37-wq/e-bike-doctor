"""3.3: Stripe Checkout - one-time payments, Stripe-Signature verification, licence ONLY after the session is re-read from
the Stripe API as paid, no duplicate licence / e-mail, download link in the buyer's language, refunds."""
import hashlib, hmac, json, os, re, time, unittest, urllib.parse

from tests_path import *  # noqa
import shop as S
import stripe_pay as SP
from test_shop31 import Base

WHSEC = "whsec_test_secret"


class FakeStripe(SP.StripeClient):
    """Real client, HTTP replaced: sessions[id] is what the Stripe API answers for GET /checkout/sessions/<id>."""
    def __init__(self, key="sk_test_123"):
        super().__init__(secret_key=key)
        self.calls, self.sessions, self.fail = [], {}, None

    def _http(self, method, url, body, headers):
        self.calls.append({"method": method, "url": url, "body": body.decode() if body else "", "headers": headers})
        if self.fail:
            raise self.fail
        if method == "POST" and url.endswith("/checkout/sessions"):
            return 200, json.dumps({"id": "cs_test_new", "url": "https://checkout.stripe.com/c/pay/cs_test_new"})
        if method == "GET" and "/payment_intents/" in url:
            pid = url.split("/payment_intents/", 1)[1].split("?", 1)[0]
            if pid in getattr(self, "pis", {}):
                return 200, json.dumps(self.pis[pid])
            return 404, json.dumps({"error": {"type": "invalid_request_error", "code": "resource_missing"}})
        if method == "GET" and "/checkout/sessions?" in url:          # list (reconcile)
            return 200, json.dumps({"data": [s for s in self.sessions.values() if s.get("status") == "complete"], "has_more": False})
        sid = url.rsplit("/", 1)[-1]
        if sid in self.sessions:
            return 200, json.dumps(self.sessions[sid])
        return 404, json.dumps({"error": {"type": "invalid_request_error", "code": "resource_missing"}})


def session(sid="cs_test_1", product="EBD-WIN", lang="hu", paid=True, live=False, amount=1999, currency="eur", email="vevo@example.com",
            mode="payment", pi="pi_1"):
    return {"id": sid, "object": "checkout.session", "mode": mode, "status": "complete" if paid else "open",
            "payment_status": "paid" if paid else "unpaid", "livemode": live, "amount_subtotal": amount, "amount_total": amount,
            "currency": currency, "payment_intent": pi,
            "customer_details": {"email": email, "name": "Vevő Béla", "address": {"line1": "Fő utca 1.", "line2": None, "postal_code": "1011",
                                                                               "city": "Budapest", "state": None, "country": "HU"},
                                 "tax_ids": [{"type": "eu_vat", "value": "HU12345678"}]},
            "total_details": {"amount_tax": 0, "amount_discount": 0}, "metadata": {"product": product, "lang": lang, "shop": "ebd-website"}}


def event(obj, etype="checkout.session.completed", ev_id="evt_1", live=False):
    return json.dumps({"id": ev_id, "object": "event", "type": etype, "livemode": live, "data": {"object": obj}}).encode()


def sig(raw, t, secret=WHSEC):
    return f"t={int(t)},v1=" + hmac.new(secret.encode(), str(int(t)).encode() + b"." + raw, hashlib.sha256).hexdigest()


class StripeBase(Base):
    def setUp(self):
        super().setUp()
        self._s_saved = {k: os.environ.get(k) for k in ("STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_MAP", "STRIPE_SECRET_KEY", "SITE_URL",
                                                         "STRIPE_PUBLISHABLE_KEY", "SHOP_ACCEPT_TEST_ORDERS", "OWNER_NOTIFY_EMAIL", "MAIL_REPLY_TO")}
        for k in self._s_saved:
            os.environ.pop(k, None)
        os.environ["STRIPE_WEBHOOK_SECRET"] = WHSEC
        os.environ["OWNER_NOTIFY_EMAIL"] = "owner@example.com"
        self.stripe = FakeStripe()
        self.shop = S.Shop(self.store, self.lic, self.mail, clock=self.clock, stripe=self.stripe)

    def tearDown(self):
        for k, v in self._s_saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def deliver(self, obj, etype="checkout.session.completed", ev_id="evt_1", live=False):
        raw = event(obj, etype, ev_id, live)
        return self.shop.handle_stripe_webhook(raw, sig(raw, self.clock()))

    def owner_mails(self):
        return [c["payload"] for c in self.mail.calls if c["payload"].get("to") == ["owner@example.com"]]

    def licence_mails(self):
        return [c for c in self.mail.calls if "EBD" in c["payload"].get("text", "") and "?t=" in c["payload"].get("text", "")]


class SignatureTest(unittest.TestCase):
    def test_valid_tampered_stale_and_missing(self):
        raw, now = b'{"id":"evt"}', 1_800_000_000
        good = sig(raw, now)
        self.assertTrue(SP.verify_signature(raw, good, WHSEC, now=now))
        self.assertTrue(SP.verify_signature(raw, good + ",v1=deadbeef", WHSEC, now=now))       # several v1 (secret rotation)
        self.assertTrue(SP.verify_signature(raw, "v1=deadbeef," + good, WHSEC, now=now))
        self.assertFalse(SP.verify_signature(raw + b" ", good, WHSEC, now=now))               # body changed
        self.assertFalse(SP.verify_signature(raw, good, "whsec_other", now=now))              # other secret
        self.assertFalse(SP.verify_signature(raw, good, WHSEC, now=now + 301))                # replay outside the tolerance
        self.assertFalse(SP.verify_signature(raw, good.replace("v1=", "v0="), WHSEC, now=now))
        for h in ("", "t=abc,v1=00", "garbage"):
            self.assertFalse(SP.verify_signature(raw, h, WHSEC, now=now))
        self.assertFalse(SP.verify_signature(raw, good, "", now=now))                         # no secret configured → reject

    def test_form_encoding_and_key_modes(self):
        q = urllib.parse.parse_qs(SP.form_encode({"mode": "payment", "line_items": [{"price_data": {"unit_amount": 1999}, "quantity": 1}],
                                                  "x": True, "skip": None}))
        self.assertEqual(q, {"mode": ["payment"], "line_items[0][price_data][unit_amount]": ["1999"], "line_items[0][quantity]": ["1"], "x": ["true"]})
        self.assertEqual([SP.key_mode(k) for k in ("sk_test_a", "sk_live_a", "rk_live_a", "pk_test_a", "", "x")], ["test", "live", "live", "test", "", ""])


class CheckoutTest(StripeBase):
    def test_one_time_payment_session_with_shop_prices_language_and_return_urls(self):
        r = self.shop.stripe_checkout("windows", "de", True)
        self.assertEqual(r["url"], "https://checkout.stripe.com/c/pay/cs_test_new")
        call = self.stripe.calls[-1]
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk_test_123")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(call["body"]).items()}
        self.assertEqual(q["mode"], "payment")                                    # one-time, never a subscription
        self.assertNotIn("subscription_data", " ".join(q))
        self.assertEqual(q["line_items[0][price_data][unit_amount]"], "1999")
        self.assertEqual(q["line_items[0][price_data][currency]"], "eur")
        self.assertEqual(q["line_items[0][quantity]"], "1")
        self.assertNotIn("line_items[0][price_data][recurring][interval]", q)
        self.assertEqual((q["metadata[product]"], q["metadata[lang]"], q["locale"]), ("EBD-WIN", "de", "de"))
        self.assertEqual(q["payment_intent_data[metadata][product]"], "EBD-WIN")
        self.assertTrue(q["metadata[withdrawal_waiver]"].startswith("accepted 20"))
        self.assertEqual(q["success_url"], "https://ebikedoctorpro.com/de/danke/?session_id={CHECKOUT_SESSION_ID}")
        # manual invoicing: full billing address required, tax ID offered, Customer kept in Stripe
        self.assertEqual((q["billing_address_collection"], q["tax_id_collection[enabled]"], q["customer_creation"]), ("required", "true", "always"))
        self.assertEqual(q["cancel_url"], "https://ebikedoctorpro.com/de/shop/")
        self.shop.stripe_checkout("android", "hu", True)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertEqual((q["line_items[0][price_data][unit_amount]"], q["metadata[product]"]), ("1499", "EBD-AND"))
        self.assertEqual(q["success_url"], "https://ebikedoctorpro.com/hu/koszonjuk/?session_id={CHECKOUT_SESSION_ID}")

    def test_return_urls_follow_an_allowed_origin_only(self):
        saved = os.environ.get("CORS_ORIGINS")
        os.environ["CORS_ORIGINS"] = "https://ebikedoctorpro.com,http://localhost:8099"
        try:
            def urls(origin):
                self.shop.stripe_checkout("windows", "hu", True, origin)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
                return q["success_url"], q["cancel_url"]
            self.assertEqual(urls("http://localhost:8099"), ("http://localhost:8099/hu/koszonjuk/?session_id={CHECKOUT_SESSION_ID}",
                                                             "http://localhost:8099/hu/webaruhaz/"))
            self.assertTrue(urls("https://evil.example")[0].startswith("https://ebikedoctorpro.com/hu/koszonjuk/"))   # not allowed
            self.assertTrue(urls("")[0].startswith("https://ebikedoctorpro.com/"))
            os.environ["SHOP_MODE"] = "live"; self.shop.stripe = FakeStripe("sk_live_1"); self.stripe = self.shop.stripe
            self.assertTrue(urls("http://localhost:8099")[0].startswith("https://ebikedoctorpro.com/"))         # live: https only
        finally:
            os.environ.pop("CORS_ORIGINS") if saved is None else os.environ.__setitem__("CORS_ORIGINS", saved)

    def test_dashboard_price_ids_are_used_when_configured(self):
        os.environ["STRIPE_PRICE_MAP"] = '{"EBD-AND":"price_and","EBD-WIN":"bogus"}'
        self.shop.stripe_checkout("android", "en", True)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertEqual(q["line_items[0][price]"], "price_and"); self.assertNotIn("line_items[0][price_data][unit_amount]", q)
        self.shop.stripe_checkout("windows", "en", True)                                # invalid id ignored → shop price
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertEqual(q["line_items[0][price_data][unit_amount]"], "1999")

    def test_refused_without_key_with_wrong_key_mode_and_unknown_product(self):
        with self.assertRaises(S.LicenseError) as cm:
            self.shop.stripe_checkout("linux", "en", True)
        self.assertEqual((cm.exception.code, cm.exception.status), ("bad_product", 422))
        for key in ("", "sk_live_123"):                                          # SHOP_MODE=test: a live key never charges
            self.shop.stripe = FakeStripe(key)
            with self.assertRaises(S.LicenseError) as cm:
                self.shop.stripe_checkout("windows", "en", True)
            self.assertEqual(cm.exception.code, "not_configured")
            self.assertEqual(self.shop.stripe.calls, [])
        os.environ["SHOP_MODE"] = "live"; self.shop.stripe = FakeStripe("sk_test_123")
        with self.assertRaises(S.LicenseError):
            self.shop.stripe_checkout("windows", "en", True)

    def test_no_checkout_without_consent_to_immediate_delivery(self):
        for c in (False, None, "yes", 1):
            with self.assertRaises(S.LicenseError) as cm:
                self.shop.stripe_checkout("windows", "en", c)
            self.assertEqual(cm.exception.code, "consent_required")
        self.assertEqual(self.stripe.calls, [])

    def test_stripe_outage_is_a_polite_503(self):
        self.stripe.fail = OSError("down")
        with self.assertRaises(S.LicenseError) as cm:
            self.shop.stripe_checkout("windows", "en", True)
        self.assertEqual(cm.exception.code, "payment_unavailable")

    def test_status_never_contains_keys(self):
        os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_abc"
        st = self.shop.stripe_status()
        self.assertEqual((st["key_mode"], st["publishable_mode"], st["checkout_ready"], st["webhook_secret"]), ("test", "test", True, True))
        self.assertNotIn("sk_test_123", json.dumps(st)); self.assertNotIn("pk_test_abc", json.dumps(st))


class WebhookTest(StripeBase):
    def test_verified_paid_session_issues_one_licence_and_mails_key_and_download_link_in_buyer_language(self):
        self.stripe.sessions["cs_test_1"] = session(lang="de")
        r = self.deliver({"id": "cs_test_1"})
        self.assertEqual(len(r["licences"]), 1)
        rec = self.lic.get(r["licences"][0])
        self.assertEqual((rec["product"], rec["source"], rec["order_id"], rec["test"]), ("EBD-WIN", "stripe", "cs_test_1", True))
        mails = self.licence_mails()
        self.assertEqual(len(mails), 1)
        p = mails[0]["payload"]
        self.assertEqual(p["to"], ["vevo@example.com"]); self.assertIn("Lizenzschlüssel und Download", p["subject"])
        self.assertIn("Stripe", p["text"]); self.assertNotIn("Lemon", p["text"])
        key = re.search(r"EBDW-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", p["text"]).group(0)
        self.assertEqual(self.lic.find_by_key(key)["lid"], rec["lid"])
        link = re.search(r"https://api\.example/v1/shop/download\?t=\S+", p["text"]).group(0)
        self.assertEqual(self.shop.resolve_download(link.split("t=", 1)[1]), "https://files.example/legacy.exe")   # the Windows installer
        o = self.shop.orders()[0]
        self.assertEqual((o["provider"], o["total"], o["currency"], o["lang"], o["mode"]), ("stripe", 1999, "EUR", "de", "test"))

    def test_android_purchase_gets_the_android_licence(self):
        self.stripe.sessions["cs_test_a"] = session(sid="cs_test_a", product="EBD-AND", amount=1499, lang="en")
        r = self.deliver({"id": "cs_test_a"})
        self.assertEqual(self.lic.get(r["licences"][0])["product"], "EBD-AND")
        self.assertIn("EBDA-", self.licence_mails()[0]["payload"]["text"])

    def test_no_duplicate_licence_or_mail_on_replays_and_second_events(self):
        self.stripe.sessions["cs_test_1"] = session()
        self.deliver({"id": "cs_test_1"})
        self.assertTrue(self.deliver({"id": "cs_test_1"})["duplicate"])                                  # same event again
        r = self.deliver({"id": "cs_test_1"}, etype="checkout.session.async_payment_succeeded", ev_id="evt_2")  # other event, same payment
        self.assertEqual(r["licences"], [])
        self.store.set("shop:stripe_evt:evt_1", None, 1)                                                 # even if the event memory is lost
        self.assertEqual(self.deliver({"id": "cs_test_1"})["licences"], [])
        self.assertEqual(len([l for l in self.lic.all() if l.get("order_id") == "cs_test_1"]), 1)
        self.assertEqual(len(self.licence_mails()), 1)
        self.assertEqual(len(self.shop.orders()), 1)

    def test_parallel_delivery_of_the_same_session_is_rejected_for_retry(self):
        self.stripe.sessions["cs_test_1"] = session()
        self.store.incr("shop:stripe_lock:cs_test_1", 120)                      # another worker is processing it
        with self.assertRaises(S.LicenseError) as cm:
            self.deliver({"id": "cs_test_1"})
        self.assertEqual(cm.exception.code, "busy")
        self.assertIsNone(self.store.get("shop:stripe_evt:evt_1"))              # not marked done → Stripe retries
        self.assertEqual(self.lic.all(), [])

    def test_event_body_is_not_trusted_the_api_answer_decides(self):
        self.stripe.sessions["cs_test_1"] = session(paid=False)                  # event claims paid, Stripe says unpaid
        r = self.deliver(dict(session(), id="cs_test_1"))
        self.assertIn("not paid", r["ignored"]); self.assertEqual(self.lic.all(), []); self.assertEqual(self.licence_mails(), [])
        # delayed payment method succeeds later → licence then
        self.stripe.sessions["cs_test_1"] = session()
        r = self.deliver({"id": "cs_test_1"}, etype="checkout.session.async_payment_succeeded", ev_id="evt_2")
        self.assertEqual(len(r["licences"]), 1)

    def test_rejections_issue_nothing(self):
        cases = {"cs_test_amount": session(sid="cs_test_amount", amount=100),          # manipulated amount
                 "cs_test_cur": session(sid="cs_test_cur", currency="usd"),
                 "cs_test_sub": session(sid="cs_test_sub", mode="subscription"),
                 "cs_test_prod": session(sid="cs_test_prod", product="EBD-XXX"),
                 "cs_test_live": session(sid="cs_test_live", live=True),
                 "cs_test_mail": session(sid="cs_test_mail", email="")}
        for i, (sid, s) in enumerate(cases.items()):
            self.stripe.sessions[sid] = s
            r = self.deliver({"id": sid}, ev_id=f"evt_r{i}")
            self.assertIn("ignored", r, sid)
        self.assertEqual(self.lic.all(), []); self.assertEqual(self.licence_mails(), [])
        self.assertEqual(int(self.store.get("shop:stripe_rejected")), len(cases))

    def test_unknown_session_and_stripe_outage_are_retried_not_dropped(self):
        with self.assertRaises(S.LicenseError) as cm:                           # 404 from Stripe (e.g. another account)
            self.deliver({"id": "cs_test_404"})
        self.assertEqual(cm.exception.code, "verification_unavailable")
        self.stripe.sessions["cs_test_1"] = session(); self.stripe.fail = TimeoutError("slow")
        with self.assertRaises(S.LicenseError):
            self.deliver({"id": "cs_test_1"}, ev_id="evt_9")
        self.assertIsNone(self.store.get("shop:stripe_evt:evt_9")); self.assertEqual(self.lic.all(), [])
        self.stripe.fail = None
        self.assertEqual(len(self.deliver({"id": "cs_test_1"}, ev_id="evt_9")["licences"]), 1)        # Stripe's retry succeeds

    def test_bad_signature_wrong_mode_and_other_events(self):
        self.stripe.sessions["cs_test_1"] = session()
        raw = event({"id": "cs_test_1"})
        for h in ("", "t=1,v1=00", sig(raw, self.clock(), "whsec_forged"), sig(raw, self.clock() - 3600)):
            with self.assertRaises(S.LicenseError) as cm:
                self.shop.handle_stripe_webhook(raw, h)
            self.assertEqual(cm.exception.code, "bad_signature")
        os.environ.pop("STRIPE_WEBHOOK_SECRET")
        with self.assertRaises(S.LicenseError):
            self.shop.handle_stripe_webhook(raw, sig(raw, self.clock()))
        os.environ["STRIPE_WEBHOOK_SECRET"] = WHSEC
        self.assertIn("live event", self.deliver({"id": "cs_test_1"}, live=True)["ignored"])        # SHOP_MODE=test
        os.environ["SHOP_MODE"] = "live"
        self.assertIn("test event", self.deliver({"id": "cs_test_1"}, ev_id="evt_t")["ignored"])     # SHOP_MODE=live
        os.environ["SHOP_MODE"] = "test"
        self.assertEqual(self.deliver({"id": "pi_1"}, etype="payment_intent.succeeded", ev_id="evt_o")["ignored"], "payment_intent.succeeded")
        self.assertEqual(self.lic.all(), [])
        self.assertEqual(len([c for c in self.stripe.calls if c["method"] == "GET"]), 0)            # nothing verified/issued


class AdaptivePricingTest(StripeBase):
    """Buyer pays in HUF through Stripe Adaptive Pricing; the shop price (EUR) is in currency_conversion."""
    def huf_session(self, src_amount=1999, sid="cs_test_huf", pi="pi_huf"):
        s = session(sid=sid, currency="huf", amount=799000, pi=pi)
        s["currency_conversion"] = {"source_currency": "eur", "amount_subtotal": src_amount, "amount_total": src_amount, "fx_rate": "399.7"}
        return s

    def test_huf_payment_issues_licence_and_books_eur(self):
        self.stripe.sessions["cs_test_huf"] = self.huf_session()
        r = self.deliver({"id": "cs_test_huf"})
        self.assertEqual(len(r["licences"]), 1)
        o = self.shop.orders()[0]
        self.assertEqual((o["total"], o["currency"], o["paid_currency"], o["paid_total"]), (1999, "EUR", "HUF", 799000))
        m = self.owner_mails()[0]
        self.assertIn("19.99 EUR", m["text"]); self.assertIn("HUF-ban fizetett: 7990.00 HUF", m["text"])
        # full refund of the HUF charge → revoked, booked as the EUR order total
        self.deliver({"id": "ch_h", "amount": 799000, "amount_refunded": 799000, "refunded": True, "currency": "huf", "payment_intent": "pi_huf",
                      "billing_details": {"email": "vevo@example.com"}}, etype="charge.refunded", ev_id="evt_hr")
        self.assertEqual(self.lic.get(r["licences"][0])["status"], "revoked")
        self.assertEqual(self.shop.orders()[0]["refunded_cents"], 1999)

    def test_eur_session_paid_in_huf_on_the_charge_shows_huf_to_the_owner(self):
        """Shape of the real staging test 2026-11-18: session eur 1499, no currency_conversion, charge presentment 574738 huf."""
        self.stripe.sessions["cs_test_1"] = session(product="EBD-AND", amount=1499, pi="pi_h2")
        self.stripe.pis = {"pi_h2": {"id": "pi_h2", "latest_charge": {"id": "ch_1", "amount": 1499, "currency": "eur",
                                                                      "presentment_details": {"presentment_amount": 574738, "presentment_currency": "huf"}}}}
        r = self.deliver({"id": "cs_test_1"})
        self.assertEqual(len(r["licences"]), 1)
        o = self.shop.orders()[0]
        self.assertEqual((o["total"], o["currency"], o["paid_currency"], o["paid_total"]), (1499, "EUR", "HUF", 574738))
        self.assertIn("HUF-ban fizetett: 5747.38 HUF", self.owner_mails()[0]["text"])
        q = urllib.parse.parse_qs(urllib.parse.urlsplit([c for c in self.stripe.calls if "/payment_intents/" in c["url"]][0]["url"]).query)
        self.assertEqual(q["expand[0]"], ["latest_charge"])

    def test_charge_lookup_failure_never_blocks_the_licence(self):
        self.stripe.sessions["cs_test_1"] = session(pi="pi_unknown")             # payment_intents → 404
        self.assertEqual(len(self.deliver({"id": "cs_test_1"})["licences"]), 1)
        self.assertNotIn("paid_currency", self.shop.orders()[0])

    def test_converted_session_with_wrong_source_amount_is_rejected(self):
        self.stripe.sessions["cs_test_bad"] = self.huf_session(src_amount=100, sid="cs_test_bad", pi="pi_bad")
        self.assertIn("amount mismatch", self.deliver({"id": "cs_test_bad"})["ignored"])
        self.assertEqual(self.lic.all(), [])


class OwnerNoticeTest(StripeBase):
    def test_one_sale_notice_with_invoicing_data_and_no_licence_key(self):
        self.stripe.sessions["cs_test_1"] = session(lang="de")
        self.deliver({"id": "cs_test_1"})
        self.deliver({"id": "cs_test_1"}, etype="checkout.session.async_payment_succeeded", ev_id="evt_2")   # second event: no 2nd notice
        mails = self.owner_mails()
        self.assertEqual(len(mails), 1)
        m = mails[0]
        self.assertTrue(m["subject"].startswith("[TESZT] Új vásárlás:")); self.assertIn("19.99 EUR", m["subject"])
        for x in ("Vevő Béla", "vevo@example.com", "Fő utca 1., 1011, Budapest, HU", "HU12345678 (eu_vat)", "cs_test_1", "pi_1",
                  "https://dashboard.stripe.com/test/payments/pi_1", "Vásárló nyelve: de", "licenclevél: sent", "TESZTVÁSÁRLÁS"):
            self.assertIn(x, m["text"], x)
        key = re.search(r"EBDW-[0-9A-Z-]{23}", self.licence_mails()[0]["payload"]["text"]).group(0)
        self.assertNotIn(key, m["text"])                                         # never the licence key
        self.assertNotIn("?t=", m["text"])                                       # nor a download link

    def test_live_notice_asks_for_invoice_and_private_buyer_without_tax_id(self):
        os.environ["SHOP_MODE"] = "live"; self.shop.stripe = FakeStripe("sk_live_1"); self.stripe = self.shop.stripe
        s = session(live=True, pi="pi_9"); s["customer_details"].pop("tax_ids")
        self.stripe.sessions["cs_test_1"] = s
        self.deliver({"id": "cs_test_1"}, live=True)
        m = self.owner_mails()[0]
        self.assertTrue(m["subject"].endswith("számla kiállítandó")); self.assertNotIn("TESZT", m["subject"])
        self.assertIn("Adószám: - (magánszemély)", m["text"]); self.assertIn("https://dashboard.stripe.com/payments/pi_9", m["text"])

    def test_fallback_to_reply_to_and_missing_address_never_breaks_the_purchase(self):
        os.environ.pop("OWNER_NOTIFY_EMAIL"); os.environ["MAIL_REPLY_TO"] = "reply@example.com"
        self.stripe.sessions["cs_test_1"] = session()
        self.deliver({"id": "cs_test_1"})
        self.assertTrue(any(c["payload"]["to"] == ["reply@example.com"] for c in self.mail.calls))
        os.environ.pop("MAIL_REPLY_TO")
        self.stripe.sessions["cs_test_2"] = session(sid="cs_test_2", pi="pi_2")
        r = self.deliver({"id": "cs_test_2"}, ev_id="evt_x")
        self.assertEqual(len(r["licences"]), 1)                                  # licence + buyer mail still fine
        self.assertEqual(len(self.licence_mails()), 2)


class RefundTest(StripeBase):
    def setUp(self):
        super().setUp()
        self.stripe.sessions["cs_test_1"] = session(lang="en")
        self.lid = self.deliver({"id": "cs_test_1"})["licences"][0]

    def charge(self, refunded, full, email="vevo@example.com"):
        return {"id": "ch_1", "object": "charge", "amount": 1999, "amount_refunded": refunded, "refunded": full, "currency": "eur",
                "payment_intent": "pi_1", "billing_details": {"email": email}}

    def test_partial_refund_keeps_licence_full_refund_revokes_and_notifies(self):
        r = self.deliver(self.charge(500, False), etype="charge.refunded", ev_id="evt_p")
        self.assertTrue(r["partial_refund"]); self.assertEqual(self.lic.get(self.lid)["status"], "active")
        r = self.deliver(self.charge(1999, True), etype="charge.refunded", ev_id="evt_f")
        self.assertEqual(r["revoked"], [self.lid]); self.assertEqual(self.lic.get(self.lid)["status"], "revoked")
        notice = [c["payload"] for c in self.mail.calls if c["payload"]["to"] == ["vevo@example.com"]][-1]   # buyer notice
        self.assertIn("refunded", notice["subject"].lower() + notice["text"].lower()); self.assertIn("Stripe", notice["text"])
        self.assertNotIn("Lemon", notice["text"])
        self.assertEqual(self.shop.orders()[0]["refunded_cents"], 1999)
        self.assertTrue(self.deliver(self.charge(1999, True), etype="charge.refunded", ev_id="evt_f")["duplicate"])

    def test_refunds_notify_the_owner_for_the_storno_invoice(self):
        self.deliver(self.charge(500, False), etype="charge.refunded", ev_id="evt_p")
        self.deliver(self.charge(1999, True), etype="charge.refunded", ev_id="evt_f")
        subj = [m["subject"] for m in self.owner_mails()]
        self.assertTrue(any("Részleges visszatérítés: 5.00 EUR" in x for x in subj), subj)
        full = [m for m in self.owner_mails() if "TELJES" in m["subject"]][0]
        self.assertIn("Licenc visszavonva: " + self.lid, full["text"]); self.assertIn("19.99 EUR / 19.99 EUR", full["text"])

    def test_failed_refund_and_chargebacks(self):
        self.deliver(self.charge(1999, True), etype="charge.refunded", ev_id="evt_f")
        r = self.deliver({"id": "re_1", "object": "refund", "status": "failed", "failure_reason": "expired_or_canceled_card", "amount": 1999,
                          "currency": "eur", "payment_intent": "pi_1"}, etype="charge.refund.updated", ev_id="evt_rf")
        self.assertTrue(r["refund_failed"])
        self.assertIn("SIKERTELEN visszatérítés (failed)", self.owner_mails()[-1]["subject"])
        self.assertEqual(self.deliver({"id": "re_2", "status": "succeeded", "payment_intent": "pi_1"}, etype="charge.refund.updated",
                                      ev_id="evt_rs")["ignored"], "refund succeeded")

    def test_dispute_opened_keeps_licence_lost_revokes(self):
        d = {"id": "dp_1", "object": "dispute", "amount": 1999, "currency": "eur", "payment_intent": "pi_1", "reason": "fraudulent"}
        self.deliver(dict(d, status="needs_response"), etype="charge.dispute.created", ev_id="evt_d1")
        self.assertEqual(self.lic.get(self.lid)["status"], "active")
        self.assertIn("Chargeback (needs_response)", self.owner_mails()[-1]["subject"])
        r = self.deliver(dict(d, status="lost"), etype="charge.dispute.closed", ev_id="evt_d2")
        self.assertEqual(r["revoked"], [self.lid]); self.assertEqual(self.lic.get(self.lid)["status"], "revoked")
        self.assertIn("sztornó", self.owner_mails()[-1]["text"])

    def test_refund_without_billing_email_reads_the_session(self):
        self.deliver(self.charge(1999, True, email=""), etype="charge.refunded", ev_id="evt_f")
        self.assertTrue(any("refunded" in c["payload"]["subject"].lower() + c["payload"]["text"].lower()
                            for c in self.mail.calls if c["payload"]["to"] == ["vevo@example.com"]))

    def test_refund_of_unknown_payment_is_ignored(self):
        c = dict(self.charge(1999, True), payment_intent="pi_other")
        self.assertEqual(self.deliver(c, etype="charge.refunded", ev_id="evt_u")["ignored"], "unknown payment")
        self.assertEqual(self.lic.get(self.lid)["status"], "active")


class NoLossTest(StripeBase):
    """3.3: a sleeping / restarting free Render instance and Stripe's retries can never lose a paid licence or its e-mail;
    every uncertain delivery is reported to the owner."""
    def owner_subjects(self):
        return [m["subject"] for m in self.owner_mails()]

    def test_crash_after_licence_before_mail_is_completed_by_the_retry(self):
        self.stripe.sessions["cs_test_1"] = session()
        key, rec = self.lic.create("EBD-WIN", email="vevo@example.com", test=True, source="stripe", order_id="cs_test_1")  # then: crash
        r = self.deliver({"id": "cs_test_1"})                                   # Stripe retry after the wake-up
        self.assertEqual((r["licences"], r["recovered"]), ([], [rec["lid"]]))
        self.assertEqual(len(self.licence_mails()), 1)                          # the mail goes out now - exactly once
        mailed = re.search(r"EBDW-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", self.licence_mails()[0]["payload"]["text"]).group(0)
        self.assertEqual((mailed, self.lic.find_by_key(mailed)["lid"]), (key, rec["lid"]))   # the original key, recovered
        self.assertEqual(len([o for o in self.shop.orders() if o["order"] == "cs_test_1"]), 1)
        self.assertTrue(any("Új vásárlás" in x for x in self.owner_subjects()))
        self.assertTrue(self.deliver({"id": "cs_test_1"}, ev_id="evt_9")["licences"] == [])   # later retries: nothing more
        self.assertEqual(len(self.licence_mails()), 1)

    def test_crash_during_mail_send_is_reported_never_duplicated(self):
        self.stripe.sessions["cs_test_1"] = session()
        key, rec = self.lic.create("EBD-WIN", email="vevo@example.com", test=True, source="stripe", order_id="cs_test_1")
        self.store.incr(f"mail:once:purchase:{rec['lid']}", 100)              # send started, result never recorded
        self.deliver({"id": "cs_test_1"})
        self.assertEqual(self.licence_mails(), [])                              # no blind second send
        self.assertTrue(any("kézbesítése sikertelen (interrupted)" in x for x in self.owner_subjects()))
        self.assertEqual(self.shop.mail_lists()["uncertain"][0]["lid"], rec["lid"])   # owner can re-send it with one click
        self.assertEqual(self.shop.orders()[0]["mail_state"], "interrupted")

    def test_mail_provider_down_alerts_owner_and_delivers_later(self):
        self.stripe.sessions["cs_test_1"] = session()
        self.mail.status = 422                                                  # provider refuses everything (also the owner notice)
        r = self.deliver({"id": "cs_test_1"})
        self.assertEqual(len(r["licences"]), 1)                                 # licence exists regardless
        self.assertEqual(self.shop.orders()[0]["mail_state"], "queued")
        pending = self.store.get("mail:owner_pending")
        self.assertTrue(any("kézbesítése sikertelen (queued)" in x["subject"] for x in pending))   # the alert is kept, not lost
        self.mail.status = 200; self.mail.calls.clear()                         # provider works again
        self.shop.process_mail_queue()
        self.assertEqual(len(self.licence_mails()), 1)
        subj = self.owner_subjects()
        self.assertTrue(any("kézbesítése sikertelen (queued)" in x for x in subj), subj)
        self.assertTrue(any("utólag kézbesítve" in x for x in subj), subj)
        self.assertEqual(self.store.get("mail:owner_pending"), [])
        self.assertEqual(self.shop.orders()[0]["mail_state"], "sent")

    def test_uncertain_send_alerts_owner(self):
        self.stripe.sessions["cs_test_1"] = session()
        self.mail.status = 503                                                  # 5xx after sending: delivery uncertain
        self.deliver({"id": "cs_test_1"})
        self.mail.status = 200
        self.assertIn(self.shop.orders()[0]["mail_state"], ("uncertain", "queued"))
        self.shop.process_mail_queue()
        self.assertTrue(any("kézbesítése sikertelen" in x for x in self.owner_subjects()))

    def test_queue_gives_up_after_14_days_and_tells_the_owner(self):
        self.stripe.sessions["cs_test_1"] = session()
        self.mail.status = 422
        self.deliver({"id": "cs_test_1"})
        self.clock.t += 15 * 86_400
        self.mail.status = 200
        self.shop.process_mail_queue(); self.shop.process_mail_queue()
        self.assertEqual(len([x for x in self.owner_subjects() if "(gave_up)" in x]), 1)

    def test_rejected_paid_session_is_never_silent(self):
        self.stripe.sessions["cs_test_bad"] = session(sid="cs_test_bad", amount=100)
        self.deliver({"id": "cs_test_bad"})
        self.assertTrue(any("LICENC NÉLKÜL" in x for x in self.owner_subjects()))

    def test_lost_webhook_reconcile_issues_the_licence_once(self):
        self.stripe.sessions["cs_test_1"] = session()                           # paid, but no webhook ever arrived
        self.stripe.sessions["cs_test_open"] = session(sid="cs_test_open", paid=False)
        self.stripe.sessions["cs_test_other"] = dict(session(sid="cs_test_other"), metadata={"product": "EBD-WIN", "shop": "other"})
        r = self.shop.reconcile_stripe(force=True)
        self.assertEqual((r["checked"], r["fixed"]), (1, 1))
        self.assertEqual(len(self.licence_mails()), 1)
        self.assertTrue(any("Pótolt licenc" in x for x in self.owner_subjects()))
        self.assertEqual(self.shop.reconcile_stripe(force=True)["fixed"], 0)    # second run: nothing to do
        self.assertEqual(self.shop.reconcile_stripe()["skipped"], "ran recently")
        self.assertEqual(self.deliver({"id": "cs_test_1"})["licences"], [])     # a very late webhook: no duplicate
        self.assertEqual(len(self.licence_mails()), 1)

    def test_reconcile_needs_matching_key_mode(self):
        self.shop.stripe = FakeStripe("sk_live_x")                              # SHOP_MODE=test
        self.assertIn("skipped", self.shop.reconcile_stripe(force=True))

    def test_thank_you_page_confirm(self):
        self.stripe.sessions["cs_test_1234567890"] = session(sid="cs_test_1234567890")
        self.assertEqual(self.shop.stripe_confirm("cs_test_1234567890"), {"status": "ok"})
        self.assertEqual(len(self.licence_mails()), 1)
        self.assertEqual(self.shop.stripe_confirm("cs_test_1234567890"), {"status": "ok"})   # repeat: idempotent
        self.assertEqual(len(self.licence_mails()), 1)
        self.stripe.sessions["cs_test_unpaid0001"] = session(sid="cs_test_unpaid0001", paid=False)
        self.assertEqual(self.shop.stripe_confirm("cs_test_unpaid0001"), {"status": "pending"})
        self.assertEqual(self.shop.stripe_confirm("cs_live_1234567890"), {"status": "pending"})  # other mode: ignored
        self.assertEqual(self.shop.stripe_confirm("cs_test_missing0001"), {"status": "pending"})  # Stripe 404 → retry later
        with self.assertRaises(S.LicenseError):
            self.shop.stripe_confirm("../../etc")


if __name__ == "__main__":
    unittest.main()
