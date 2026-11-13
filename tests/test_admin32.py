"""3.2: owner admin with 2FA, dashboard, cookieless visit stats, test/live data separation, Gmail API mail provider."""
import json, os, re, unittest

from tests_path import *  # noqa
from fakes import install_web_stubs, FakeRequest

install_web_stubs()
import admin_auth as A
import analytics as AN
import licensing as L
import mailer as M
import s3presign
from store import MemoryStore
from test_shop31 import Base, CapturingMailer, ls_event, sign

SECRET = "JBSWY3DPEHPK3PXP"


class Clock:
    def __init__(self, t=1_800_000_000.0): self.t = t
    def __call__(self): return self.t


class AdminAuthTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"ADMIN_PASSWORD_HASH": A.hash_password("correct horse", iterations=1000), "ADMIN_TOTP_SECRET": SECRET,
                           "ADMIN_SESSION_SECRET": "sess-secret", "ADMIN_TOKEN": "machine-token"})
        os.environ.pop("ADMIN_HEADER_TOKEN", None)
        self.clock = Clock()
        self.auth = A.AdminAuth(MemoryStore(), clock=self.clock)

    def tearDown(self):
        for k in ("ADMIN_PASSWORD_HASH", "ADMIN_TOTP_SECRET", "ADMIN_SESSION_SECRET", "ADMIN_TOKEN", "ADMIN_HEADER_TOKEN"):
            os.environ.pop(k, None)

    def code(self, dt=0):
        return A.totp(SECRET, int(self.clock() + dt) // 30)

    def test_rfc6238_reference_vector(self):
        # RFC 6238 Appendix B, SHA-1, T=59 -> 94287082 (8 digits); secret "12345678901234567890"
        import base64
        self.assertEqual(A.totp(base64.b32encode(b"12345678901234567890").decode(), 59 // 30, digits=8), "94287082")

    def test_login_needs_password_and_fresh_code_and_session_works(self):
        with self.assertRaises(L.LicenseError):
            self.auth.login("wrong", self.code(), "ip1")
        with self.assertRaises(L.LicenseError):
            self.auth.login("correct horse", "000000" if self.code() != "000000" else "111111", "ip1")
        cookie, exp = self.auth.login("correct horse", self.code(), "ip1")
        self.assertTrue(self.auth.session_valid(cookie))
        self.assertTrue(self.auth.allowed(None, cookie, "GET"))
        self.assertFalse(self.auth.allowed(None, cookie, "POST", ""))                 # CSRF header required for changes
        self.assertTrue(self.auth.allowed(None, cookie, "POST", "ebd-admin"))
        self.assertFalse(self.auth.session_valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")))   # tampered
        with self.assertRaises(L.LicenseError):                                          # replay of the same code
            self.auth.login("correct horse", self.code(), "ip2")
        self.clock.t += 9 * 3600
        self.assertFalse(self.auth.session_valid(cookie))                                # expired (8 h)

    def test_logout_ends_session_server_side(self):
        cookie, _ = self.auth.login("correct horse", self.code(), "ip1")
        self.auth.logout(cookie, "ip1")
        self.assertFalse(self.auth.session_valid(cookie))

    def test_bruteforce_lockout(self):
        for _ in range(5):
            with self.assertRaises(L.LicenseError):
                self.auth.login("bad", "123456", "ip9")
        with self.assertRaises(L.LicenseError) as cm:
            self.auth.login("correct horse", self.code(), "ip9")                        # correct, but locked
        self.assertEqual(cm.exception.status, 429)
        self.clock.t += 1800
        self.assertTrue(self.auth.login("correct horse", self.code(), "ip9")[0])
        self.assertIn("login_failed", [a["a"] for a in self.auth.audit_log()])

    def test_header_token_can_be_disabled(self):
        self.assertTrue(self.auth.allowed("machine-token", None))
        self.assertFalse(self.auth.allowed("wrong", None))
        os.environ["ADMIN_HEADER_TOKEN"] = "0"
        self.assertFalse(self.auth.allowed("machine-token", None))

    def test_not_configured(self):
        os.environ.pop("ADMIN_TOTP_SECRET")
        with self.assertRaises(L.LicenseError) as cm:
            self.auth.login("correct horse", "123456", "ip")
        self.assertEqual(cm.exception.status, 503)


class VisitsTest(unittest.TestCase):
    def test_aggregates_only_and_filters_bots(self):
        st = MemoryStore(); v = AN.Visits(st)
        ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/130"
        self.assertTrue(v.hit("/hu/hibakodok/bafang/21/?x=1", "www.google.com", ua, "k1"))
        v.hit("/de/shop/", "", ua, "k2"); v.hit("/en/", "ebikedoctorpro.pages.dev", ua, "k3", own_host="ebikedoctorpro.pages.dev")
        self.assertFalse(v.hit("/hu/", "", "Googlebot/2.1", "k4"))
        self.assertFalse(v.hit("/hu/", "", "", "k5"))
        v.hit("/<script>", "evil<>", ua, "k6")
        r = v.report(30)
        self.assertEqual(r["total_30d"], 4)
        d = r["days"][0]
        self.assertEqual((d["sections"]["codes"], d["sections"]["shop"], d["sections"]["home"], d["langs"]["hu"]), (1, 1, 2, 1))
        self.assertEqual([p["k"] for p in r["top_referrers"]], ["www.google.com"])        # own host and junk ignored
        self.assertIn("/hu/hibakodok/bafang/21/", [p["k"] for p in r["top_pages"]])        # query string dropped
        self.assertNotIn("/<script>", json.dumps(r))
        dump = json.dumps(st._d, default=str)
        self.assertNotIn("k1", dump); self.assertNotIn("Mozilla", dump)                   # no client key / UA stored


class SeparationTest(Base):
    def test_live_shop_ignores_test_orders_and_stats_are_separated(self):
        raw = ls_event(order_id="t1")
        lid_test = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        os.environ["SHOP_MODE"] = "live"
        raw2 = ls_event(order_id="t2")
        self.assertIn("test order while SHOP_MODE=live", self.shop.handle_webhook(raw2, sign(raw2))["ignored"])
        raw3 = ls_event(order_id="l1", test_mode=False, variant="222")
        lid_live = self.shop.handle_webhook(raw3, sign(raw3))["licences"][0]
        self.assertEqual(self.lic.stats("live")["EBD-AND"]["total"], 1); self.assertEqual(self.lic.stats("live")["EBD-WIN"]["total"], 0)
        self.assertEqual(self.lic.stats("test")["EBD-WIN"]["total"], 1)
        self.assertEqual([o["order"] for o in self.shop.orders("live")], ["l1"])
        self.assertEqual([o["order"] for o in self.shop.orders("test")], ["t1"])
        m = self.shop.stats()["months"][0]
        self.assertEqual((m["EBD-WIN:test"], m["EBD-AND:live"]), (1, 1))
        self.assertEqual(m["test_revenue_EUR"], 19.99)
        r = self.shop.purge_test_data()
        self.assertEqual(r["purged_licences"], [lid_test])
        self.assertEqual(self.lic.get(lid_test)["status"], "revoked")
        self.assertEqual(self.lic.get(lid_live)["status"], "active")                    # live data untouched
        self.assertEqual(self.shop.orders("test"), []); self.assertEqual(len(self.shop.orders("live")), 1)
        m = self.shop.stats()["months"][0]
        self.assertEqual((m["EBD-WIN:test"], m["EBD-AND:live"]), (0, 1)); self.assertNotIn("test_revenue_EUR", m)
        self.assertEqual(self.lic.stats("test")["EBD-WIN"]["total"], 0); self.assertEqual(self.lic.stats()["EBD-WIN"]["purged"], 1)

    def test_download_counters_by_mode_and_refund_marks_order(self):
        raw = ls_event(order_id="d1")
        lid = self.shop.handle_webhook(raw, sign(raw))["licences"][0]
        self.shop.resolve_download(self.shop.download_token(lid, "EBD-WIN"))
        row = self.shop.downloads_daily(1)[0]
        self.assertEqual((row["EBD-WIN:test"], row["EBD-WIN:live"]), (1, 0))
        ref = ls_event("order_refunded", order_id="d1", status="refunded")
        self.shop.handle_webhook(ref, sign(ref))
        self.assertTrue(self.shop.orders("test")[0]["refunded"])
        self.assertEqual(self.shop.stats()["months"][0]["refunds_test"], 1)


class GmailProviderTest(Base):
    def test_gmail_api_flow(self):
        for k in ("RESEND_API_KEY",):
            os.environ.pop(k, None)
        os.environ.update({"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "csec", "GMAIL_REFRESH_TOKEN": "rt", "MAIL_FROM": "E-Bike Doctor PRO <owner@gmail.com>"})
        M.Mailer._gmail_token = (None, 0.0)
        calls = []

        class G(M.Mailer):
            def _http_post(self, url, headers, payload, form=False):
                calls.append((url, headers, payload, form))
                if url == M.GOOGLE_TOKEN_URL:
                    return 200, json.dumps({"access_token": "at-1", "expires_in": 3600})
                return 200, '{"id":"m1"}'
        try:
            g = G(self.store)
            self.assertEqual((g.provider, g.configured), ("gmail", True))
            e = g.send("buyer@example.com", "Tárgy ő", "szöveg EBDW-AAAAA-BBBBB-CCCCC-DDDDD", kind="purchase", html="<b>x</b>")
            self.assertTrue(e["sent"])
            self.assertEqual(calls[0][0], M.GOOGLE_TOKEN_URL); self.assertTrue(calls[0][3]); self.assertEqual(calls[0][2]["grant_type"], "refresh_token")
            self.assertEqual(calls[1][0], M.GMAIL_SEND_URL); self.assertEqual(calls[1][1]["Authorization"], "Bearer at-1")
            import base64, email
            msg = email.message_from_bytes(base64.urlsafe_b64decode(calls[1][2]["raw"]))
            self.assertEqual(msg["To"], "buyer@example.com"); self.assertIn("owner@gmail.com", msg["From"])
            g.send("b2@example.com", "s", "t")
            self.assertEqual(len([c for c in calls if c[0] == M.GOOGLE_TOKEN_URL]), 1)   # access token cached
        finally:
            for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
                os.environ.pop(k, None)
            M.Mailer._gmail_token = (None, 0.0)


class SigV4HeaderTest(unittest.TestCase):
    def test_aws_reference_vector_get_object(self):
        import datetime
        h = s3presign.sign_headers("GET", "examplebucket.s3.amazonaws.com", "/test.txt", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                   "us-east-1", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                                   datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc), {"Range": "bytes=0-9"})
        self.assertTrue(h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))


class AdminEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.m = main

    def setUp(self):
        os.environ.update({"ADMIN_TOKEN": "adm-32", "ADMIN_PASSWORD_HASH": A.hash_password("pw", iterations=1000), "ADMIN_TOTP_SECRET": SECRET})

    def tearDown(self):
        for k in ("ADMIN_TOKEN", "ADMIN_PASSWORD_HASH", "ADMIN_TOTP_SECRET"):
            os.environ.pop(k, None)

    def test_dashboard_requires_auth_and_has_all_sections(self):
        with self.assertRaises(Exception) as cm:
            self.m.admin_dashboard("live", x_admin_token=None, request=FakeRequest())
        self.assertEqual(cm.exception.status_code, 404)
        d = self.m.admin_dashboard("live", x_admin_token="adm-32", request=None)
        for k in ("licenses", "shop", "orders", "downloads_daily", "visits", "ai", "interest", "config", "outbox", "audit"):
            self.assertIn(k, d)
        self.assertIn("month_cost_usd", d["ai"]); self.assertTrue(d["config"]["admin_2fa"])
        self.assertNotIn("adm-32", json.dumps(d, default=str))

    def test_purge_needs_confirmation_text(self):
        with self.assertRaises(Exception) as cm:
            self.m.admin_purge_test(self.m.PurgeRequest(confirm="igen"), x_admin_token="adm-32", request=None)
        self.assertEqual(cm.exception.status_code, 422)

    def test_admin_page_csp_uses_script_hash(self):
        p = self.m.admin_page()
        csp = p.headers["Content-Security-Policy"]
        self.assertIn("'sha256-", csp); self.assertNotIn("unsafe-inline' ;", csp); self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("noindex", p.headers["X-Robots-Tag"])


if __name__ == "__main__":
    unittest.main()
