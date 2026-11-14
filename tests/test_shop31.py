"""3.1: automatic purchase mail (HU/DE/EN, API mail providers), private-bucket downloads, key re-send, Smart Hardware
interest list (double opt-in, unsubscribe, admin export), staging key prefix."""
import datetime, hashlib, hmac, json, os, re, sys, unittest, urllib.parse

from tests_path import *  # noqa
from fakes import install_web_stubs, FakeRequest

install_web_stubs()
import interest as I
import licensing as L
import mail_templates as MT
import mailer as M
import quota as Q
import s3presign
import shop as S
from store import MemoryStore, UpstashStore

ENV_KEYS = ("LEMONSQUEEZY_WEBHOOK_SECRET", "LS_PRODUCT_MAP", "SHOP_MODE", "DOWNLOAD_SECRET", "DOWNLOAD_URL_EBD_WIN", "LICENSE_KEY_PEPPER",
            "RESEND_API_KEY", "BREVO_API_KEY", "MAIL_FROM", "MAIL_PROVIDER", "DOWNLOAD_S3_ENDPOINT", "DOWNLOAD_S3_BUCKET",
            "DOWNLOAD_S3_ACCESS_KEY_ID", "DOWNLOAD_S3_SECRET_ACCESS_KEY", "DOWNLOAD_S3_REGION", "DOWNLOAD_OBJECT_EBD_WIN",
            "PUBLIC_BASE_URL", "SUPPORT_EMAIL", "INTEREST_SECRET", "LICENSE_KEY_ENC_SECRET")
DEV_A = "a" * 64


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


class CapturingMailer(M.Mailer):
    """Real Mailer, provider HTTP call captured instead of sent."""
    def __init__(self, store, status=200):
        super().__init__(store); self.calls, self.status = [], status

    def _http_post(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        return self.status, '{"id":"x"}'


def ls_event(name="order_created", order_id="1001", test_mode=True, variant="111", status="paid", email="vevo@example.com", lang=None):
    meta = {"event_name": name, "test_mode": test_mode}
    if lang:
        meta["custom_data"] = {"lang": lang}
    return json.dumps({"meta": meta, "data": {"type": "orders", "id": order_id, "attributes": {
        "status": status, "user_email": email, "user_name": "Vevő Béla", "currency": "EUR", "total": 1999,
        "first_order_item": {"variant_id": variant, "product_id": 9}}}}).encode()


def sign(raw, secret="whsec"):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({"LEMONSQUEEZY_WEBHOOK_SECRET": "whsec", "LS_PRODUCT_MAP": '{"111":"EBD-WIN","222":"EBD-AND"}', "SHOP_MODE": "test",
                           "DOWNLOAD_SECRET": "dl-secret", "DOWNLOAD_URL_EBD_WIN": "https://files.example/legacy.exe",
                           "LICENSE_KEY_PEPPER": "pepper-1", "RESEND_API_KEY": "re_test", "MAIL_FROM": "E-Bike Doctor PRO <shop@example.com>",
                           "PUBLIC_BASE_URL": "https://api.example", "SUPPORT_EMAIL": "help@example.com", "INTEREST_SECRET": "int-secret"})
        self.clock = Clock()
        self.store = MemoryStore()
        self.quota = Q.QuotaManager(self.store, Q.TokenSigner("s"))
        self.lic = L.LicenseService(self.store, self.quota.signer, lease_signer=L.LeaseSigner(_pem()), clock=self.clock)
        self.mail = CapturingMailer(self.store)
        self.shop = S.Shop(self.store, self.lic, self.mail, clock=self.clock)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_PEM = None


def _pem():
    global _PEM
    if _PEM is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        _PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                                     serialization.NoEncryption()).decode()
    return _PEM


class PurchaseMailTest(Base):
    def test_purchase_mail_goes_out_in_buyer_language_with_key_link_and_steps(self):
        raw = ls_event(lang="hu")
        r = self.shop.handle_webhook(raw, sign(raw))
        self.assertEqual(len(r["licences"]), 1)
        call = self.mail.calls[-1]
        self.assertEqual(call["url"], M.RESEND_URL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer re_test")
        p = call["payload"]
        self.assertEqual(p["to"], ["vevo@example.com"]); self.assertEqual(p["from"], "E-Bike Doctor PRO <shop@example.com>")
        self.assertIn("licenckulcs és letöltés", p["subject"])
        key = re.search(r"EBDW-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", p["text"]).group(0)
        self.assertEqual(self.lic.find_by_key(key)["lid"], r["licences"][0])            # the mailed key activates
        self.assertIn("https://api.example/v1/shop/download?t=", p["text"])
        self.assertIn("Licenc és vásárlás → Aktiválás", p["text"])
        self.assertIn("TESZTVÁSÁRLÁS", p["text"])                                          # LS test mode is visible to the buyer
        self.assertIn(key, p["html"]); self.assertIn("help@example.com", p["text"])
        box = self.store.get("mail:outbox")[-1]
        self.assertTrue(box["sent"]); self.assertEqual(box["provider"], "resend")
        self.assertNotIn(key, json.dumps(box)); self.assertNotRegex(box["body_preview"], r"\?t=[A-Za-z0-9]")   # nothing secret in the log

    def test_language_defaults_and_german_mail(self):
        raw = ls_event(order_id="2", lang="de", variant="222")
        self.shop.handle_webhook(raw, sign(raw))
        p = self.mail.calls[-1]["payload"]
        self.assertIn("Lizenzschlüssel und Download", p["subject"]); self.assertIn("EBDA-", p["text"]); self.assertIn("APK", p["text"])
        raw = ls_event(order_id="3")                                                        # no lang -> English
        self.shop.handle_webhook(raw, sign(raw))
        self.assertIn("licence key and download", self.mail.calls[-1]["payload"]["subject"])

    def test_brevo_payload_and_provider_failure_never_breaks_the_webhook(self):
        os.environ.pop("RESEND_API_KEY"); os.environ["BREVO_API_KEY"] = "xkeysib-test"
        self.mail.status = 401
        raw = ls_event(order_id="9")
        r = self.shop.handle_webhook(raw, sign(raw))
        self.assertEqual(len(r["licences"]), 1)                                           # licence exists although mail failed
        call = self.mail.calls[-1]
        self.assertEqual(call["url"], M.BREVO_URL); self.assertEqual(call["headers"]["api-key"], "xkeysib-test")
        self.assertEqual(call["payload"]["sender"], {"email": "shop@example.com", "name": "E-Bike Doctor PRO"})
        self.assertEqual(call["payload"]["to"], [{"email": "vevo@example.com"}])
        box = self.store.get("mail:outbox")[-1]
        self.assertFalse(box["sent"]); self.assertEqual(box["error"], "HTTP 401")

    def test_no_provider_keeps_redacted_outbox_only(self):
        os.environ.pop("RESEND_API_KEY")
        m = M.Mailer(self.store)
        self.assertEqual((m.provider, m.configured), ("none", False))
        e = m.send("a@b.example", "s", "key EBDW-AAAAA-BBBBB-CCCCC-DDDDD link https://x/v1/shop/download?t=abc.def", kind="purchase")
        self.assertFalse(e["sent"])
        prev = self.store.get("mail:outbox")[-1]["body_preview"]
        self.assertNotIn("AAAAA", prev); self.assertNotIn("abc.def", prev)


class PriceTest(Base):
    def test_both_products_are_one_time_eur_prices_in_every_language(self):
        self.assertEqual(S.PRICES, {"EBD-WIN": ("19.99", "EUR"), "EBD-AND": ("14.99", "EUR")})
        want = {"EBD-WIN": {"hu": "19,99 €", "de": "19,99 €", "en": "€19.99"}, "EBD-AND": {"hu": "14,99 €", "de": "14,99 €", "en": "€14.99"}}
        for prod, langs in want.items():
            for lang, price in langs.items():
                _, text, html = MT.purchase(prod, lang, "", "EBDX-AAAAA-BBBBB-CCCCC-DDDDD", "https://x/dl", False)
                self.assertIn(price, text); self.assertIn(price, html)
                self.assertNotIn("Ft", text); self.assertNotIn("HUF", text)


class RefundTest(Base):
    def _buy(self, oid, lang, variant="111"):
        raw = ls_event(order_id=oid, lang=lang, variant=variant)
        return self.shop.handle_webhook(raw, sign(raw))["licences"][0]

    def _refund(self, oid, status="refunded"):
        raw = ls_event("order_refunded", order_id=oid, status=status)
        return self.shop.handle_webhook(raw, sign(raw))

    def test_full_refund_revokes_licence_blocks_download_and_notifies_in_buyer_language(self):
        for oid, lang, subj in (("71", "hu", "visszatérítés és licencvisszavonás"), ("72", "de", "Erstattung und Lizenzwiderruf"),
                                ("73", "en", "refund and licence revocation")):
            lid = self._buy(oid, lang)
            link = self.shop.download_link(lid, "EBD-WIN")
            r = self._refund(oid)
            self.assertEqual(r["revoked"], [lid])
            self.assertEqual(self.lic.get(lid)["status"], "revoked")
            p = self.mail.calls[-1]["payload"]
            self.assertIn(subj, p["subject"]); self.assertIn(oid, p["text"]); self.assertIn(self.lic.get(lid)["key_hint"], p["text"])
            self.assertIn(MT.T[lang]["test"], p["text"])                                          # test purchase is labelled
            with self.assertRaises(L.LicenseError) as cm:
                self.shop.resolve_download(link.split("t=", 1)[1])
            self.assertEqual(cm.exception.code, "revoked")
            self.assertTrue([o for o in self.shop.orders() if o["order"] == oid][0]["refunded"])
        self.assertEqual(self.shop.stats()["months"][0]["refunds_test"], 3)

    def test_partial_refund_keeps_licence_and_a_later_full_refund_still_revokes(self):
        lid = self._buy("81", "en")
        n = len(self.mail.calls)
        r = self._refund("81", status="partial_refund")
        self.assertTrue(r["partial_refund"]); self.assertEqual(self.lic.get(lid)["status"], "active"); self.assertEqual(len(self.mail.calls), n)
        r = self._refund("81")                                                                    # not swallowed as a duplicate
        self.assertEqual(r["revoked"], [lid]); self.assertEqual(self.lic.get(lid)["status"], "revoked")
        self.assertTrue(self._refund("81")["duplicate"])                                          # LS retry of the same event
        self.assertEqual(len(self.mail.calls), n + 1)                                             # exactly one notice


class OrderMoneyTest(Base):
    """3.2.x: the admin shows net / VAT / gross / refunded amounts exactly as Lemon Squeezy reports them."""
    def _event(self, name, oid, status, **amounts):
        ev = json.loads(ls_event(name, order_id=oid, status=status, lang="de"))
        ev["data"]["attributes"].update(amounts)
        raw = json.dumps(ev).encode()
        return self.shop.handle_webhook(raw, sign(raw))

    def test_tax_and_refund_amounts_are_recorded_per_order_and_per_month(self):
        self._event("order_created", "91", "paid", subtotal=1680, tax=319, total=1999, tax_name="VAT", tax_rate="19.00",
                    tax_inclusive=True, discount_total=0, total_usd=2310, tax_usd=369)
        o = self.shop.orders()[0]
        self.assertEqual((o["subtotal"], o["tax"], o["total"], o["tax_name"], o["tax_rate"], o["tax_inclusive"], o["total_usd"]),
                         (1680, 319, 1999, "VAT", 19.0, True, 2310))
        m = self.shop.stats()["months"][0]
        self.assertEqual((m["test_revenue_EUR"], m["test_tax_EUR"]), (19.99, 3.19))
        self.assertNotIn("revenue_EUR", m)                                                         # nothing leaks into the live figures
        self._event("order_refunded", "91", "partial_refund", refunded_amount=500)
        self._event("order_refunded", "91", "refunded", refunded_amount=1999)                     # cumulative in LS
        self._event("order_refunded", "91", "refunded", refunded_amount=1999)                     # retry: ignored
        o = self.shop.orders()[0]
        self.assertEqual((o["refunded_cents"], o["partial_refund_cents"], o["refunded"]), (1999, 500, True))
        self.assertEqual(self.shop.stats()["months"][0]["test_refunded_EUR"], 19.99)              # 5.00 + 14.99, never double counted
        self.shop.purge_test_data()
        m = self.shop.stats()["months"][0]
        self.assertEqual([k for k in m if k.startswith("test_") and m[k]], [])

    def test_missing_or_odd_amounts_never_break_the_webhook(self):
        r = self._event("order_created", "92", "paid", tax=None, tax_rate="n/a", subtotal="x")
        self.assertEqual(len(r["licences"]), 1)
        o = self.shop.orders()[0]
        self.assertEqual((o["tax"], o["tax_rate"], o["subtotal"], o["total"]), (0, 0.0, 0, 1999))


class DownloadTest(Base):
    def test_sigv4_matches_the_aws_reference_vector(self):
        u = s3presign.presign_get("examplebucket.s3.amazonaws.com", "/test.txt", "AKIAIOSFODNN7EXAMPLE",
                                  "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", 86400,
                                  datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(u.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))

    def test_private_bucket_link_is_short_lived_and_licence_bound(self):
        os.environ.update({"DOWNLOAD_S3_ENDPOINT": "https://acc123.r2.cloudflarestorage.com", "DOWNLOAD_S3_BUCKET": "ebd-downloads",
                           "DOWNLOAD_S3_ACCESS_KEY_ID": "AKID", "DOWNLOAD_S3_SECRET_ACCESS_KEY": "SECRET",
                           "DOWNLOAD_OBJECT_EBD_WIN": "windows/EBikeDoctorPRO-Setup-1.1.0.exe"})
        raw = ls_event()
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        url = self.shop.resolve_download(self.shop.download_token(lid, "EBD-WIN"))
        u = urllib.parse.urlsplit(url); q = urllib.parse.parse_qs(u.query)
        self.assertEqual(u.netloc, "acc123.r2.cloudflarestorage.com")
        self.assertEqual(u.path, "/ebd-downloads/windows/EBikeDoctorPRO-Setup-1.1.0.exe")
        self.assertEqual(q["X-Amz-Expires"], ["300"]); self.assertIn("/auto/s3/aws4_request", q["X-Amz-Credential"][0])
        self.assertIn('filename="EBikeDoctorPRO-Setup-1.1.0.exe"', q["response-content-disposition"][0])
        self.assertNotIn("SECRET", url)
        self.lic.revoke(lid, "refund")
        with self.assertRaises(L.LicenseError):
            self.shop.resolve_download(self.shop.download_token(lid, "EBD-WIN"))

    def test_daily_download_cap_per_licence(self):
        raw = ls_event()
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        tok = self.shop.download_token(lid, "EBD-WIN")
        for _ in range(10):
            self.shop.resolve_download(tok)
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.resolve_download(tok)
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "download_limit"))
        self.clock.t += 86_400
        self.assertTrue(self.shop.resolve_download(self.shop.download_token(lid, "EBD-WIN")))


class KeyResendTest(Base):
    def test_encrypted_key_copy_recovers_only_the_real_key(self):
        key, rec = self.lic.create("EBD-WIN", email="x@example.com")
        self.assertNotIn(key, json.dumps(rec)); self.assertTrue(rec["key_enc"])
        self.assertEqual(self.lic.recover_key(rec), key)
        self.assertIsNone(self.lic.recover_key(dict(rec, lid="L000999")))                # bound to its licence id
        os.environ["LICENSE_KEY_PEPPER"] = "other"
        self.assertIsNone(self.lic.recover_key(rec))                                       # useless without the server secret
        os.environ["LICENSE_KEY_PEPPER"] = "pepper-1"
        new_key, rec2 = self.lic.reissue_key(rec["lid"])
        self.assertEqual(self.lic.recover_key(rec2), new_key)
        self.assertNotIn("key_enc", L.public_view(rec2))

    def test_resend_mails_active_licences_only_to_that_address(self):
        raw = ls_event(email="Buyer@Example.com")
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        self.mail.calls.clear()
        self.assertEqual(self.shop.resend("buyer@example.com", "hu", "iph"), {"ok": True})
        p = self.mail.calls[-1]["payload"]
        self.assertEqual(p["to"], ["buyer@example.com"]); self.assertIn("licenceid", p["subject"])
        self.assertRegex(p["text"], r"EBDW-[0-9A-Z]{5}-"); self.assertIn("/v1/shop/download?t=", p["text"])
        self.lic.revoke(lid, "refund")
        self.shop.resend("buyer@example.com", "en", "iph")
        self.assertNotRegex(self.mail.calls[-1]["payload"]["text"], r"EBDW-")                # revoked: no key
        self.assertIn("no active licence", self.mail.calls[-1]["payload"]["text"])

    def test_resend_does_not_reveal_or_spam(self):
        self.assertEqual(self.shop.resend("nobody@example.com", "en", "iph"), {"ok": True})
        n = len(self.mail.calls)
        for _ in range(5):
            self.assertEqual(self.shop.resend("nobody@example.com", "en", "iph"), {"ok": True})
        self.assertEqual(len(self.mail.calls), n + 2)                                       # 3 mails / address / day
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.resend("bad-address", "en", "iph")
        self.assertEqual(cm.exception.status, 422)
        for i in range(10):
            try:
                self.shop.resend(f"u{i}@example.com", "en", "iph")
            except L.LicenseError as exc:
                self.assertEqual(exc.status, 429)
                break
        else:
            self.fail("per-network limit not applied")


class InterestTest(Base):
    def setUp(self):
        super().setUp()
        self.il = I.InterestList(self.store, self.mail, clock=self.clock)

    def _links(self):
        text = self.mail.calls[-1]["payload"]["text"]
        return (re.search(r"https://api\.example/v1/interest/confirm\?t=(\S+)", text).group(1),
                re.search(r"https://api\.example/v1/interest/unsubscribe\?t=(\S+)", text).group(1))

    def test_double_opt_in_confirm_and_unsubscribe_deletes(self):
        with self.assertRaises(L.LicenseError):
            self.il.subscribe("a@example.com", "hu", [], "rider", False, "iph")              # consent required
        r = self.il.subscribe("Muhely@Example.com", "hu", ["sensors", "bogus"], "workshop", True, "iph", name="Műhely Kft.")
        self.assertEqual(r, {"ok": True, "status": "check_email"})
        p = self.mail.calls[-1]["payload"]
        self.assertIn("Erősítsd meg", p["subject"])
        self.assertEqual(p["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        e = self.il.entries()[0]
        self.assertEqual((e["status"], e["interests"], e["role"], e["name"]), ("pending", ["sensors"], "workshop", "Műhely Kft."))
        confirm_t, unsub_t = self._links()
        self.assertEqual(self.il.confirm("x" + confirm_t[1:]), (None, "invalid"))
        self.assertEqual(self.il.confirm(unsub_t), (None, "invalid"))                        # tokens are purpose-bound
        self.assertEqual(self.il.confirm(confirm_t), ("hu", "confirmed"))
        self.assertEqual(self.il.stats()["confirmed"], 1)
        n = len(self.mail.calls)
        self.il.subscribe("muhely@example.com", "hu", [], "", True, "iph")                    # already confirmed: same answer, no mail
        self.assertEqual(len(self.mail.calls), n)
        self.assertEqual(self.il.unsubscribe(unsub_t), ("hu", "unsubscribed"))
        self.assertEqual(self.il.entries(), [])
        self.assertIsNone(self.store.get("int:r:" + L.email_hash("muhely@example.com")).get("email"))
        self.assertEqual(self.il.stats()["unsubscribed_total"], 1)

    def test_pending_expires_honeypot_and_limits(self):
        self.il.subscribe("bot@example.com", "en", [], "", True, "iph", honeypot="http://spam")
        self.assertEqual(self.il.entries(), []); self.assertEqual(self.mail.calls, [])
        self.il.subscribe("late@example.com", "en", [], "", True, "iph")
        confirm_t, _ = self._links()
        self.store._d = {k: (v, exp - 8 * 86_400 if exp else exp) for k, (v, exp) in self.store._d.items()}   # 8 days later
        self.assertEqual(self.il.confirm(confirm_t), (None, "invalid"))
        with self.assertRaises(L.LicenseError):
            self.il.subscribe("not-an-email", "en", [], "", True, "iph")
        for i in range(12):
            try:
                self.il.subscribe(f"p{i}@example.com", "en", [], "", True, "net2")
            except L.LicenseError as exc:
                self.assertEqual(exc.status, 429)
                break
        else:
            self.fail("per-network limit not applied")

    def test_admin_delete(self):
        self.il.subscribe("d@example.com", "de", [], "dealer", True, "iph")
        sid = self.il.entries()[0]["id"]
        self.assertFalse(self.il.delete("short"))
        self.assertTrue(self.il.delete(sid)); self.assertEqual(self.il.entries(), [])

    def test_pages_are_localized_and_escape(self):
        html = I.page("unsub_ask", "de", form_action='/v1/interest/unsubscribe?t=a"b')
        self.assertIn('lang="de"', html); self.assertIn("Abmelden", html); self.assertIn("&quot;", html); self.assertIn("noindex", html)


class StorePrefixTest(unittest.TestCase):
    def test_staging_prefix_isolates_every_key(self):
        s = UpstashStore("https://u.example", "tok", prefix="stg:")
        seen = []
        s._pipeline = lambda cmds: (seen.extend(cmds), [{"result": 1}, {"result": 1}][:len(cmds)] and [1, 1][:len(cmds)])[1]
        s.incr("lic:seq", 10); s.set("lic:r:L000001", {"a": 1}, 10); s.get("lic:r:L000001")
        self.assertTrue(all(c[1].startswith("stg:") for c in seen), seen)


class EndpointTest31(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.m = main

    def setUp(self):
        os.environ.update({"INTEREST_SECRET": "int-secret", "PUBLIC_BASE_URL": "https://api.example", "ADMIN_TOKEN": "adm-31"})
        self.mail = CapturingMailer(self.m.LIC_STORE)
        os.environ.update({"RESEND_API_KEY": "re_x", "MAIL_FROM": "shop@example.com"})
        self.m.INTEREST.mailer = self.mail
        self.m.SHOP.mailer = self.mail

    def tearDown(self):
        for k in ("INTEREST_SECRET", "PUBLIC_BASE_URL", "ADMIN_TOKEN", "RESEND_API_KEY", "MAIL_FROM"):
            os.environ.pop(k, None)

    def test_interest_flow_over_http_handlers(self):
        m = self.m
        r = m.interest_subscribe(m.InterestRequest(email="e2e@example.com", name="", lang="en", role="rider", interests=["battery-bms"],
                                                   consent=True, website=""), FakeRequest(ip="198.51.100.7"))
        self.assertEqual(r["status"], "check_email")
        text = self.mail.calls[-1]["payload"]["text"]
        ct = re.search(r"confirm\?t=(\S+)", text).group(1); ut = re.search(r"unsubscribe\?t=(\S+)", text).group(1)
        page = m.interest_confirm(ct)
        self.assertEqual(page.status_code, 200); self.assertIn("Sign-up confirmed", page.content)
        self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])
        csv = m.admin_interest("csv", x_admin_token="adm-31")
        self.assertIn("e2e@example.com", csv.content); self.assertIn("text/csv", csv.headers["Content-Type"])
        ask = m.interest_unsubscribe_page(ut, "en")
        self.assertIn("<form", ask.content)
        self.assertEqual(len([e for e in m.INTEREST.entries() if e["email"] == "e2e@example.com"]), 1)   # GET deletes nothing
        done = m.interest_unsubscribe(ut)
        self.assertIn("Unsubscribed", done.content)
        self.assertEqual([e for e in m.INTEREST.entries() if e["email"] == "e2e@example.com"], [])
        self.assertEqual(m.interest_confirm("bad.token").status_code, 404)
        with self.assertRaises(Exception) as cm:
            m.admin_interest("json", x_admin_token="wrong")
        self.assertEqual(cm.exception.status_code, 404)

    def test_resend_endpoint_is_generic(self):
        r = self.m.shop_resend(self.m.ResendRequest(email="unknown@example.com", lang="hu"), FakeRequest(ip="198.51.100.8"))
        self.assertEqual(r, {"ok": True})
        with self.assertRaises(Exception) as cm:
            self.m.shop_resend(self.m.ResendRequest(email="nope", lang="hu"), FakeRequest(ip="198.51.100.8"))
        self.assertEqual(cm.exception.status_code, 422)

    def test_admin_stats_reports_new_configuration_without_secrets(self):
        s = self.m.admin_stats(x_admin_token="adm-31")
        self.assertEqual(s["mail_provider"], "resend"); self.assertIn("interest", s); self.assertFalse(s["download_bucket_configured"])
        self.assertNotIn("re_x", json.dumps(s, default=str))


if __name__ == "__main__":
    unittest.main()
