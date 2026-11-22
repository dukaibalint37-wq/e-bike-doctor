"""Purchase funnel (3.6) – checked against Stripe's real data, nothing estimated.

Steps (each counted separately, per product, per Budapest day):
  start     = the website buy button sent a checkout request (our event "bs"); unique visitors = distinct daily visitor hashes
              (salted with a random salt that changes every day and is deleted after two days: no IP address, no cookie,
              the same person cannot be followed from one day to the next -> "unique per day")
  reached   = Stripe created the payment page and the browser was sent there (Checkout Session exists at Stripe)
  paid      = Stripe: status complete + payment_status paid
  failed    = PROVEN payment failure: Stripe has a failed charge or a payment-intent error for the session (reason = Stripe's
              decline code / outcome); several card attempts in one session are counted as attempts
  abandoned = the session expired (or is still open but past its 24 h) with NO payment attempt at all
  open      = still payable (younger than 24 h, no attempt yet)
  coupon    = a paid session with a discount (and: payment pages opened with a code)
Own tests are kept apart (never mixed into the real figures):
  * requests of our own tools (X-Admin-Token / "EBD-" user agent) and of the network from which the owner logged into the
    statistics page that day
  * sessions expired by an API call (Stripe event with a request id - the shop code never expires a session)
  * sessions classified once by the evidence-based review of the history (st:fn:class:<session>)
Test-mode payments (Stripe test keys) never appear in the live account: they belong to the staging system.
"""
import hashlib
import os
import time

import stats as ST

PRODUCTS = ("EBD-WIN", "EBD-AND")
KEEP = ST.KEEP
REASON_HU = {
    "card_declined": "Kártya elutasítva (általános)", "generic_decline": "Kártya elutasítva (általános)", "do_not_honor": "A bank elutasította",
    "insufficient_funds": "Nincs elég fedezet", "expired_card": "Lejárt kártya", "incorrect_cvc": "Hibás CVC", "invalid_cvc": "Hibás CVC",
    "incorrect_number": "Hibás kártyaszám", "invalid_number": "Hibás kártyaszám", "invalid_expiry_month": "Hibás lejárati dátum",
    "invalid_expiry_year": "Hibás lejárati dátum", "authentication_required": "3D Secure azonosítás sikertelen / elmaradt",
    "payment_intent_authentication_failure": "3D Secure azonosítás sikertelen", "processing_error": "Feldolgozási hiba (bank / Stripe)",
    "fraudulent": "Csalásgyanú (bank)", "highest_risk_level": "Stripe Radar letiltotta (magas kockázat)", "elevated_risk_level": "Stripe Radar: emelt kockázat",
    "rule": "Stripe Radar szabály tiltotta", "lost_card": "Elveszett kártya", "stolen_card": "Ellopott kártya", "card_not_supported": "A kártya nem támogatott",
    "currency_not_supported": "A pénznem nem támogatott", "transaction_not_allowed": "A tranzakció nem engedélyezett", "try_again_later": "Próbálja később (bank)",
    "async_payment_failed": "Késleltetett fizetés sikertelen (pl. banki átutalás)", "unknown": "Ismeretlen ok"}


def _day_salt(store, day):
    """Random salt of one day, deleted after two days (so a hash can never be linked back or across days)."""
    k = "st:salt:" + day
    s = store.get(k)
    if not s:
        cand = os.urandom(16).hex()
        if store.incr(k + ":lock", 3 * 86_400) == 1:
            store.set(k, cand, 2 * 86_400)
        s = store.get(k) or cand
    return str(s)


def visitor_hashes(store, ip, user_agent, now=None):
    """(visitor hash = IP + browser, network hash = IP only) for today; never stored with the IP itself."""
    day = time.strftime("%Y%m%d", time.gmtime(now or time.time()))
    try:
        salt = _day_salt(store, day)
    except Exception:                                    # noqa: BLE001 - statistics never break the shop
        return "", ""
    vh = hashlib.sha256(f"{salt}|{ip}|{user_agent or ''}".encode()).hexdigest()[:16]
    nh = hashlib.sha256(f"{salt}|net|{ip}".encode()).hexdigest()[:16]
    return vh, nh


def mark_owner_network(store, ip, now=None):
    """The owner opened the statistics page from this network today: same-day purchase starts from it count as own tests."""
    day = time.strftime("%Y%m%d", time.gmtime(now or time.time()))
    _, nh = visitor_hashes(store, ip, "", now)
    if not nh:
        return
    try:
        cur = store.get("st:ownnet:" + day) or []
        if nh not in cur:
            store.set("st:ownnet:" + day, (cur if isinstance(cur, list) else [])[-20:] + [nh], 3 * 86_400)
    except Exception:                                    # noqa: BLE001
        pass


class Funnel:
    def __init__(self, stripe_client, stats, shop_mode, clock=time.time):
        self.stripe, self.stats, self.clock = stripe_client, stats, clock
        self.shop_mode = shop_mode                       # callable -> "live" / "test"
        self._cache = {}

    # ---- Stripe data (read-only, cached 5 minutes)
    def _paged(self, path, params, pages=30):
        out, p = [], dict(params)
        for _ in range(pages):
            page = self.stripe.request("GET", path, p)
            data = page.get("data") or []
            out += data
            if not page.get("has_more") or not data:
                break
            p["starting_after"] = data[-1]["id"]
        return out

    def _load(self, since):
        key = (since, int(self.clock()) // 300)
        if key in self._cache:
            return self._cache[key]
        store = self.stats.store
        errors = []
        sessions, charges, expired_events, failed_events = [], [], [], []
        try:
            sessions = self._paged("/checkout/sessions", {"limit": 100, "created": {"gte": since}, "expand": ["data.payment_intent"]})
        except Exception as exc:                         # noqa: BLE001
            errors.append("checkout_sessions: " + str(getattr(exc, "code", "") or type(exc).__name__))
        try:
            charges = self._paged("/charges", {"limit": 100, "created": {"gte": since}})
        except Exception as exc:                         # noqa: BLE001
            errors.append("charges: " + str(getattr(exc, "code", "") or type(exc).__name__))
        try:                                             # Stripe keeps events for 30 days: the result is stored permanently
            expired_events = self._paged("/events", {"limit": 100, "type": "checkout.session.expired", "created": {"gte": max(since, int(self.clock()) - 30 * 86_400)}})
        except Exception as exc:                         # noqa: BLE001
            errors.append("events: " + str(getattr(exc, "code", "") or type(exc).__name__))
        try:
            failed_events = self._paged("/events", {"limit": 100, "type": "checkout.session.async_payment_failed", "created": {"gte": max(since, int(self.clock()) - 30 * 86_400)}})
        except Exception as exc:                         # noqa: BLE001
            errors.append("events_async: " + str(getattr(exc, "code", "") or type(exc).__name__))
        for ev in expired_events:
            sid = ((ev.get("data") or {}).get("object") or {}).get("id")
            req = ev.get("request")
            by_api = bool(req.get("id")) if isinstance(req, dict) else bool(req)
            if sid:
                try:
                    if store.get("st:fn:exp:" + sid) is None:
                        store.set("st:fn:exp:" + sid, "api" if by_api else "auto", KEEP)
                except Exception:                        # noqa: BLE001
                    pass
        async_failed = {((ev.get("data") or {}).get("object") or {}).get("id") for ev in failed_events}
        from store import mget
        sids = [s.get("id", "") for s in sessions]
        try:
            vals = mget(store, ["st:fn:exp:" + s for s in sids] + ["st:fn:class:" + s for s in sids])
        except Exception:                                # noqa: BLE001
            vals = [None] * (2 * len(sids))
        exp = dict(zip(sids, vals[:len(sids)]))
        cls = dict(zip(sids, vals[len(sids):]))
        ch_by_pi = {}
        for c in charges:
            pi = c.get("payment_intent")
            ch_by_pi.setdefault(pi if isinstance(pi, str) else (pi or {}).get("id"), []).append(c)
        out = {"sessions": sessions, "ch_by_pi": ch_by_pi, "exp": exp, "cls": cls, "async_failed": async_failed, "errors": errors}
        self._cache = {key: out}
        return out

    # ---- classification of one Stripe Checkout Session
    def classify(self, s, data, own_sids, now):
        meta = s.get("metadata") or {}
        pi = s.get("payment_intent") if isinstance(s.get("payment_intent"), dict) else {}
        pi_id = (pi or {}).get("id") or (s.get("payment_intent") if isinstance(s.get("payment_intent"), str) else None)
        chs = data["ch_by_pi"].get(pi_id, []) if pi_id else []
        failed = [c for c in chs if c.get("status") == "failed"]
        lpe = (pi or {}).get("last_payment_error") or {}
        reasons = []
        for c in failed:
            o = c.get("outcome") or {}
            reasons.append(c.get("failure_code") if c.get("failure_code") not in (None, "card_declined") else (o.get("reason") or c.get("failure_code") or "card_declined"))
        if not failed and lpe:
            reasons.append(lpe.get("decline_code") or lpe.get("code") or "unknown")
        if s.get("id") in data["async_failed"]:
            reasons.append("async_payment_failed")
        paid = s.get("status") == "complete" and s.get("payment_status") == "paid"
        if paid:
            outcome = "paid"
        elif reasons:
            outcome = "failed"
        elif s.get("status") == "open" and (s.get("expires_at") or 0) > now:
            outcome = "open"
        else:
            outcome = "abandoned"
        sid = s.get("id", "")
        own_why = ""
        if sid in own_sids:
            own_why = own_sids[sid]
        elif data["exp"].get(sid) == "api":
            own_why = "api_expired"
        elif (data["cls"].get(sid) or "").startswith("own"):
            own_why = "review"
        disc = int((s.get("total_details") or {}).get("amount_discount") or 0)
        return {"sid": sid, "t": int(s.get("created") or 0), "p": meta.get("product", ""), "lang": meta.get("lang", ""), "promo": meta.get("promo", ""),
                "outcome": outcome, "reasons": reasons, "attempts": len(chs) or (1 if lpe else 0), "own": own_why,
                "review": data["cls"].get(sid) or "", "discount": disc, "amount": int(s.get("amount_total") or 0),
                "currency": str(s.get("currency") or "").upper(), "email_entered": bool((s.get("customer_details") or {}).get("email")),
                "live": bool(s.get("livemode"))}

    # ---- report
    def report(self, d_from, d_to, include_own=False):
        now = int(self.clock())
        today = ST.local_date(now)
        y, m, _ = today.split("-")
        start_all = os.getenv("FUNNEL_START", "2026-11-01")
        since = ST.day_start(min(start_all, d_from, f"{y}-01-01"))
        data = self._load(since)
        live = self.shop_mode() == "live"
        # our own purchase-start events: unique visitors + own-test marks
        evs = [e for e in self.stats.events(since, now + 86_400) if e.get("k") == "bs"]
        own_nets = {}
        own_sids = {}
        for e in evs:
            day = time.strftime("%Y%m%d", time.gmtime(e["t"]))
            if day not in own_nets:
                try:
                    own_nets[day] = set(self.stats.store.get("st:ownnet:" + day) or [])
                except Exception:                        # noqa: BLE001
                    own_nets[day] = set()
            if e.get("own"):
                e["_own"] = "own_tool"
            elif e.get("nh") and e["nh"] in own_nets[day]:
                e["_own"] = "owner_network"
            if e.get("_own") and e.get("sid"):
                own_sids[e["sid"]] = e["_own"]
        rows = [self.classify(s, data, own_sids, now) for s in data["sessions"]
                if (s.get("metadata") or {}).get("shop") == "ebd-website" and bool(s.get("livemode")) == live]
        rows.sort(key=lambda r: r["t"])
        firsts = [r["t"] for r in rows] + [e["t"] for e in evs]
        all_from = min([start_all] + ([ST.local_date(min(firsts))] if firsts else []))

        def bucket(a, b):
            ta, tb = ST.day_start(a), ST.day_start(ST.add_days(b, 1))
            return summarize([r for r in rows if ta <= r["t"] < tb], [e for e in evs if ta <= e["t"] < tb], include_own)

        periods = {"today": (today, today), "week": (ST.week_start(today), today), "month": (f"{y}-{m}-01", today),
                   "year": (f"{y}-01-01", today), "all": (all_from, today), "range": (d_from, d_to)}
        out_periods = {k: {"from": a, "to": b, "metrics": bucket(a, b)} for k, (a, b) in periods.items()}
        daily, d = [], d_from
        while d <= d_to and len(daily) < 400:
            daily.append({"key": d, **mini(bucket(d, d))})
            d = ST.add_days(d, 1)
        weekly, ws = [], ST.week_start(d_to)
        for i in range(25, -1, -1):
            a = ST.add_days(ws, -7 * i)
            weekly.append({"key": a, **mini(bucket(a, ST.add_days(a, 6)))})
        monthly, yy, mm, months = [], int(d_to[:4]), int(d_to[5:7]), []
        for _ in range(24):
            months.append((yy, mm)); mm -= 1
            if mm == 0:
                yy, mm = yy - 1, 12
        import calendar
        for yy, mm in reversed(months):
            a, b = f"{yy:04d}-{mm:02d}-01", f"{yy:04d}-{mm:02d}-{calendar.monthrange(yy, mm)[1]:02d}"
            monthly.append({"key": a[:7], **mini(bucket(a, b))})
        ta, tb = ST.day_start(d_from), ST.day_start(ST.add_days(d_to, 1))
        listing = [dict(r, local=time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["t"] + ST.bp_offset(r["t"]))), sid=r["sid"][-8:])
                   for r in rows if ta <= r["t"] < tb][-100:]
        first_bs = min([e["t"] for e in evs], default=0)
        return {"generated": now, "live": live, "include_own": include_own, "periods": out_periods,
                "series": {"daily": daily, "weekly": weekly, "monthly": monthly}, "sessions": list(reversed(listing)),
                "measured_since": first_bs, "stripe_errors": data["errors"], "reason_labels": REASON_HU}


def _z():
    return {p: 0 for p in PRODUCTS}


def summarize(rows, evs, include_own=False):
    m = {"start_requests": _z(), "start_failed": 0, "start_unique_daily": 0, "reached": _z(), "paid": _z(), "failed": _z(), "abandoned": _z(),
         "open": _z(), "attempts_failed": 0, "reasons": {}, "coupon_paid": _z(), "coupon_started": _z(), "coupon_reached": _z(), "email_entered_abandoned": 0,
         "own": {"sessions": 0, "paid": 0, "starts": 0, "by_reason": {}}, "unknown_product": 0, "paid_amount": {}, "external_unverified": 0}
    days = {}
    for e in evs:
        if e.get("_own") and not include_own:
            m["own"]["starts"] += 1
            continue
        p = e.get("p")
        if e.get("ok"):
            if p in PRODUCTS:
                m["start_requests"][p] += 1
                if e.get("promo"):
                    m["coupon_started"][p] += 1
        else:
            m["start_failed"] += 1
        if e.get("vh"):
            days.setdefault(time.strftime("%Y%m%d", time.gmtime(e["t"])), set()).add(e["vh"])
    m["start_unique_daily"] = sum(len(v) for v in days.values())
    for r in rows:
        if r["own"] and not include_own:
            m["own"]["sessions"] += 1
            m["own"]["paid"] += 1 if r["outcome"] == "paid" else 0
            m["own"]["by_reason"][r["own"]] = m["own"]["by_reason"].get(r["own"], 0) + 1
            continue
        p = r["p"]
        if p not in PRODUCTS:
            m["unknown_product"] += 1
            continue
        m["reached"][p] += 1
        if r["promo"]:
            m["coupon_reached"][p] += 1
        m[r["outcome"]][p] += 1
        if r["outcome"] == "paid":
            m["paid_amount"][r["currency"]] = m["paid_amount"].get(r["currency"], 0) + r["amount"]
            if r["discount"]:
                m["coupon_paid"][p] += 1
        if r["outcome"] == "failed":
            m["attempts_failed"] += max(1, r["attempts"])
            for x in r["reasons"] or ["unknown"]:
                m["reasons"][x] = m["reasons"].get(x, 0) + 1
        if r["outcome"] == "abandoned" and r["email_entered"]:
            m["email_entered_abandoned"] += 1
        if r["review"].startswith("external"):
            m["external_unverified"] += 1          # history: probably a real visitor, but not provable (e.g. the owner's phone)
    return m


def mini(m):
    s = lambda k: sum(m[k].values())
    return {"start": s("start_requests"), "uniq": m["start_unique_daily"], "reached": s("reached"), "paid": s("paid"),
            "failed": s("failed"), "abandoned": s("abandoned"), "open": s("open"), "own": m["own"]["sessions"]}
