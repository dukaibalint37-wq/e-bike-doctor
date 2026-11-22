"""3.6: purchase funnel checked against Stripe data (paid / proven failure / abandoned / open, own tests apart)."""
import calendar, json, os, unittest

from tests_path import *  # noqa
from fakes import FakeRequest
import funnel as F
import stats as ST
from store import MemoryStore

NOW = calendar.timegm((2026, 11, 22, 12, 0, 0))


class FakeStripe:
    configured = True

    def __init__(self, sessions=(), charges=(), events=()):
        self.sessions, self.charges, self.events, self.calls = list(sessions), list(charges), list(events), []

    def request(self, method, path, params=None):
        self.calls.append(path)
        if path == "/checkout/sessions":
            return {"data": self.sessions, "has_more": False}
        if path == "/charges":
            return {"data": self.charges, "has_more": False}
        if path == "/events":
            return {"data": [e for e in self.events if e["type"] == params.get("type")], "has_more": False}
        raise AssertionError(path)


def sess(sid, product="EBD-AND", t=NOW - 3600, status="expired", paid=False, discount=0, pi=None, promo="", live=True, email=None, expires=None):
    return {"id": sid, "created": t, "status": "complete" if paid else status, "payment_status": "paid" if paid else "unpaid", "livemode": live,
            "amount_total": 500 - discount, "currency": "usd", "total_details": {"amount_discount": discount}, "expires_at": expires or t + 86400,
            "payment_intent": pi, "customer_details": {"email": email} if email else None,
            "metadata": {"shop": "ebd-website", "product": product, "lang": "hu", **({"promo": promo} if promo else {})}}


class FunnelTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.st = ST.Stats(self.store, clock=lambda: NOW)
        self.st.start()

    def funnel(self, stripe, mode="live"):
        return F.Funnel(stripe, self.st, lambda: mode, clock=lambda: NOW)

    def test_outcomes_reasons_coupon_and_products(self):
        stripe = FakeStripe(
            sessions=[sess("cs_live_paid0000001", paid=True, discount=250, promo="EBIKE-50"),
                      sess("cs_live_paid0000002", product="EBD-WIN", paid=True),
                      sess("cs_live_fail0000001", pi={"id": "pi_1", "status": "requires_payment_method",
                                                      "last_payment_error": {"code": "card_declined", "decline_code": "insufficient_funds"}}),
                      sess("cs_live_aban0000001", email="x@example.com"),
                      sess("cs_live_open0000001", status="open", t=NOW - 600),
                      sess("cs_live_late0000001", status="open", t=NOW - 7200, expires=NOW - 60),
                      sess("cs_test_other000001", live=False),
                      {**sess("cs_live_foreign0001"), "metadata": {"shop": "other"}}],
            charges=[{"payment_intent": "pi_1", "status": "failed", "failure_code": "card_declined", "outcome": {"reason": "insufficient_funds"}},
                     {"payment_intent": "pi_1", "status": "failed", "failure_code": "incorrect_cvc", "outcome": {"reason": "incorrect_cvc"}}])
        r = self.funnel(stripe).report("2026-11-22", "2026-11-22")
        m = r["periods"]["range"]["metrics"]
        self.assertEqual(m["reached"], {"EBD-WIN": 1, "EBD-AND": 5})
        self.assertEqual(m["paid"], {"EBD-WIN": 1, "EBD-AND": 1})
        self.assertEqual(m["coupon_paid"]["EBD-AND"], 1)
        self.assertEqual(m["failed"]["EBD-AND"], 1)
        self.assertEqual(m["attempts_failed"], 2)
        self.assertEqual(m["reasons"], {"insufficient_funds": 1, "incorrect_cvc": 1})
        self.assertEqual(m["abandoned"]["EBD-AND"], 2)                       # expired + open past its 24 h
        self.assertEqual(m["email_entered_abandoned"], 1)
        self.assertEqual(m["open"]["EBD-AND"], 1)
        self.assertEqual(m["paid_amount"], {"USD": 750})
        self.assertEqual(len(r["sessions"]), 6)                               # test-mode and foreign sessions never listed
        self.assertEqual(r["series"]["daily"][-1]["paid"], 2)

    def test_own_tests_kept_apart(self):
        self.store.set("st:fn:class:cs_live_hist00000001", "own_review", 100)
        self.store.set("st:fn:class:cs_live_hist00000002", "external_unverified", 100)
        self.st.record("bs", ok=1, p="EBD-AND", sid="cs_live_tool00000001", own=True, vh="v1")
        stripe = FakeStripe(
            sessions=[sess("cs_live_tool00000001"), sess("cs_live_apix00000001"), sess("cs_live_hist00000001"), sess("cs_live_hist00000002"),
                      sess("cs_live_real00000001")],
            events=[{"type": "checkout.session.expired", "data": {"object": {"id": "cs_live_apix00000001"}}, "request": {"id": "req_1"}},
                    {"type": "checkout.session.expired", "data": {"object": {"id": "cs_live_real00000001"}}, "request": {"id": None}}])
        f = self.funnel(stripe)
        m = f.report("2026-11-22", "2026-11-22")["periods"]["range"]["metrics"]
        self.assertEqual(m["own"]["sessions"], 3)
        self.assertEqual(m["own"]["by_reason"], {"own_tool": 1, "api_expired": 1, "review": 1})
        self.assertEqual(m["abandoned"]["EBD-AND"], 2)                       # history 'external' + the real one
        self.assertEqual(m["external_unverified"], 1)
        self.assertEqual(m["own"]["starts"], 1); self.assertEqual(sum(m["start_requests"].values()), 0)
        self.assertEqual(self.store.get("st:fn:exp:cs_live_apix00000001"), "api")        # remembered after Stripe's 30 days
        f._cache.clear()
        m2 = f.report("2026-11-22", "2026-11-22", include_own=True)["periods"]["range"]["metrics"]
        self.assertEqual(m2["abandoned"]["EBD-AND"], 5)

    def test_unique_visitors_daily_salt_and_owner_network(self):
        vh1, nh1 = F.visitor_hashes(self.store, "198.51.100.7", "Mozilla/5.0 A", now=NOW)
        vh2, _ = F.visitor_hashes(self.store, "198.51.100.7", "Mozilla/5.0 A", now=NOW)
        vh3, _ = F.visitor_hashes(self.store, "198.51.100.8", "Mozilla/5.0 A", now=NOW)
        vh_next, _ = F.visitor_hashes(self.store, "198.51.100.7", "Mozilla/5.0 A", now=NOW + 86400)
        self.assertEqual(vh1, vh2); self.assertNotEqual(vh1, vh3); self.assertNotEqual(vh1, vh_next)   # new salt every day
        self.assertNotIn("198.51.100.7", json.dumps(self.store._d, default=str))
        for vh in (vh1, vh2, vh3):
            self.st.record("bs", ok=1, p="EBD-WIN", vh=vh, sid="")
        F.mark_owner_network(self.store, "203.0.113.9", now=NOW)
        ovh, onh = F.visitor_hashes(self.store, "203.0.113.9", "Mozilla/5.0 owner", now=NOW)
        self.st.record("bs", ok=1, p="EBD-WIN", vh=ovh, nh=onh, sid="cs_live_ownnet000001")
        self.st.record("bs", ok=0, p="EBD-WIN", r="payment_unavailable", vh=vh1)
        stripe = FakeStripe(sessions=[sess("cs_live_ownnet000001", product="EBD-WIN")])
        m = self.funnel(stripe).report("2026-11-22", "2026-11-22")["periods"]["range"]["metrics"]
        self.assertEqual(m["start_requests"]["EBD-WIN"], 3)
        self.assertEqual(m["start_unique_daily"], 2)
        self.assertEqual(m["start_failed"], 1)
        self.assertEqual(m["own"]["by_reason"], {"owner_network": 1})

    def test_stripe_outage_is_reported_not_hidden(self):
        class Down(FakeStripe):
            def request(self, *a, **k): raise TimeoutError("stripe down")
        r = self.funnel(Down()).report("2026-11-22", "2026-11-22")
        self.assertTrue(r["stripe_errors"])
        self.assertEqual(sum(r["periods"]["range"]["metrics"]["reached"].values()), 0)


class FunnelEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main
        cls.m = main

    def setUp(self):
        os.environ["ADMIN_TOKEN"] = "adm-36"

    def tearDown(self):
        os.environ.pop("ADMIN_TOKEN", None)

    def test_checkout_records_purchase_start_without_ip(self):
        m = self.m
        before = len([e for e in m.STATS.events(NOW - 10 ** 8, 2 ** 31 - 1) if e["k"] == "bs"])
        req = m.StripeCheckoutRequest(product="android", lang="hu", consent=True, promo="")
        with self.assertRaises(Exception):                                   # no Stripe key in tests -> 503
            m.stripe_checkout(req, FakeRequest(ip="198.51.100.20", headers={"user-agent": "Mozilla/5.0", "origin": "https://ebikedoctorpro.com"}))
        evs = [e for e in m.STATS.events(NOW - 10 ** 8, 2 ** 31 - 1) if e["k"] == "bs"][before:]
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e.get("ok", 0), e["p"], e.get("web")), (0, "EBD-AND", 1))
        self.assertTrue(e.get("vh")); self.assertNotIn("198.51.100.20", json.dumps(e))
        with self.assertRaises(Exception):
            m.stripe_checkout(req, FakeRequest(headers={"user-agent": "x", "x-admin-token": "adm-36"}))
        self.assertTrue([x for x in m.STATS.events(NOW - 10 ** 8, 2 ** 31 - 1) if x["k"] == "bs"][-1].get("own"))

    def test_funnel_endpoint_requires_login_and_classify_requires_admin(self):
        r = FakeRequest(); r.cookies = {}; r.query_params = {}
        with self.assertRaises(Exception) as cm:
            self.m.stats_funnel(request=r)
        self.assertEqual(cm.exception.status_code, 404)
        d = self.m.stats_funnel(request=None, x_admin_token="adm-36")
        self.assertIn("periods", d); self.assertEqual(set(d["periods"]), {"today", "week", "month", "year", "all", "range"})
        with self.assertRaises(Exception):
            self.m.admin_funnel_classify(self.m.FunnelClassRequest(items=[]), x_admin_token="wrong", request=None)
        out = self.m.admin_funnel_classify(self.m.FunnelClassRequest(items=[self.m.FunnelClassItem(sid="cs_live_abcdefghij12", cls="own_review"),
                                                                            self.m.FunnelClassItem(sid="bad", cls="own_review")]), x_admin_token="adm-36", request=None)
        self.assertEqual(out["classified"], 1)


if __name__ == "__main__":
    unittest.main()
