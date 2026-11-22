"""Free 48-hour trial (EBIKE-TRIAL2) and the 50% promotion (EBIKE-50).

Trial: starts at the FIRST activation, enforced by the server (lease exp = trial end, refresh/AI/downloads refused after it),
one trial per e-mail and per device, no transfer, reinstall cannot restart it, unlimited different users, no payment card.
Promo: Stripe coupon applied by Checkout (one discount, never stackable), discounted price quoted before the Stripe page,
the paid discounted session issues the ordinary full licence; any other discount is rejected."""
import os, re, unittest, urllib.parse

from tests_path import *  # noqa
from fakes import FakeRequest
import licensing as L
import trial as TR
from test_stripe33 import StripeBase, session
from test_shop31 import CapturingMailer

DEV_A, DEV_B = "a" * 64, "b" * 64
H = 3600
TRIAL_ENV = ("TRIAL_ENABLED", "TRIAL_CODE", "TRIAL_HOURS", "TRIAL_SECRET", "TRIAL_PER_IP_PER_DAY", "STRIPE_COUPON_EBIKE50")


class TrialBase(StripeBase):
    def setUp(self):
        super().setUp()
        self._t_saved = {k: os.environ.get(k) for k in TRIAL_ENV}
        for k in TRIAL_ENV:
            os.environ.pop(k, None)
        os.environ.update({"TRIAL_ENABLED": "1", "TRIAL_SECRET": "trial-secret", "STRIPE_COUPON_EBIKE50": "coupon_ebike50"})
        self.trial = TR.TrialService(self.store, self.lic, self.shop, self.mail, clock=self.clock)

    def tearDown(self):
        for k, v in self._t_saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def mails_to(self, addr):
        return [c["payload"] for c in self.mail.calls if c["payload"].get("to") == [addr]]

    def token_from_last_mail(self, addr):
        return re.search(r"trial/confirm\?t=(\S+)", self.mails_to(addr)[-1]["text"]).group(1)

    def issue(self, email="proba@example.com", product="android", lang="hu"):
        self.trial.request(email, product, lang, "EBIKE-TRIAL2", True, "net-" + email)     # one network per user
        lang_, kind, data = self.trial.confirm(self.token_from_last_mail(email))
        self.assertEqual(kind, "issued")
        return data["key"]

    def lease(self, r):
        return L.verify_lease(r["lease"], self.lic.lease.public_spki_b64())


# --------------------------------------------------------------------------------------------- licence server rules
class TrialLicenceTest(TrialBase):
    def test_window_starts_at_first_activation_and_lease_never_outlives_it(self):
        key = self.issue()
        self.clock.t += 5 * H                                   # the key may wait in the mailbox: the clock has not started
        start = int(self.clock())
        r = self.lic.activate(key, "EBD-AND", DEV_A)
        p = self.lease(r)
        self.assertEqual(p["ed"], "TRIAL")
        self.assertEqual(p["exp"], start + 48 * H)               # not the 14-day PRO lease
        self.assertEqual(r["license"]["trial_expires"], start + 48 * H)
        self.assertEqual(r["license"]["transfers_left"], 0)
        self.clock.t += 30 * H                                  # refresh later: same end, never extended
        self.assertEqual(self.lease(self.lic.refresh(key, "EBD-AND", DEV_A))["exp"], start + 48 * H)

    def test_after_48h_everything_is_refused_server_side(self):
        key = self.issue()
        r = self.lic.activate(key, "EBD-AND", DEV_A)
        rec = self.lic.find_by_key(key)
        token_iid = f"lic-{rec['lid']}-{DEV_A[:8]}"
        self.assertEqual(self.lic.resolve_ai_subject(token_iid), "lic-" + rec["lid"])
        link = self.shop.new_download_link_for_key(key)
        self.clock.t += 48 * H
        for call in (lambda: self.lic.refresh(key, "EBD-AND", DEV_A), lambda: self.lic.activate(key, "EBD-AND", DEV_A),
                     lambda: self.shop.new_download_link_for_key(key), lambda: self.shop.resolve_download(link.split("t=")[1])):
            with self.assertRaises(L.LicenseError) as cm:
                call()
            self.assertEqual((cm.exception.code, cm.exception.status), ("trial_expired", 403))
        self.assertIsNone(self.lic.resolve_ai_subject(token_iid))        # the old AI token stops too
        self.assertIsNotNone(r)

    def test_reinstall_or_device_clock_cannot_restart_the_trial(self):
        key = self.issue()
        self.lic.activate(key, "EBD-AND", DEV_A)
        self.lic.deactivate(key, DEV_A)                          # "uninstall"
        self.clock.t += 49 * H
        with self.assertRaises(L.LicenseError) as cm:            # reinstall + same key: the start time is on the server
            self.lic.activate(key, "EBD-AND", DEV_A)
        self.assertEqual(cm.exception.code, "trial_expired")
        # a new e-mail address on the same phone: one trial per device
        key2 = self.issue("masik@example.com")
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.activate(key2, "EBD-AND", DEV_A)
        self.assertEqual((cm.exception.code, cm.exception.status), ("trial_used", 409))
        self.assertIsNone(L.trial_of(self.lic.find_by_key(key2)).get("started"))   # the refused trial did not start

    def test_no_transfer_to_another_device(self):
        key = self.issue()
        self.lic.activate(key, "EBD-AND", DEV_A)
        for transfer in (False, True):
            with self.assertRaises(L.LicenseError) as cm:
                self.lic.activate(key, "EBD-AND", DEV_B, transfer=transfer)
            self.assertEqual(cm.exception.code, "trial_device")

    def test_unlimited_users_each_get_their_own_trial_and_trials_are_not_sales(self):
        for i in range(25):
            key = self.issue(f"user{i}@example.com", "windows" if i % 2 else "android")
            prod = "EBD-WIN" if i % 2 else "EBD-AND"
            self.assertEqual(self.lease(self.lic.activate(key, prod, f"{i:064x}"))["ed"], "TRIAL")
        st = self.lic.stats("test")
        self.assertEqual(st["EBD-AND"]["trials"] + st["EBD-WIN"]["trials"], 25)
        self.assertEqual(st["EBD-AND"]["total"] + st["EBD-WIN"]["total"], 0)       # no sale counted

    def test_paid_licence_unaffected(self):
        key, rec = self.lic.create("EBD-WIN", "vevo@example.com", test=True)
        p = self.lease(self.lic.activate(key, "EBD-WIN", DEV_A))
        self.assertEqual(p["ed"], "PRO")
        self.assertEqual(p["exp"], int(self.clock()) + 14 * 86_400)
        self.assertNotIn("trial_expires", self.lic.activate(key, "EBD-WIN", DEV_A)["license"])


# --------------------------------------------------------------------------------------------- request / confirm flow
class TrialFlowTest(TrialBase):
    def test_double_opt_in_issues_one_licence_with_key_and_installer_link(self):
        r = self.trial.request("Proba@Example.com ", "windows", "de", "", True, "ip1")      # button: no code needed
        self.assertEqual(r, {"ok": True, "status": "check_email"})
        m = self.mails_to("Proba@Example.com")[-1]
        self.assertIn("48", m["subject"]); self.assertIn("Test starten", m["html"])
        t = self.token_from_last_mail("Proba@Example.com")
        self.assertEqual(self.trial.check(t), ("de", "ask"))
        self.assertEqual(self.lic.stats("test")["EBD-WIN"]["trials"], 0)                   # GET/check issues nothing
        lang, kind, data = self.trial.confirm(t)
        self.assertEqual((lang, kind, data["product"]), ("de", "issued", "EBD-WIN"))
        self.assertTrue(data["key"].startswith("EBDW-"))
        self.assertIn("/v1/shop/download?t=", data["link"])
        mail = self.mails_to("Proba@Example.com")[-1]
        self.assertIn(data["key"], mail["text"]); self.assertIn("/v1/shop/download?t=", mail["text"])
        self.assertIn("ebikedoctorpro.com/de/shop/", mail["text"])                         # buy link for after the trial
        again = self.trial.confirm(t)                                                       # second click: same key, no new licence
        self.assertEqual(again[2]["key"], data["key"])
        self.assertEqual(self.lic.stats("test")["EBD-WIN"]["trials"], 1)
        rec = self.lic.find_by_key(data["key"])
        self.assertEqual((rec["source"], L.trial_of(rec)["hours"], L.trial_of(rec)["campaign"]), ("trial", 48, "EBIKE-TRIAL2"))

    def test_one_trial_per_email(self):
        key = self.issue("egy@example.com")
        self.trial.request("egy@example.com", "windows", "hu", "", True, "ip2")
        self.assertIn(key, self.mails_to("egy@example.com")[-1]["text"])           # still running: the same key again
        self.lic.activate(key, "EBD-AND", DEV_A)
        self.clock.t += 49 * H
        self.trial.request("egy@example.com", "windows", "hu", "", True, "ip3")
        last = self.mails_to("egy@example.com")[-1]
        self.assertIn("már tartozik ingyenes próba", last["text"]); self.assertNotIn("trial/confirm", last["text"])
        self.assertEqual(sum(self.lic.stats("test")[p]["trials"] for p in ("EBD-WIN", "EBD-AND")), 1)

    def test_codes_consent_email_honeypot_and_switch(self):
        self.trial.request("a@example.com", "android", "en", " ebike-trial2 ", True, "ip1")       # case/space tolerant
        for args, code in (((("a@example.com", "android", "en", "EBIKE-50", True, "ip1")), "invalid_code"),
                           ((("a@example.com", "android", "en", "", False, "ip1")), "consent_required"),
                           ((("not-an-email", "android", "en", "", True, "ip1")), "bad_email"),
                           ((("a@example.com", "ios", "en", "", True, "ip1")), "bad_product")):
            with self.assertRaises(L.LicenseError) as cm:
                self.trial.request(*args)
            self.assertEqual(cm.exception.code, code)
        n = len(self.mail.calls)
        self.trial.request("bot@example.com", "android", "en", "", True, "ip1", honeypot="http://spam")
        self.assertEqual(len(self.mail.calls), n)
        os.environ["TRIAL_ENABLED"] = "0"
        with self.assertRaises(L.LicenseError) as cm:
            self.trial.request("b@example.com", "android", "en", "", True, "ip1")
        self.assertEqual((cm.exception.code, cm.exception.status), ("trial_unavailable", 503))

    def test_tampered_or_expired_links_and_rate_limit(self):
        self.trial.request("c@example.com", "android", "en", "", True, "ip9")
        t = self.token_from_last_mail("c@example.com")
        self.assertEqual(self.trial.confirm(t[:-2] + "xx")[1], "invalid")
        self.assertEqual(self.trial.confirm("garbage")[1], "invalid")
        self.clock.t += 25 * H
        self.assertEqual(self.trial.confirm(t)[1], "invalid")
        with self.assertRaises(L.LicenseError) as cm:
            for i in range(6):
                self.trial.request(f"r{i}@example.com", "android", "en", "", True, "same-net")
        self.assertEqual(cm.exception.code, "rate_limited")

    def test_confirmation_page_has_button_and_escapes(self):
        html = TR.page("ask", "hu", form_action='/v1/trial/confirm?t="><script>')
        self.assertIn("<form method=\"post\"", html); self.assertIn("Próba indítása", html); self.assertNotIn("<script>", html)
        html = TR.page("issued", "en", {"key": "EBDA-X", "link": "https://api.example/v1/shop/download?t=1"})
        self.assertIn("EBDA-X", html); self.assertIn("Download the app", html)


# --------------------------------------------------------------------------------------------- EBIKE-50
class PromoTest(TrialBase):
    def test_quote_shows_the_discount_before_payment(self):
        q = self.shop.promo_quote("android", " ebike-50")
        self.assertEqual((q["code"], q["percent"], q["original_cents"], q["discount_cents"], q["total_cents"], q["currency"]),
                         ("EBIKE-50", 50, 500, 250, 250, "USD"))
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.promo_quote("android", "FREE100")
        self.assertEqual(cm.exception.code, "invalid_promo")
        os.environ.pop("STRIPE_COUPON_EBIKE50")
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.promo_quote("windows", "EBIKE-50")
        self.assertEqual((cm.exception.code, cm.exception.status), ("promo_unavailable", 503))

    def test_checkout_gets_exactly_one_coupon_and_no_extra_code_field(self):
        self.shop.stripe_checkout("windows", "hu", True, promo="ebike-50")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertEqual(q["discounts[0][coupon]"], "coupon_ebike50")
        self.assertNotIn("discounts[1][coupon]", q)
        self.assertNotIn("allow_promotion_codes", q)                         # nothing to stack on the Stripe page
        self.assertEqual(q["metadata[promo]"], "EBIKE-50")
        self.assertEqual(q["line_items[0][price_data][unit_amount]"], "500")  # original price; Stripe shows -50% and the total
        self.shop.stripe_checkout("windows", "hu", True)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.stripe.calls[-1]["body"]).items()}
        self.assertNotIn("discounts[0][coupon]", q); self.assertNotIn("metadata[promo]", q)

    def test_unknown_code_never_silently_ignored(self):
        n = len(self.stripe.calls)
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.stripe_checkout("windows", "hu", True, promo="EBIKE-90")
        self.assertEqual(cm.exception.code, "invalid_promo")
        self.assertEqual(len(self.stripe.calls), n)                           # no session opened at full price instead

    def _paid(self, sid, discount, promo):
        s = session(sid, product="EBD-AND", amount=500)
        s["amount_total"] = 500 - discount
        s["total_details"]["amount_discount"] = discount
        if promo:
            s["metadata"]["promo"] = promo
        self.stripe.sessions[sid] = s
        return s

    def test_discounted_payment_issues_the_full_licence(self):
        s = self._paid("cs_test_promo1", 250, "EBIKE-50")
        r = self.deliver(s)
        self.assertNotIn("ignored", r)
        lid = self.store.get("lic:order:cs_test_promo1:EBD-AND")
        rec = self.lic.get(lid)
        self.assertIsNone(L.trial_of(rec))
        self.assertEqual(self.lease(self.lic.activate(self.licence_key_from_mail(), "EBD-AND", DEV_A))["ed"], "PRO")

    def licence_key_from_mail(self):
        return re.search(r"(EBDA-[0-9A-Z-]{23})", self.licence_mails()[-1]["payload"]["text"]).group(1)

    def test_other_discounts_are_rejected(self):
        for sid, disc, promo in (("cs_test_nopromo", 250, None), ("cs_test_wrongpct", 400, "EBIKE-50"), ("cs_test_unknown", 250, "XMAS")):
            r = self.deliver(self._paid(sid, disc, promo), ev_id="evt_" + sid)
            self.assertIn("discount", r.get("ignored", ""), sid)
            self.assertIsNone(self.store.get(f"lic:order:{sid}:EBD-AND"))


# --------------------------------------------------------------------------------------------- HTTP handlers
class TrialEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.m = main

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in TRIAL_ENV + ("PUBLIC_BASE_URL", "RESEND_API_KEY", "MAIL_FROM", "INTEREST_SECRET")}
        os.environ.update({"TRIAL_ENABLED": "1", "TRIAL_SECRET": "t-secret", "PUBLIC_BASE_URL": "https://api.example",
                           "RESEND_API_KEY": "re_x", "MAIL_FROM": "shop@example.com", "STRIPE_COUPON_EBIKE50": "coupon_x"})
        self.mail = CapturingMailer(self.m.LIC_STORE)
        self.m.TRIAL.mailer = self.mail

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_trial_over_http_and_promo_endpoint(self):
        m = self.m
        r = m.trial_request(m.TrialRequest(email="http@example.com", product="android", lang="en", code="EBIKE-TRIAL2", consent=True),
                            FakeRequest(ip="198.51.100.20"))
        self.assertEqual(r["status"], "check_email")
        t = re.search(r"trial/confirm\?t=(\S+)", self.mail.calls[-1]["payload"]["text"]).group(1)
        page = m.trial_confirm_page(t)
        self.assertEqual(page.status_code, 200); self.assertIn("<form", page.content)
        self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])
        done = m.trial_confirm(t)
        self.assertEqual(done.status_code, 200); self.assertRegex(done.content, r"EBDA-[0-9A-Z-]{23}")
        self.assertEqual(m.trial_confirm_page("bad").status_code, 404)
        self.assertEqual(m.shop_promo(m.PromoRequest(code="ebike-trial2", product="android"))["kind"], "trial")
        q = m.shop_promo(m.PromoRequest(code="EBIKE-50", product="windows"))
        self.assertEqual((q["kind"], q["total_cents"]), ("discount", 250)); self.assertNotIn("coupon", q)   # coupon id stays private
        with self.assertRaises(Exception) as cm:
            m.shop_promo(m.PromoRequest(code="NOPE", product="windows"))
        self.assertEqual(cm.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
