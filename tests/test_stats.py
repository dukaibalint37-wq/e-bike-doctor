"""3.5: permanent download / trial / sales statistics, password-only statistics page."""
import calendar, json, os, re, unittest

from tests_path import *  # noqa
from fakes import FakeRequest
import admin_auth as A
import licensing as L
import stats as ST
from store import MemoryStore
from test_trial_promo import TrialBase, DEV_A, DEV_B
from test_stripe33 import session

H = 3600


def ts(y, m, d, hh=12, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0))


class CalendarTest(unittest.TestCase):
    def test_budapest_offsets_and_dst_switches(self):
        self.assertEqual(ST.bp_offset(ts(2026, 1, 15)), 3600)
        self.assertEqual(ST.bp_offset(ts(2026, 7, 15)), 7200)
        # 2026: summer time 29 March 01:00 UTC - 25 October 01:00 UTC
        self.assertEqual(ST.bp_offset(ts(2026, 3, 29, 0, 59)), 3600)
        self.assertEqual(ST.bp_offset(ts(2026, 3, 29, 1, 0)), 7200)
        self.assertEqual(ST.bp_offset(ts(2026, 10, 25, 0, 59)), 7200)
        self.assertEqual(ST.bp_offset(ts(2026, 10, 25, 1, 0)), 3600)

    def test_local_day_boundaries(self):
        self.assertEqual(ST.local_date(ts(2026, 11, 22, 22, 59)), "2026-11-22")      # 23:59 Budapest
        self.assertEqual(ST.local_date(ts(2026, 11, 22, 23, 0)), "2026-11-23")       # 00:00 Budapest
        self.assertEqual(ST.local_date(ts(2026, 7, 1, 21, 59)), "2026-07-01")
        self.assertEqual(ST.local_date(ts(2026, 7, 1, 22, 0)), "2026-07-02")
        self.assertEqual(ST.day_start("2026-11-23"), ts(2026, 11, 22, 23, 0))
        self.assertEqual(ST.day_start("2026-07-02"), ts(2026, 7, 1, 22, 0))
        self.assertEqual(ST.week_start("2026-11-22"), "2026-11-16")                  # Sunday -> Monday
        self.assertEqual(ST.add_days("2026-12-31", 1), "2027-01-01")
        self.assertTrue(ST.valid_date("2026-02-28")); self.assertFalse(ST.valid_date("2026-02-30")); self.assertFalse(ST.valid_date("x"))


class FlakyStore(MemoryStore):
    def __init__(self):
        super().__init__(); self.down = False

    def push(self, key, value, ttl):
        if self.down:
            raise TimeoutError("upstash down")
        return super().push(key, value, ttl)


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.t = [ts(2026, 11, 22, 10)]
        self.store = FlakyStore()
        self.st = ST.Stats(self.store, clock=lambda: self.t[0])
        self.st.start()

    def test_outage_keeps_events_and_flushes_later(self):
        self.store.down = True
        self.st.record("trq", p="EBD-AND", new=True)                 # never raises
        self.assertEqual(len(self.st.pending), 1)
        self.store.down = False
        self.st.record("trq", p="EBD-WIN", new=True)
        self.assertEqual(len(self.st.pending), 0)
        r = self.st.report("2026-11-22", "2026-11-22")
        self.assertEqual(r["periods"]["today"]["metrics"]["trials"]["requested"], 2)

    def test_downloads_unique_repeat_new_bots_failures(self):
        for lid in ("L000001", "L000001", "L000002"):
            self.st.download(True, "EBD-WIN", lid, user_agent="Mozilla/5.0 (Windows NT 10.0)")
        self.st.download(True, "EBD-AND", "L000003", user_agent="")                     # app (no user agent)
        self.st.download(True, "EBD-WIN", "L000004", user_agent="Mozilla/5.0 (compatible; SafeLinks scanner)")   # mail scanner
        self.st.download(False, "EBD-WIN", "L000001", reason="expired", user_agent="Mozilla/5.0")
        self.st.download(False, "", "", reason="bad_token", user_agent="Mozilla/5.0")
        self.t[0] += 86_400                                                               # next day: L000001 again = not new
        self.st.download(True, "EBD-WIN", "L000001", user_agent="Mozilla/5.0")
        d = self.st.report("2026-11-22", "2026-11-23")["periods"]["range"]["metrics"]["downloads"]
        self.assertEqual(d["started"], {"EBD-WIN": 4, "EBD-AND": 1})
        self.assertEqual(d["unique"], {"EBD-WIN": 2, "EBD-AND": 1})
        self.assertEqual(d["repeat"], {"EBD-WIN": 2, "EBD-AND": 0})
        self.assertEqual(d["new"], {"EBD-WIN": 2, "EBD-AND": 1})
        self.assertEqual(d["bots"], 1)                                                    # scanner: never a download
        self.assertEqual(d["opened"]["EBD-WIN"], 5); self.assertEqual(d["opened_unknown"], 1)
        self.assertEqual(d["failed_by_reason"], {"expired": 1, "bad_token": 1})
        day1 = self.st.report("2026-11-22", "2026-11-22")["periods"]["range"]["metrics"]["downloads"]
        self.assertEqual(day1["started"]["EBD-WIN"], 3)

    def test_test_and_owner_data_hidden_by_default(self):
        self.st.download(True, "EBD-WIN", "L000009", test=True)
        self.st.download(True, "EBD-WIN", "L000002", owner=True)
        self.st.download(True, "EBD-WIN", "L000003")
        r = self.st.report("2026-11-22", "2026-11-22")
        self.assertEqual(r["periods"]["range"]["metrics"]["downloads"]["started"]["EBD-WIN"], 1)
        r = self.st.report("2026-11-22", "2026-11-22", include_test=True, include_owner=True)
        self.assertEqual(r["periods"]["range"]["metrics"]["downloads"]["started"]["EBD-WIN"], 3)

    def test_periods_series_and_persistence_across_restart(self):
        self.store = FlakyStore()
        self.t[0] = ts(2026, 10, 3, 9)                                              # statistics running since October
        self.st = ST.Stats(self.store, clock=lambda: self.t[0]); self.st.start()
        self.st.download(True, "EBD-WIN", "L000002")
        self.t[0] = ts(2026, 11, 22, 10)
        self.st.download(True, "EBD-AND", "L000001")
        self.t[0] = ts(2026, 11, 22, 18)
        fresh = ST.Stats(self.store, clock=lambda: self.t[0])                     # "restart": new process, same store
        r = fresh.report("2026-11-01", "2026-11-22")
        P = r["periods"]
        self.assertEqual(sum(P["today"]["metrics"]["downloads"]["started"].values()), 1)
        self.assertEqual(sum(P["month"]["metrics"]["downloads"]["started"].values()), 1)
        self.assertEqual(sum(P["year"]["metrics"]["downloads"]["started"].values()), 2)
        self.assertEqual(sum(P["all"]["metrics"]["downloads"]["started"].values()), 2)
        self.assertEqual(P["week"]["from"], "2026-11-16")
        self.assertEqual(len(r["series"]["daily"]), 22)
        self.assertEqual(len(r["series"]["weekly"]), 26); self.assertEqual(len(r["series"]["monthly"]), 24)
        self.assertEqual(r["series"]["monthly"][-1]["key"], "2026-11"); self.assertEqual(r["series"]["monthly"][-2]["dl_win"], 1)
        self.assertEqual(r["series"]["daily"][-1]["dl_and"], 1)


class FlowTest(TrialBase):
    """Real shop / licence / trial code with the statistics attached (as in main.py)."""
    def setUp(self):
        super().setUp()
        self.st = ST.Stats(self.store, clock=self.clock)
        self.st.start()
        self.lic.stats_sink = self.shop.stats_sink = self.trial.stats_sink = self.st
        os.environ["DOWNLOAD_URL_EBD_AND"] = "https://files.example/app.apk"
        self.day = ST.local_date(self.clock())

    def tearDown(self):
        os.environ.pop("DOWNLOAD_URL_EBD_AND", None)
        super().tearDown()

    def metrics(self, test=True):
        return self.st.report(self.day, self.day, include_test=test)["periods"]["range"]["metrics"]

    def test_trial_request_issue_activation_and_download(self):
        key = self.issue("anna@example.com", "android")
        self.trial.request("anna@example.com", "android", "hu", "", True, "net-x")    # again: existing trial, not a new one
        rec = self.lic.find_by_key(key)
        url, r2 = self.shop.resolve_download_rec(self.shop.download_token(rec["lid"], "EBD-AND"))
        self.st.download(True, r2["product"], r2["lid"], test=bool(r2.get("test")))
        self.lic.activate(key, "EBD-AND", DEV_A)
        self.lic.refresh(key, "EBD-AND", DEV_A)                                        # a refresh is not a new activation
        m = self.metrics()
        self.assertEqual(m["trials"]["requested"], 2); self.assertEqual(m["trials"]["requested_new"], 1)
        self.assertEqual(m["trials"]["issued"]["EBD-AND"], 1)
        self.assertEqual(m["trials"]["activated"]["EBD-AND"], 1)
        self.assertEqual(m["activations"]["by_edition"]["EBD-AND"]["TRIAL"], 1)
        self.assertEqual(m["downloads"]["started"]["EBD-AND"], 1)
        self.assertEqual(self.metrics(test=False)["trials"]["issued"]["EBD-AND"], 0)  # SHOP_MODE=test -> test data

    def test_checkout_sale_with_promo_refund_and_activation(self):
        self.shop.stripe_checkout("android", "hu", True, promo="EBIKE-50")
        s = session("cs_test_p1", product="EBD-AND", amount=500)
        s["amount_total"], s["total_details"]["amount_discount"], s["metadata"]["promo"] = 250, 250, "EBIKE-50"
        self.stripe.sessions["cs_test_p1"] = s
        self.deliver(s)
        self.deliver(s, etype="checkout.session.async_payment_succeeded", ev_id="evt_2")    # duplicate delivery: one sale
        key = re.search(r"(EBDA-[0-9A-Z-]{23})", self.licence_mails()[-1]["payload"]["text"]).group(1)
        self.lic.activate(key, "EBD-AND", DEV_A)
        self.lic.activate(key, "EBD-AND", DEV_B, transfer=True)                          # device change
        sa = self.metrics()["sales"]
        self.assertEqual(sa["checkouts"]["EBD-AND"], 1); self.assertEqual(sa["checkouts_promo"], 1)
        self.assertEqual(sa["count"]["EBD-AND"], 1); self.assertEqual(sa["promo_count"], 1)
        self.assertEqual(sa["revenue"], {"USD": 250}); self.assertEqual(sa["discount"], {"USD": 250})
        ac = self.metrics()["activations"]
        self.assertEqual(ac["first"]["EBD-AND"], 1); self.assertEqual(ac["by_edition"]["EBD-AND"]["PRO"], 2); self.assertEqual(ac["transfers"], 1)
        self.shop._order_refunded("cs_test_p1", {"refunded_amount": 250, "status": "refunded", "total": 250, "currency": "USD"}, True)
        sa = self.metrics()["sales"]
        self.assertEqual(sa["refunds"], {"USD": 250}); self.assertEqual(sa["net"], {"USD": 0}); self.assertEqual(sa["refund_count"], 1)

    def test_statistics_failure_never_breaks_a_purchase(self):
        class Broken:
            def record(self, *a, **k): raise RuntimeError("stats down")
            def activation(self, *a, **k): raise RuntimeError("stats down")
        self.shop.stats_sink = Broken()
        self.stripe.sessions["cs_test_b"] = session("cs_test_b")
        r = self.deliver({"id": "cs_test_b"})
        self.assertEqual(len(r["licences"]), 1)


class BackfillTest(TrialBase):
    def test_history_before_the_start_is_added_once_and_flagged(self):
        # before the statistics existed: a paid order, a trial (issued + activated) and download counters
        self.stripe.sessions["cs_test_old"] = session("cs_test_old", product="EBD-WIN")
        self.deliver({"id": "cs_test_old"})
        key = self.issue("old@example.com", "android")
        self.lic.activate(key, "EBD-AND", DEV_A)
        day = __import__("time").strftime("%Y%m%d", __import__("time").gmtime(self.clock()))
        self.store.incr(f"shop:dlday:{day}:EBD-WIN:test", 400 * 86_400, 3)
        self.clock.t += 2 * H
        st = ST.Stats(self.store, clock=self.clock)
        st.start()
        st.download(True, "EBD-WIN", "L000001", test=True)                           # measured after the start
        self.store.incr(f"shop:dlday:{day}:EBD-WIN:test", 400 * 86_400)               # (the shop counts it as well)
        info = st.backfill(self.lic, self.shop)
        self.assertGreater(info["events"], 0)
        self.assertEqual(st.backfill(self.lic, self.shop), info)                      # once only
        d = ST.local_date(self.clock())
        m = st.report(d, d, include_test=True)["periods"]["range"]["metrics"]
        self.assertEqual(m["downloads"]["legacy_started"]["EBD-WIN"], 3)              # 4 counted - 1 measured
        self.assertEqual(m["downloads"]["started"]["EBD-WIN"], 1)
        self.assertEqual(m["sales"]["count"]["EBD-WIN"], 1); self.assertEqual(m["sales"]["revenue"], {"USD": 500})
        self.assertEqual(m["trials"]["issued"]["EBD-AND"], 1); self.assertEqual(m["trials"]["activated"]["EBD-AND"], 1)
        # a re-activation after the start is not a "first" one
        self.lic.stats_sink = st
        self.lic.deactivate(key, DEV_A); self.lic.activate(key, "EBD-AND", DEV_A)
        m = st.report(d, d, include_test=True)["periods"]["range"]["metrics"]
        self.assertEqual(m["activations"]["first"]["EBD-AND"], 1)


class StatsAuthTest(unittest.TestCase):
    def setUp(self):
        os.environ.update({"STATS_PASSWORD_HASH": A.hash_password("titkos-jelszo", iterations=1000), "STATS_SESSION_SECRET": "s"})
        self.t = [1_800_000_000]
        self.auth = ST.StatsAuth(MemoryStore(), clock=lambda: self.t[0])

    def tearDown(self):
        for k in ("STATS_PASSWORD_HASH", "STATS_SESSION_SECRET", "STATS_SESSION_HOURS"):
            os.environ.pop(k, None)

    def test_password_only_login_session_and_logout(self):
        with self.assertRaises(L.LicenseError):
            self.auth.login("rossz", "ip1")
        cookie, exp = self.auth.login("titkos-jelszo", "ip1")                  # no second factor
        self.assertTrue(self.auth.valid(cookie))
        self.assertFalse(self.auth.valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")))
        self.auth.logout(cookie)
        self.assertFalse(self.auth.valid(cookie))
        cookie, _ = self.auth.login("titkos-jelszo", "ip1")
        self.t[0] += 13 * H
        self.assertFalse(self.auth.valid(cookie))                             # 12 h session

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            with self.assertRaises(L.LicenseError):
                self.auth.login("rossz", "ip9")
        with self.assertRaises(L.LicenseError) as cm:
            self.auth.login("titkos-jelszo", "ip9")
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(self.auth.login("titkos-jelszo", "ip2")[0])           # other network unaffected
        self.t[0] += 1800
        self.assertTrue(self.auth.login("titkos-jelszo", "ip9")[0])

    def test_not_configured(self):
        os.environ.pop("STATS_PASSWORD_HASH")
        with self.assertRaises(L.LicenseError) as cm:
            self.auth.login("x", "ip")
        self.assertEqual(cm.exception.status, 503)


class StatsEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.m = main

    def setUp(self):
        os.environ.update({"ADMIN_TOKEN": "adm-35", "STATS_PASSWORD_HASH": A.hash_password("pw35", iterations=1000), "STATS_SESSION_SECRET": "x"})

    def tearDown(self):
        for k in ("ADMIN_TOKEN", "STATS_PASSWORD_HASH", "STATS_SESSION_SECRET"):
            os.environ.pop(k, None)

    def req(self, cookie=None):
        r = FakeRequest()
        r.cookies = {ST.COOKIE: cookie} if cookie else {}
        r.query_params = {}
        return r

    def test_report_requires_login_and_works_with_password_session(self):
        with self.assertRaises(Exception) as cm:
            self.m.stats_report(request=self.req())
        self.assertEqual(cm.exception.status_code, 404)
        r = self.m.stats_login(self.m.StatsLoginRequest(password="pw35"), FakeRequest())
        cookie = re.search(ST.COOKIE + r"=([^;]+)", r.headers["Set-Cookie"]).group(1)
        self.assertIn("HttpOnly", r.headers["Set-Cookie"]); self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
        d = self.m.stats_report(request=self.req(cookie))
        for k in ("periods", "series", "backfill", "stats_start"):
            self.assertIn(k, d)
        self.assertEqual(set(d["periods"]), {"today", "week", "month", "year", "all", "range"})
        self.assertTrue(self.m.stats_session(self.req(cookie))["ok"])
        self.assertNotIn("pw35", json.dumps(d))
        self.assertTrue(self.m.stats_report(request=None, x_admin_token="adm-35"))   # the 2FA admin token works too

    def test_download_endpoint_records_success_and_failure(self):
        m = self.m
        import time as _t
        span = (int(_t.time()) - 3 * 86_400, int(_t.time()) + 3 * 86_400)
        before = len(m.STATS.events(*span))
        with self.assertRaises(Exception):
            m.shop_download("garbage", request=FakeRequest(headers={"user-agent": "Mozilla/5.0"}))
        evs = m.STATS.events(*span)[before:]
        self.assertEqual([(e["k"], e.get("ok", 0), e.get("r")) for e in evs], [("dl", 0, "bad_token")])

    def test_page_has_strict_csp(self):
        p = self.m.stats_page()
        self.assertIn("'sha256-", p.headers["Content-Security-Policy"]); self.assertIn("noindex", p.headers["X-Robots-Tag"])
        self.assertIn("Statisztika", p.content); self.assertIn('lang="hu"', p.content)


if __name__ == "__main__":
    unittest.main()
