"""3.0 commercial backend tests: licences, abuse attempts, Lemon Squeezy webhooks, downloads, cost protection, admin."""
import asyncio, hashlib, hmac, json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests_path import *  # noqa
from fakes import install_web_stubs, FakeRequest
install_web_stubs()

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import licensing as L
import mailer as M
import quota as Q
import shop as S
from store import MemoryStore

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
DEV_A = hashlib.sha256(b"device-A").hexdigest()
DEV_B = hashlib.sha256(b"device-B").hexdigest()
DEV_C = hashlib.sha256(b"device-C").hexdigest()


class Clock:
    def __init__(self, t=1_800_000_000): self.t = t
    def __call__(self): return self.t


def make(clock=None):
    store = MemoryStore()
    quota = Q.QuotaManager(store, Q.TokenSigner("test-secret"))
    lic = L.LicenseService(store, quota.signer, L.LeaseSigner(PEM), clock=clock or Clock())
    quota.subject_resolver = lic.resolve_ai_subject
    return store, quota, lic


class KeyFormatTest(unittest.TestCase):
    def test_keys_are_unique_checked_and_typo_tolerant(self):
        keys = {L.generate_key("EBD-WIN") for _ in range(500)}
        self.assertEqual(len(keys), 500)
        k = next(iter(keys))
        self.assertRegex(k, r"^EBDW-[0-9A-HJKMNP-TV-Z]{5}(-[0-9A-HJKMNP-TV-Z]{5}){3}$")
        self.assertEqual(L.parse_key(k.lower().replace("-", " "))[0], "EBD-WIN")
        broken = k[:-1] + ("0" if k[-1] != "0" else "1")
        with self.assertRaises(L.LicenseError):
            L.parse_key(broken)                                   # check character catches a typo
        self.assertEqual(L.parse_key(L.generate_key("EBD-AND"))[0], "EBD-AND")


class LicenceTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store, self.quota, self.lic = make(self.clock)
        self.key, self.rec = self.lic.create("EBD-WIN", email="Shop@Example.com", name="Teszt Műhely")
        self.pub = L.LeaseSigner(PEM).public_spki_b64()

    def test_key_is_never_stored_in_plain_text(self):
        dump = json.dumps({k: v for k, (v, _) in self.store._d.items()}, default=str)
        self.assertNotIn(L.normalize_key(self.key), dump)
        self.assertNotIn("key_hash", L.public_view(self.rec))

    def test_activation_returns_verifiable_offline_lease(self):
        r = self.lic.activate(self.key, "EBD-WIN", DEV_A, "PC-1", "1.1.0")
        p = L.verify_lease(r["lease"], self.pub)
        self.assertEqual((p["lid"], p["prod"], p["dev"], p["ed"]), (self.rec["lid"], "EBD-WIN", DEV_A, "PRO"))
        self.assertEqual(p["exp"] - p["iat"], 14 * 86400)
        self.assertIsNone(L.verify_lease(r["lease"][:-3] + "AAA", self.pub))                     # tampered signature
        body, sig = r["lease"][6:].split(".")
        forged = json.loads(L.base64.urlsafe_b64decode(body + "==")); forged["dev"] = DEV_B
        self.assertIsNone(L.verify_lease("EBDL1-" + L._b64u(json.dumps(forged).encode()) + "." + sig, self.pub))
        other = L.LeaseSigner(ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        self.assertIsNone(L.verify_lease(other.sign(p), self.pub))                               # signed with another key

    def test_second_device_is_refused_reinstall_on_same_device_is_fine(self):
        self.lic.activate(self.key, "EBD-WIN", DEV_A)
        self.lic.activate(self.key, "EBD-WIN", DEV_A)                       # reinstall / re-activation, same device
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.activate(self.key, "EBD-WIN", DEV_B)
        self.assertEqual((cm.exception.code, cm.exception.status), ("device_limit", 409))

    def test_device_change_is_limited_and_old_device_loses_access(self):
        a = self.lic.activate(self.key, "EBD-WIN", DEV_A)
        b = self.lic.activate(self.key, "EBD-WIN", DEV_B, transfer=True)
        self.assertEqual(b["license"]["transfers_left"], 1)
        with self.assertRaises(L.LicenseError):
            self.lic.refresh(self.key, "EBD-WIN", DEV_A)                    # old device cannot refresh
        self.assertIsNone(self.quota.signer.verify(a["ai_token"]) and self.lic.resolve_ai_subject(self.quota.signer.verify(a["ai_token"])))
        self.lic.activate(self.key, "EBD-WIN", DEV_C, transfer=True)
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.activate(self.key, "EBD-WIN", DEV_A, transfer=True)
        self.assertEqual(cm.exception.code, "transfer_limit")
        self.clock.t += 31 * 86400
        self.lic.activate(self.key, "EBD-WIN", DEV_A, transfer=True)        # allowed again after 30 days

    def test_deactivate_frees_the_licence(self):
        self.lic.activate(self.key, "EBD-WIN", DEV_A)
        with self.assertRaises(L.LicenseError):
            self.lic.deactivate(self.key, DEV_B)                            # cannot deactivate someone else's device
        self.lic.deactivate(self.key, DEV_A)
        self.lic.activate(self.key, "EBD-WIN", DEV_B)

    def test_wrong_product_invalid_key_and_bad_device_id(self):
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.activate(self.key, "EBD-AND", DEV_A)
        self.assertEqual(cm.exception.code, "wrong_product")
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.activate(L.generate_key("EBD-WIN"), "EBD-WIN", DEV_A)   # well-formed but never sold
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(L.LicenseError):
            self.lic.activate(self.key, "EBD-WIN", "not-a-hash")

    def test_revocation_blocks_activation_and_ai_immediately(self):
        r = self.lic.activate(self.key, "EBD-WIN", DEV_A)
        iid = self.quota.signer.verify(r["ai_token"])
        self.assertEqual(self.lic.resolve_ai_subject(iid), "lic-" + self.rec["lid"])
        self.lic.revoke(self.rec["lid"], "refund")
        self.assertIsNone(self.lic.resolve_ai_subject(iid))
        d = self.quota.check_and_consume(r["ai_token"], "1.2.3.4")
        self.assertEqual((d.allowed, d.reason), (False, "license_invalid"))
        with self.assertRaises(L.LicenseError) as cm:
            self.lic.refresh(self.key, "EBD-WIN", DEV_A)
        self.assertEqual(cm.exception.code, "revoked")
        self.lic.reinstate(self.rec["lid"])
        self.lic.refresh(self.key, "EBD-WIN", DEV_A)

    def test_ai_quota_is_per_licence_reinstall_does_not_reset(self):
        t1 = self.lic.activate(self.key, "EBD-WIN", DEV_A)["ai_token"]
        for _ in range(30):
            self.assertTrue(self.quota.check_and_consume(t1, "1.1.1.1").allowed)
        self.clock.t += 5
        t2 = self.lic.activate(self.key, "EBD-WIN", DEV_A)["ai_token"]           # "reinstall" → new token
        self.assertEqual(self.quota.remaining(t2, "1.1.1.1"), 20)
        for _ in range(20):
            self.assertTrue(self.quota.check_and_consume(t2, "2.2.2.2").allowed)
        self.assertEqual(self.quota.check_and_consume(t2, "3.3.3.3").reason, "daily_quota")

    def test_access_modes(self):
        anon = self.quota.check_and_consume(None, "9.9.9.9")
        self.assertTrue(anon.allowed)                                           # default 'open' keeps old Android versions working
        install = self.quota.signer.issue("legacy-install-id-123456")
        os.environ["AI_ACCESS_MODE"] = "license"
        try:
            self.assertEqual(self.quota.check_and_consume(None, "9.9.9.9").reason, "license_required")
            self.assertEqual(self.quota.check_and_consume(install, "9.9.9.9").reason, "license_required")
            lic_tok = self.lic.activate(self.key, "EBD-WIN", DEV_A)["ai_token"]
            self.assertTrue(self.quota.check_and_consume(lic_tok, "9.9.9.9").allowed)
            self.assertIsNone(self.quota.signer.verify(lic_tok[:-2] + "xx"))    # forged token
        finally:
            del os.environ["AI_ACCESS_MODE"]

    def test_reissue_key_invalidates_old_key(self):
        new_key, _ = self.lic.reissue_key(self.rec["lid"])
        with self.assertRaises(L.LicenseError):
            self.lic.activate(self.key, "EBD-WIN", DEV_A)
        self.lic.activate(new_key, "EBD-WIN", DEV_A)

    def test_owner_licence_and_order_idempotency(self):
        k, rec = self.lic.create("EBD-AND", name="Tulajdonos", owner=True)
        p = L.verify_lease(self.lic.activate(k, "EBD-AND", DEV_A)["lease"], self.pub)
        self.assertEqual((p["ed"], p["owner"]), ("OWNER", True))
        k1, r1 = self.lic.create("EBD-WIN", email="a@b.hu", order_id="777")
        k2, r2 = self.lic.create("EBD-WIN", email="a@b.hu", order_id="777")
        self.assertIsNone(k2); self.assertEqual(r1["lid"], r2["lid"])
        self.assertEqual(len(self.lic.search("a@b.hu")), 1)

    def test_lease_not_available_without_server_key(self):
        store = MemoryStore(); q = Q.QuotaManager(store, Q.TokenSigner("x"))
        lic = L.LicenseService(store, q.signer, L.LeaseSigner(""))
        k, _ = lic.create("EBD-WIN")
        with self.assertRaises(L.LicenseError) as cm:
            lic.activate(k, "EBD-WIN", DEV_A)
        self.assertEqual(cm.exception.status, 503)


def ls_event(name, order_id="1001", test_mode=True, variant="111", status="paid", email="vevo@example.com"):
    return json.dumps({"meta": {"event_name": name, "test_mode": test_mode},
                       "data": {"type": "orders", "id": order_id, "attributes": {"status": status, "user_email": email, "user_name": "Vevő",
                                                                                "currency": "EUR", "total": 1999, "first_order_item": {"variant_id": variant, "product_id": 9}}}}).encode()


def sign(raw, secret="whsec"):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class ShopTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"LEMONSQUEEZY_WEBHOOK_SECRET": "whsec", "LS_PRODUCT_MAP": '{"111":"EBD-WIN","222":"EBD-AND"}', "SHOP_MODE": "test",
                           "DOWNLOAD_SECRET": "dl-secret", "DOWNLOAD_URL_EBD_WIN": "https://files.example/ebd-win-setup.exe"})
        self.clock = Clock()
        self.store, self.quota, self.lic = make(self.clock)
        self.mail = M.Mailer(self.store)
        self.shop = S.Shop(self.store, self.lic, self.mail, clock=self.clock)

    def tearDown(self):
        for k in ("LEMONSQUEEZY_WEBHOOK_SECRET", "LS_PRODUCT_MAP", "SHOP_MODE", "DOWNLOAD_SECRET", "DOWNLOAD_URL_EBD_WIN"):
            os.environ.pop(k, None)

    def test_forged_and_unsigned_webhooks_are_rejected(self):
        raw = ls_event("order_created")
        for sig in ("", "00" * 32, sign(raw, "wrong-secret"), sign(raw + b" ")):
            with self.assertRaises(L.LicenseError) as cm:
                self.shop.handle_webhook(raw, sig)
            self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.lic.all(), [])

    def test_paid_test_order_creates_licence_mail_and_stats_once(self):
        raw = ls_event("order_created")
        r = self.shop.handle_webhook(raw, sign(raw))
        self.assertEqual(len(r["licences"]), 1)
        rec = self.lic.get(r["licences"][0])
        self.assertTrue(rec["test"]); self.assertEqual(rec["product"], "EBD-WIN"); self.assertEqual(rec["order_id"], "1001")
        box = self.store.get("mail:outbox")
        self.assertEqual(box[-1]["kind"], "purchase")
        self.assertNotRegex(box[-1]["body_preview"], r"EBDW-[0-9A-Z]{5}-")             # key never logged / previewed
        self.assertTrue(self.shop.handle_webhook(raw, sign(raw))["duplicate"])        # Lemon Squeezy retry
        self.assertEqual(len(self.lic.all()), 1)
        self.assertEqual(self.shop.stats()["months"][0]["EBD-WIN:test"], 1)

    def test_test_mode_ignores_live_orders_and_live_mode_processes_them(self):
        raw = ls_event("order_created", order_id="2002", test_mode=False)
        self.assertIn("ignored", self.shop.handle_webhook(raw, sign(raw)))
        os.environ["SHOP_MODE"] = "live"
        r = self.shop.handle_webhook(raw, sign(raw))
        self.assertFalse(self.lic.get(r["licences"][0])["test"])

    def test_unpaid_or_unknown_product_creates_nothing(self):
        for raw in (ls_event("order_created", status="pending"), ls_event("order_created", order_id="3", variant="999")):
            self.shop.handle_webhook(raw, sign(raw))
        self.assertEqual(self.lic.all(), [])

    def test_refund_revokes_licence_download_and_ai(self):
        raw = ls_event("order_created")
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        tok = self.shop.download_token(lid, "EBD-WIN")
        self.assertEqual(self.shop.resolve_download(tok), "https://files.example/ebd-win-setup.exe")
        ref = ls_event("order_refunded", status="refunded")
        self.assertEqual(self.shop.handle_webhook(ref, sign(ref))["revoked"], [lid])
        with self.assertRaises(L.LicenseError):
            self.shop.resolve_download(tok)

    def test_download_tokens_cannot_be_forged_reused_after_expiry_or_without_payment(self):
        raw = ls_event("order_created")
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        tok = self.shop.download_token(lid, "EBD-WIN")
        for bad in ("", "abc", tok[:-2] + "zz", tok.split(".")[0] + "." + tok.split(".")[0]):
            with self.assertRaises(L.LicenseError):
                self.shop.resolve_download(bad)
        with self.assertRaises(L.LicenseError):                                     # a token for a licence that does not exist
            fake = S.Shop(self.store, self.lic, self.mail, clock=self.clock).download_token("L999999", "EBD-WIN")
            self.shop.resolve_download(fake)
        self.clock.t += 49 * 3600
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.resolve_download(tok)
        self.assertEqual(cm.exception.code, "expired")
        key, _ = self.lic.create("EBD-WIN")
        self.assertIn("/v1/shop/download?t=", self.shop.new_download_link_for_key(key))
        os.environ.pop("DOWNLOAD_URL_EBD_WIN")
        with self.assertRaises(L.LicenseError) as cm:
            self.shop.resolve_download(self.shop.download_token(lid, "EBD-WIN"))
        self.assertEqual(cm.exception.status, 503)


class CostTest(unittest.TestCase):
    def test_monthly_cap_and_one_time_warnings(self):
        os.environ.update({"MONTHLY_COST_LIMIT_USD": "1.0", "DAILY_COST_LIMIT_USD": "5"})
        try:
            store = MemoryStore(); warnings = []
            q = Q.QuotaManager(store, Q.TokenSigner("s"), on_cost_warning=lambda *a: warnings.append(a))
            q.add_cost(0.55, tokens=1000)
            q.add_cost(0.30)
            q.add_cost(0.01)
            self.assertEqual([w[1] for w in warnings if w[0].startswith("month")], [50, 80])
            q.add_cost(0.20)
            self.assertIn(100, [w[1] for w in warnings if w[0].startswith("month")])
            d = q.check_and_consume(None, "1.1.1.1")
            self.assertEqual((d.allowed, d.reason), (False, "cost_limit"))
            u = q.usage()
            self.assertEqual(u["days"][0]["calls"], 4); self.assertEqual(u["days"][0]["tokens"], 1000)
        finally:
            for k in ("MONTHLY_COST_LIMIT_USD", "DAILY_COST_LIMIT_USD"):
                os.environ.pop(k, None)


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main
        main.LICENSES.lease = L.LeaseSigner(PEM)

    def test_license_endpoints_map_errors_to_http(self):
        m = self.main
        key, _ = m.LICENSES.create("EBD-WIN", name="X")
        ok = m.license_activate(m.LicenseActivateRequest(license_key=key, product="EBD-WIN", device_id=DEV_A, device_name="", app_version="1.1.0", transfer=False))
        self.assertTrue(ok["lease"].startswith("EBDL1-"))
        with self.assertRaises(Exception) as cm:
            m.license_activate(m.LicenseActivateRequest(license_key=key, product="EBD-WIN", device_id=DEV_B, device_name="", app_version="", transfer=False))
        self.assertEqual((cm.exception.status_code, cm.exception.headers["X-License-Error"]), (409, "device_limit"))

    def test_webhook_endpoint_rejects_forgery(self):
        os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = "whsec"
        try:
            raw = ls_event("order_created", order_id="55")
            with self.assertRaises(Exception) as cm:
                asyncio.run(self.main.lemonsqueezy_webhook(FakeRequest(body=raw, headers={"x-signature": "bad"})))
            self.assertEqual(cm.exception.status_code, 401)
        finally:
            os.environ.pop("LEMONSQUEEZY_WEBHOOK_SECRET")

    def test_admin_requires_token_and_billing_errors_are_polite(self):
        m = self.main
        with self.assertRaises(Exception) as cm:
            m.admin_stats(x_admin_token="wrong")
        self.assertEqual(cm.exception.status_code, 404)
        os.environ["ADMIN_TOKEN"] = "adm-token-123"
        try:
            s = m.admin_stats(x_admin_token="adm-token-123")
            self.assertIn("licenses", s); self.assertTrue(s["lease_signing_configured"])
            created = m.admin_create(m.AdminCreateRequest(product="EBD-AND", email="", name="Owner", owner=True, test=False, note="t"), x_admin_token="adm-token-123")
            self.assertTrue(created["license_key"].startswith("EBDA-"))
            self.assertNotIn("key_hash", created["license"])
            self.assertIn("<title>", m.admin_page().content)
        finally:
            os.environ.pop("ADMIN_TOKEN")
        err = m.provider_http_error(m.gemini_client.ProviderError("billing", 403) if hasattr(m.gemini_client, "ProviderError") else None)
        self.assertEqual((err.status_code, err.headers["X-Quota-Reason"]), (503, "provider_billing"))

    def test_cached_answer_is_not_served_without_licence(self):
        # regression (found in local E2E): the answer cache was checked BEFORE the licence gate
        m = self.main
        os.environ["AI_ACCESS_MODE"] = "license"
        try:
            for token in (None, "forged-token"):
                with self.assertRaises(Exception) as cm:
                    m.run_text("chat", FakeRequest(), token, [{"text": "x"}], "fast", 10, cache_key="any")
                self.assertEqual((cm.exception.status_code, cm.exception.headers["X-Quota-Reason"]), (401, "license_required"))
                with self.assertRaises(Exception) as cm:
                    m.run_stream("chat_stream", FakeRequest(), token, [{"text": "x"}], "fast", 10, cache_key="any")
                self.assertEqual(cm.exception.status_code, 401)
        finally:
            os.environ.pop("AI_ACCESS_MODE")
    def test_update_manifest_is_served(self):
        r = self.main.app_update("EBD-WIN")
        self.assertIn("signature", r)
        with self.assertRaises(Exception):
            self.main.app_update("nope")


if __name__ == "__main__":
    unittest.main()
