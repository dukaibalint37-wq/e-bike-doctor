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
        sid = url.rsplit("/", 1)[-1]
        if sid in self.sessions:
            return 200, json.dumps(self.sessions[sid])
        return 404, json.dumps({"error": {"type": "invalid_request_error", "code": "resource_missing"}})


def session(sid="cs_test_1", product="EBD-WIN", lang="hu", paid=True, live=False, amount=1999, currency="eur", email="vevo@example.com",
            mode="payment", pi="pi_1"):
    return {"id": sid, "object": "checkout.session", "mode": mode, "status": "complete" if paid else "open",
            "payment_status": "paid" if paid else "unpaid", "livemode": live, "amount_subtotal": amount, "amount_total": amount,
            "currency": currency, "customer_details": {"email": email, "name": "Vevő Béla"}, "payment_intent": pi,
            "total_details": {"amount_tax": 0, "amount_discount": 0}, "metadata": {"product": product, "lang": lang, "shop": "ebd-website"}}


def event(obj, etype="checkout.session.completed", ev_id="evt_1", live=False):
    return json.dumps({"id": ev_id, "object": "event", "type": etype, "livemode": live, "data": {"object": obj}}).encode()


def sig(raw, t, secret=WHSEC):
    return f"t={int(t)},v1=" + hmac.new(secret.encode(), str(int(t)).encode() + b"." + raw, hashlib.sha256).hexdigest()


class StripeBase(Base):
    def setUp(self):
        super().setUp()
        self._s_saved = {k: os.environ.get(k) for k in ("STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_MAP", "STRIPE_SECRET_KEY", "SITE_URL",
                                                         "STRIPE_PUBLISHABLE_KEY", "SHOP_ACCEPT_TEST_ORDERS")}
        for k in self._s_saved:
            os.environ.pop(k, None)
        os.environ["STRIPE_WEBHOOK_SECRET"] = WHSEC
        self.stripe = FakeStripe()
        self.shop = S.Shop(self.store, self.lic, self.mail, clock=self.clock, stripe=self.stripe)

    def tearDown(self):
        for k, v in self._s_saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def deliver(self, obj, etype="checkout.session.completed", ev_id="evt_1", live=False):
        raw = event(obj, etype, ev_id, live)
        return self.shop.handle_stripe_webhook(raw, sig(raw, self.clock()))

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
        self.assertEqual(q["cancel_url"], "https://ebikedoctorpro.com/de/shop/")
        self.shop.stripe_checkout("android", "hu", True)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertEqual((q["line_items[0][price_data][unit_amount]"], q["metadata[product]"]), ("1499", "EBD-AND"))
        self.assertEqual(q["success_url"], "https://ebikedoctorpro.com/hu/koszonjuk/?session_id={CHECKOUT_SESSION_ID}")

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
        notice = self.mail.calls[-1]["payload"]
        self.assertIn("refunded", notice["subject"].lower() + notice["text"].lower()); self.assertIn("Stripe", notice["text"])
        self.assertNotIn("Lemon", notice["text"])
        self.assertEqual(self.shop.orders()[0]["refunded_cents"], 1999)
        self.assertTrue(self.deliver(self.charge(1999, True), etype="charge.refunded", ev_id="evt_f")["duplicate"])

    def test_refund_without_billing_email_reads_the_session(self):
        self.deliver(self.charge(1999, True, email=""), etype="charge.refunded", ev_id="evt_f")
        self.assertEqual(self.mail.calls[-1]["payload"]["to"], ["vevo@example.com"])

    def test_refund_of_unknown_payment_is_ignored(self):
        c = dict(self.charge(1999, True), payment_intent="pi_other")
        self.assertEqual(self.deliver(c, etype="charge.refunded", ev_id="evt_u")["ignored"], "unknown payment")
        self.assertEqual(self.lic.get(self.lid)["status"], "active")


if __name__ == "__main__":
    unittest.main()
