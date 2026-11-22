"""Permanent download / trial / sales statistics (3.5) – always on, not tied to any promotion.

Storage: one append-only event list per UTC day in the persistent store (Upstash, key st:ev:YYYYMMDD, RPUSH = atomic, no lost
updates), kept for 20 years. Every figure on the statistics page is COUNTED from these events – nothing is estimated.

What a "download" is (honest definitions, shown on the page as well):
  link   = a download link was issued for a licence key (website form / app update)
  dl     = the download address /v1/shop/download was opened
           ok=1  -> the licence was valid and the browser was sent to the file (pre-signed, private bucket) = download STARTED
           ok=0  -> refused (reason: invalid / expired link, revoked licence, trial ended, daily limit, ...)
           bot=1 -> opened by an automatic link checker (mail security scanner, crawler) – counted separately, never as a download
  act    = the app was activated on a device = the program was downloaded, installed and run (the only server-side PROOF of a
           completed download; the file transfer itself happens between the browser and the bucket and is not visible here)
Personal data: events hold licence ids (L000123), never e-mail addresses, IP addresses or names.

Earlier data (before the statistics started) is added once from what the system already kept (licence histories, order log,
daily download / checkout counters) and flagged bf=1 ("visszamenőleges"); unique/repeat splits exist only for measured data.
"""
import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque

from licensing import LicenseError, trial_of
import admin_auth

logger = logging.getLogger("ebike-doctor.stats")
PRODUCTS = ("EBD-WIN", "EBD-AND")
KEEP = 20 * 365 * 86_400
DAY = 86_400
COOKIE = "ebd_stats"
SCANNER = re.compile(r"bot|crawl|spider|slurp|preview|scan|safelinks|proofpoint|mimecast|barracuda|symantec|trendmicro|"
                     r"headless|phantom|python|curl|wget|go-http|java/|okhttp/2|libwww|httpx|aiohttp|facebookexternalhit|"
                     r"linkcheck|validator|monitor", re.I)


# --------------------------------------------------------------------------------------------- Budapest calendar
def _last_sunday_utc(year, month, hour_utc=1):
    last = calendar.monthrange(year, month)[1]
    wd = calendar.weekday(year, month, last)            # Monday=0 … Sunday=6
    return calendar.timegm((year, month, last - (wd + 1) % 7, hour_utc, 0, 0))


def bp_offset(ts):
    """Europe/Budapest UTC offset (CET +1, CEST +2 from the last Sunday of March to the last Sunday of October, 01:00 UTC)."""
    y = time.gmtime(ts).tm_year
    return 7200 if _last_sunday_utc(y, 3) <= ts < _last_sunday_utc(y, 10) else 3600


def local_date(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + bp_offset(ts)))


def day_start(date_str):
    """UTC timestamp of local midnight of YYYY-MM-DD (midnight is never inside a DST switch in Hungary)."""
    y, m, d = (int(x) for x in date_str.split("-"))
    t0 = calendar.timegm((y, m, d, 0, 0, 0))
    return t0 - bp_offset(t0 - 7200)


def add_days(date_str, n):
    y, m, d = (int(x) for x in date_str.split("-"))
    return time.strftime("%Y-%m-%d", time.gmtime(calendar.timegm((y, m, d, 12, 0, 0)) + n * DAY))


def valid_date(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""):
        return False
    try:
        time.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def week_start(date_str):
    y, m, d = (int(x) for x in date_str.split("-"))
    return add_days(date_str, -calendar.weekday(y, m, d))


def _utc_day(ts):
    return time.strftime("%Y%m%d", time.gmtime(ts))


# --------------------------------------------------------------------------------------------- recorder
class Stats:
    def __init__(self, store, clock=time.time):
        self.store = getattr(store, "primary", store)        # never the silent memory fallback: errors must be visible
        self.clock = clock
        self.pending = deque(maxlen=5000)                    # events kept while the store is unreachable (flushed later)
        self.errors = 0
        self._cache, self._lock = {}, threading.Lock()
        self._bf_lock = threading.Lock()
        self._report_cache = {}
        self._ver = 0                                        # bumped by every event: a cached report is never stale
        self._started = 0

    # ---- writing
    def start(self):
        """Statistics start time (set once, before the first event): earlier history comes only from the backfill."""
        try:
            if not self.store.get("st:start"):
                if self.store.incr("st:start:lock", KEEP) == 1:
                    self.store.set("st:start", int(self.clock()), KEEP)
                    self.store.set("st:first_day", _utc_day(self.clock()), KEEP)
            self._started = int(self.store.get("st:start") or 0)
            return self._started
        except Exception:                                    # noqa: BLE001 - statistics never break the API
            self.errors += 1
            return 0

    def record(self, kind, **fields):
        """Appends one event. Never raises: a statistics problem must never affect a download, a trial or a payment."""
        if not self._started:
            self.start()                                     # store was down at startup: the start must precede every event
        ev = {"t": int(self.clock()), "k": kind, **{k: v for k, v in fields.items() if v not in (None, "", False)}}
        for k, v in list(ev.items()):
            if v is True:
                ev[k] = 1
        self.pending.append(ev)
        self._ver += 1
        self._flush()
        return ev

    def _flush(self):
        while self.pending:
            ev = self.pending[0]
            try:
                self.store.push("st:ev:" + _utc_day(ev["t"]), ev, KEEP)
            except Exception:                                # noqa: BLE001
                self.errors += 1
                logger.warning("statistics store unreachable - %d event(s) waiting", len(self.pending))
                return False
            self.pending.popleft()
        return True

    def first_download(self, lid):
        """True only for the very first started download of a licence (all time)."""
        try:
            return self.store.incr("st:first:dl:" + lid, KEEP) == 1
        except Exception:                                    # noqa: BLE001
            return False

    def first_activation(self, lid):
        try:
            return self.store.incr("st:first:act:" + lid, KEEP) == 1
        except Exception:                                    # noqa: BLE001
            return False

    # ---- hooks used by the shop / licensing / trial code
    def download(self, ok, product="", lid="", test=False, owner=False, reason="", user_agent="", method="GET"):
        bot = bool(SCANNER.search(user_agent or "")) or method == "HEAD"
        first = bool(ok and lid and not bot and self.first_download(lid))
        return self.record("dl", ok=1 if ok else 0, p=product, lid=lid, m="test" if test else "live", o=owner, r="" if ok else reason,
                           bot=bot, first=first)

    def activation(self, rec, transfer=False):
        t = trial_of(rec)
        ed = "TRIAL" if t else ("OWNER" if rec.get("owner") else "PRO")
        return self.record("act", p=rec.get("product", ""), lid=rec.get("lid", ""), ed=ed, m="test" if rec.get("test") else "live",
                           o=bool(rec.get("owner")), first=self.first_activation(rec.get("lid", "")), tr=transfer)

    # ---- reading
    def _load(self, days):
        """Event lists of UTC days (YYYYMMDD). Days before yesterday are immutable -> cached in this process."""
        today = _utc_day(self.clock())
        yesterday = _utc_day(self.clock() - DAY)
        need = [d for d in days if d in (today, yesterday) or d not in self._cache]
        if need:
            lists = self.store.lrange_many(["st:ev:" + d for d in need])
            with self._lock:
                for d, lst in zip(need, lists):
                    self._cache[d] = lst if isinstance(lst, list) else []
        return [e for d in days for e in self._cache.get(d, []) if isinstance(e, dict) and "t" in e]

    def events(self, t_from, t_to):
        days, t = [], t_from - DAY
        while t <= t_to + DAY:
            days.append(_utc_day(t))
            t += DAY
        return [e for e in self._load(sorted(set(days))) if t_from <= e["t"] < t_to]

    # ---- backfill (once): history kept by the system before the statistics started
    def backfill(self, licenses, shop, force=False):
        with self._bf_lock:
            if self.store.get("st:bf:v1") and not force:
                return self.store.get("st:bf:v1")
            start = self.start()
            if not start:
                raise LicenseError("store_unavailable", 503, "Statistics store unavailable")
            evs = []
            recs = licenses.all(limit=100_000)
            orders = shop.orders()
            ordered_lids = {o.get("lid") for o in orders}
            for r in recs:
                if r.get("purged"):
                    continue
                base = {"p": r.get("product", ""), "lid": r["lid"], "m": "test" if r.get("test") else "live", "o": 1 if r.get("owner") else 0}
                t = trial_of(r)
                if t:
                    if r.get("created", 0) < start:
                        evs.append({"t": int(r["created"]), "k": "tri", **base})
                acts = [h for h in r.get("history", []) if h.get("e") == "activated"]
                if t and t.get("started") and not acts:
                    acts = [{"t": t["started"]}]
                ed = "TRIAL" if t else ("OWNER" if r.get("owner") else "PRO")
                for i, h in enumerate(acts):
                    if int(h.get("t", 0)) < start:
                        evs.append({"t": int(h["t"]), "k": "act", **base, "ed": ed, "first": 1 if i == 0 else 0})
                if acts and int(acts[0].get("t", 0)) < start:
                    self.first_activation(r["lid"])                  # a later re-activation is not a "first" one
                if not t and not r.get("owner") and r.get("source") in ("stripe", "lemonsqueezy") and r["lid"] not in ordered_lids \
                        and r.get("created", 0) < start:
                    evs.append({"t": int(r["created"]), "k": "sale", **base, "src": r.get("source"), "nomoney": 1})
            for o in orders:
                if int(o.get("t", 0)) >= start:
                    continue
                cents = int(o.get("total") or 0)
                ev = {"t": int(o["t"]), "k": "sale", "p": o.get("product", ""), "lid": o.get("lid", ""), "m": o.get("mode", "live"),
                      "c": cents, "cur": (o.get("currency") or "EUR").upper(), "d": int(o.get("discount") or 0), "src": o.get("provider", "")}
                evs.append(ev)
                ref = int(o.get("refunded_cents") or 0)
                if ref:
                    evs.append({"t": int(o["t"]), "k": "ref", "p": o.get("product", ""), "m": o.get("mode", "live"), "c": ref,
                                "cur": ev["cur"], "full": 1 if o.get("refunded") else 0, "dated_at_purchase": 1})
            # daily counters (download starts, opened checkouts) of the last 400 days before the start
            sday = _utc_day(start)
            from store import mget
            dlist = [_utc_day(start - i * DAY) for i in range(0, 400)]
            cols = [(p, m) for p in PRODUCTS for m in ("live", "test")]
            for prefix, kind in (("shop:dlday", "dl"), ("shop:checkouts", "co")):
                vals = mget(self.store, [f"{prefix}:{d}:{p}:{m}" for d in dlist for p, m in cols])
                for i, d in enumerate(dlist):
                    for j, (p, m) in enumerate(cols):
                        n = int(vals[i * len(cols) + j] or 0)
                        if d == sday and n:                                  # start day: only the part before the start
                            measured = [e for e in self.events(day_start_from_utc(d), start + 2 * DAY)
                                        if e["k"] == kind and e.get("p") == p and e.get("m", "live") == m and (kind == "co" or e.get("ok"))
                                        and not e.get("bf") and not e.get("bot") and _utc_day(e["t"]) == d]
                            n -= len(measured)
                        if n > 0:
                            t_mid = min(calendar.timegm(time.strptime(d, "%Y%m%d")) + 12 * 3600, start - 1)
                            evs.append({"t": t_mid, "k": kind, "p": p, "m": m, "n": n, **({"ok": 1} if kind == "dl" else {})})
            for e in evs:
                e["bf"] = 1
                self.store.push("st:ev:" + _utc_day(e["t"]), e, KEEP)
            first = min([_utc_day(e["t"]) for e in evs] + [self.store.get("st:first_day") or sday])
            self.store.set("st:first_day", first, KEEP)
            undated_requests = 0
            try:
                undated_requests = int(self.store.get("trial:stat:requested") or 0)
            except Exception:                                # noqa: BLE001
                pass
            info = {"at": int(self.clock()), "start": start, "events": len(evs), "undated_trial_requests_before_start": undated_requests}
            self.store.set("st:bf:v1", info, KEEP)
            with self._lock:
                self._cache.clear()
            self._report_cache.clear()
            return info

    # ---- report
    def report(self, d_from, d_to, include_test=False, include_owner=False):
        key = (d_from, d_to, include_test, include_owner, int(self.clock()) // 20, self._ver)
        if key in self._report_cache:
            return self._report_cache[key]
        self._flush()
        now = self.clock()
        today = local_date(now)
        first_day = str(self.store.get("st:first_day") or _utc_day(now))
        all_from = time.strftime("%Y-%m-%d", time.strptime(first_day, "%Y%m%d"))
        all_from = add_days(all_from, -1)
        lo = min(all_from, d_from)
        evs = [e for e in self.events(day_start(lo), day_start(add_days(max(today, d_to), 1)))
               if (include_test or e.get("m", "live") != "test") and (include_owner or not e.get("o"))]
        evs.sort(key=lambda e: e["t"])

        def between(a, b):
            ta, tb = day_start(a), day_start(add_days(b, 1))
            return [e for e in evs if ta <= e["t"] < tb]

        y, m, _ = today.split("-")
        periods = {"today": {"from": today, "to": today}, "week": {"from": week_start(today), "to": today},
                   "month": {"from": f"{y}-{m}-01", "to": today}, "year": {"from": f"{y}-01-01", "to": today},
                   "all": {"from": all_from, "to": today}, "range": {"from": d_from, "to": d_to}}
        for p in periods.values():
            p["metrics"] = summarize(between(p["from"], p["to"]))
        # series
        daily, d = [], d_from
        while d <= d_to and len(daily) < 400:
            daily.append({"key": d, **compact(between(d, d))})
            d = add_days(d, 1)
        weekly, ws = [], week_start(d_to)
        for i in range(25, -1, -1):
            a = add_days(ws, -7 * i)
            weekly.append({"key": a, **compact(between(a, add_days(a, 6)))})
        monthly = []
        yy, mm = int(d_to[:4]), int(d_to[5:7])
        months = []
        for _ in range(24):
            months.append((yy, mm))
            mm -= 1
            if mm == 0:
                yy, mm = yy - 1, 12
        for yy, mm in reversed(months):
            a = f"{yy:04d}-{mm:02d}-01"
            b = f"{yy:04d}-{mm:02d}-{calendar.monthrange(yy, mm)[1]:02d}"
            monthly.append({"key": a[:7], **compact(between(a, b))})
        bf = self.store.get("st:bf:v1")
        out = {"generated": int(now), "timezone": "Europe/Budapest", "today": today, "stats_start": int(self.store.get("st:start") or 0),
               "backfill": bf if isinstance(bf, dict) else None, "include_test": include_test, "include_owner": include_owner,
               "periods": periods, "series": {"daily": daily, "weekly": weekly, "monthly": monthly},
               "health": {"pending_events": len(self.pending), "store_errors": self.errors}}
        self._report_cache = {key: out}
        return out


def day_start_from_utc(utc_day):
    return calendar.timegm(time.strptime(utc_day, "%Y%m%d"))


# --------------------------------------------------------------------------------------------- aggregation
def _z():
    return {p: 0 for p in PRODUCTS}


def summarize(evs):
    dl = {"opened": _z(), "opened_unknown": 0, "started": _z(), "legacy_started": _z(), "failed": _z(), "failed_unknown": 0,
          "failed_by_reason": {}, "bots": 0, "unique": _z(), "repeat": _z(), "new": _z(), "links": {"web": 0, "app": 0}}
    trials = {"requested": 0, "requested_new": 0, "issued": _z(), "activated": _z()}
    acts = {p: {"PRO": 0, "TRIAL": 0, "OWNER": 0} for p in PRODUCTS}
    acts_first, transfers = _z(), 0
    sales = {"count": _z(), "revenue": {}, "discount": {}, "refunds": {}, "refund_count": 0, "net": {}, "promo_count": 0,
             "checkouts": _z(), "checkouts_promo": 0, "without_amount": 0}
    lids = {p: set() for p in PRODUCTS}
    measured_started = _z()
    for e in evs:
        k, p, n = e.get("k"), e.get("p", ""), int(e.get("n") or 1)
        known = p in PRODUCTS
        if k == "dl":
            if e.get("bot"):
                dl["bots"] += n
                continue
            if known:
                dl["opened"][p] += n
            else:
                dl["opened_unknown"] += n
            if e.get("ok"):
                if not known:
                    continue
                if e.get("bf"):
                    dl["legacy_started"][p] += n
                else:
                    dl["started"][p] += n
                    measured_started[p] += n
                    if e.get("lid"):
                        lids[p].add(e["lid"])
                    if e.get("first"):
                        dl["new"][p] += 1
            else:
                r = e.get("r") or "unknown"
                dl["failed_by_reason"][r] = dl["failed_by_reason"].get(r, 0) + n
                if known:
                    dl["failed"][p] += n
                else:
                    dl["failed_unknown"] += n
        elif k == "link":
            src = e.get("src") if e.get("src") in ("web", "app") else "app"
            dl["links"][src] += 1
        elif k == "act" and known:
            ed = e.get("ed") if e.get("ed") in ("PRO", "TRIAL", "OWNER") else "PRO"
            acts[p][ed] += 1
            if e.get("first"):
                acts_first[p] += 1
                if ed == "TRIAL":
                    trials["activated"][p] += 1
            if e.get("tr"):
                transfers += 1
        elif k == "trq":
            trials["requested"] += 1
            trials["requested_new"] += 1 if e.get("new") else 0
        elif k == "tri" and known:
            trials["issued"][p] += 1
        elif k == "co" and known:
            sales["checkouts"][p] += n
            sales["checkouts_promo"] += 1 if e.get("promo") else 0
        elif k == "sale" and known:
            sales["count"][p] += 1
            if e.get("nomoney"):
                sales["without_amount"] += 1
            else:
                cur = e.get("cur") or "USD"
                sales["revenue"][cur] = sales["revenue"].get(cur, 0) + int(e.get("c") or 0)
                if e.get("d"):
                    sales["discount"][cur] = sales["discount"].get(cur, 0) + int(e["d"])
            if e.get("promo"):
                sales["promo_count"] += 1
        elif k == "ref":
            cur = e.get("cur") or "USD"
            sales["refunds"][cur] = sales["refunds"].get(cur, 0) + int(e.get("c") or 0)
            sales["refund_count"] += 1 if e.get("full") else 0
    for p in PRODUCTS:
        dl["unique"][p] = len(lids[p])
        dl["repeat"][p] = measured_started[p] - len(lids[p])
    for cur in set(sales["revenue"]) | set(sales["refunds"]):
        sales["net"][cur] = sales["revenue"].get(cur, 0) - sales["refunds"].get(cur, 0)
    return {"downloads": dl, "trials": trials, "activations": {"by_edition": acts, "first": acts_first, "transfers": transfers}, "sales": sales}


def compact(evs):
    """Small per-bucket figures for the charts."""
    s = summarize(evs)
    d = s["downloads"]
    return {"dl_win": d["started"]["EBD-WIN"] + d["legacy_started"]["EBD-WIN"], "dl_and": d["started"]["EBD-AND"] + d["legacy_started"]["EBD-AND"],
            "legacy": sum(d["legacy_started"].values()), "opened": sum(d["opened"].values()) + d["opened_unknown"],
            "failed": sum(d["failed"].values()) + d["failed_unknown"], "unique": sum(d["unique"].values()),
            "trial_req": s["trials"]["requested"], "trial_iss": sum(s["trials"]["issued"].values()),
            "trial_act": sum(s["trials"]["activated"].values()), "act": sum(s["activations"]["first"].values()),
            "sales": sum(s["sales"]["count"].values()), "revenue": s["sales"]["revenue"], "refunds": s["sales"]["refunds"]}


# --------------------------------------------------------------------------------------------- Stripe cross-check
class StripeView:
    """Figures read directly from Stripe for a date range (cached 5 minutes): Checkout Sessions of this shop (paid /
    expired = abandoned / open) and balance transactions (gross, Stripe fee, net). Read-only."""

    def __init__(self, stripe_client, store, clock=time.time):
        self.stripe, self.store, self.clock = stripe_client, store, clock
        self._cache = {}

    def range(self, d_from, d_to, live=True):
        key = (d_from, d_to, live, int(self.clock()) // 300)
        if key in self._cache:
            return self._cache[key]
        if not getattr(self.stripe, "configured", False):
            return {"available": False, "error": "Stripe nincs beállítva"}
        gte, lt = day_start(d_from), day_start(add_days(d_to, 1))
        out = {"available": True, "from": d_from, "to": d_to, "sessions": {"paid": 0, "unpaid_complete": 0, "expired": 0, "open": 0},
               "paid_amount": {}, "discounted": 0, "balance": {}, "errors": []}
        try:
            params = {"limit": 100, "created": {"gte": gte, "lt": lt}}
            for _ in range(20):
                page = self.stripe.request("GET", "/checkout/sessions", params)
                data = page.get("data") or []
                for s in data:
                    if (s.get("metadata") or {}).get("shop") != "ebd-website" or bool(s.get("livemode")) != live:
                        continue
                    st = s.get("status")
                    if st == "complete" and s.get("payment_status") == "paid":
                        out["sessions"]["paid"] += 1
                        cur = str(s.get("currency") or "").upper()
                        out["paid_amount"][cur] = out["paid_amount"].get(cur, 0) + int(s.get("amount_total") or 0)
                        out["discounted"] += 1 if (s.get("total_details") or {}).get("amount_discount") else 0
                    elif st == "complete":
                        out["sessions"]["unpaid_complete"] += 1
                    elif st in ("expired", "open"):
                        out["sessions"][st] += 1
                if not page.get("has_more") or not data:
                    break
                params["starting_after"] = data[-1]["id"]
        except Exception as exc:                             # noqa: BLE001
            out["errors"].append("checkout_sessions: " + str(getattr(exc, "code", "") or type(exc).__name__))
        try:
            params = {"limit": 100, "created": {"gte": gte, "lt": lt}}
            for _ in range(20):
                page = self.stripe.request("GET", "/balance_transactions", params)
                data = page.get("data") or []
                for b in data:
                    if b.get("type") not in ("charge", "payment", "refund", "payment_refund", "adjustment", "stripe_fee"):
                        continue
                    cur = str(b.get("currency") or "").upper()
                    row = out["balance"].setdefault(cur, {"gross": 0, "fee": 0, "net": 0, "refunds": 0})
                    if b.get("type") in ("refund", "payment_refund"):
                        row["refunds"] += -int(b.get("amount") or 0)
                    else:
                        row["gross"] += int(b.get("amount") or 0)
                    row["fee"] += int(b.get("fee") or 0)
                    row["net"] += int(b.get("net") or 0)
                if not page.get("has_more") or not data:
                    break
                params["starting_after"] = data[-1]["id"]
        except Exception as exc:                             # noqa: BLE001
            out["errors"].append("balance_transactions: " + str(getattr(exc, "code", "") or type(exc).__name__))
        self._cache = {key: out}
        return out


# --------------------------------------------------------------------------------------------- password login (no 2FA)
class StatsAuth:
    """Password-only login for the READ-ONLY statistics page (owner request 2026-11-22: no two-factor step here).
    The licence admin keeps its 2FA. Brute-force lockout (5 / 15 min per network, 30 / hour overall), signed server-side
    session cookie (HttpOnly, Secure, SameSite=Strict), logout ends the session on the server.
      STATS_PASSWORD_HASH   pbkdf2_sha256$… (tools/stats_setup.py)
      STATS_SESSION_HOURS   default 12"""
    MAX_FAILS_PER_IP, MAX_FAILS_GLOBAL, WINDOW = 5, 30, 900

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    @property
    def configured(self):
        return bool(os.getenv("STATS_PASSWORD_HASH", "").strip() and self._key())

    def _key(self):
        k = os.getenv("STATS_SESSION_SECRET", "").strip() or os.getenv("ADMIN_SESSION_SECRET", "").strip() or os.getenv("ADMIN_TOKEN", "").strip()
        return hashlib.sha256(("ebd-stats-session|" + k).encode()).digest() if k else b""

    def _locked(self, ip_hash):
        now = int(self.clock())
        return (int(self.store.get(f"st:fail:{ip_hash}:{now // self.WINDOW}") or 0) >= self.MAX_FAILS_PER_IP
                or int(self.store.get(f"st:failg:{now // 3600}") or 0) >= self.MAX_FAILS_GLOBAL)

    def login(self, password, ip_hash):
        if not self.configured:
            raise LicenseError("not_configured", 503, "Statistics login is not configured")
        if self._locked(ip_hash):
            raise LicenseError("locked", 429, "Too many failed attempts - wait 15 minutes")
        if not admin_auth.verify_password(password, os.getenv("STATS_PASSWORD_HASH", "").strip()):
            now = int(self.clock())
            self.store.incr(f"st:fail:{ip_hash}:{now // self.WINDOW}", self.WINDOW * 2)
            self.store.incr(f"st:failg:{now // 3600}", 7200)
            raise LicenseError("bad_credentials", 401, "Wrong password")
        hours = max(1, min(72, int(os.getenv("STATS_SESSION_HOURS", "12") or 12)))
        exp = int(self.clock()) + hours * 3600
        sid = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
        self.store.set(f"st:sess:{sid}", {"exp": exp}, hours * 3600)
        return self._sign(f"{sid}.{exp}"), exp

    def _sign(self, payload):
        return payload + "." + hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()[:40]

    def valid(self, cookie):
        try:
            sid, exp, sig = (cookie or "").split(".")
        except ValueError:
            return False
        if not self._key() or not hmac.compare_digest(self._sign(f"{sid}.{exp}").rsplit(".", 1)[1], sig) or int(exp) < self.clock():
            return False
        return bool(self.store.get(f"st:sess:{sid}"))

    def logout(self, cookie):
        sid = (cookie or "").split(".")[0]
        if sid:
            self.store.set(f"st:sess:{sid}", None, 1)
