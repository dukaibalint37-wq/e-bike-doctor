"""Cookieless, aggregate website visit statistics (3.2). No cookies, no IP address, no user ID, no fingerprinting:
only daily counters per section / language / page / referrer domain. The website skips the beacon when the browser sends
Do-Not-Track or Global Privacy Control. Bots are filtered by user agent."""
import re
import threading
import time
from collections import deque

SECTIONS = ("home", "android", "windows", "shop", "hardware", "kb", "codes", "displays", "support", "legal", "thanks", "other")
LANGS = ("hu", "de", "en")
KEEP = 400 * 86_400
BOT = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|monitor|curl|wget|python|java/|go-http|httpclient", re.I)
_rate, _lock = {}, threading.Lock()
SECTION_OF = [  # first path segment after /<lang>/ -> section
    ("android-pro", "android"), ("windows-pro", "windows"), ("webaruhaz", "shop"), ("shop", "shop"), ("smart-hardware", "hardware"),
    ("tudasbazis", "kb"), ("wissensdatenbank", "kb"), ("knowledge-base", "kb"), ("hibakodok", "codes"), ("fehlercodes", "codes"),
    ("fault-codes", "codes"), ("kijelzok-p-menuk", "displays"), ("displays-p-menues", "displays"), ("displays-p-menus", "displays"),
    ("akkumulator-bms", "kb"), ("akku-bms", "kb"), ("battery-bms", "kb"),
    ("letoltesek", "support"), ("downloads", "support"), ("frissitesek", "support"), ("updates", "support"), ("licenc-aktivalas", "support"),
    ("lizenz-aktivierung", "support"), ("licence-activation", "support"), ("gyik", "support"), ("faq", "support"), ("kapcsolat", "support"),
    ("kontakt", "support"), ("contact", "support"), ("bemutatok", "support"), ("demos", "support"), ("walkthroughs", "support"),
    ("adatvedelem", "legal"), ("datenschutz", "legal"), ("privacy", "legal"), ("aszf", "legal"), ("agb", "legal"), ("terms", "legal"),
    ("vasarlas-visszaterites", "legal"), ("kauf-erstattung", "legal"), ("purchase-refunds", "legal"),
    ("koszonjuk", "thanks"), ("danke", "thanks"), ("thank-you", "thanks")]


def _day(t):
    return time.strftime("%Y%m%d", time.gmtime(t))


def classify(path):
    parts = [p for p in (path or "/").split("?")[0].split("#")[0].split("/") if p]
    lang = parts[0] if parts and parts[0] in LANGS else ""
    rest = parts[1:] if lang else parts
    if not rest:
        return lang, "home"
    return lang, dict(SECTION_OF).get(rest[0], "other")


def _clean_path(p):
    p = (p or "/").split("?")[0].split("#")[0][:160]
    return p if re.fullmatch(r"/[A-Za-z0-9/_\-.]*", p) else "/"


def _clean_ref(r):
    r = (r or "").lower().strip()[:120]
    return r if re.fullmatch(r"[a-z0-9.-]{1,120}", r) else ""


class Visits:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def _soft_limit(self, client_key):
        """Per-process flood guard (the key is NOT stored anywhere)."""
        now = time.monotonic()
        with _lock:
            q = _rate.setdefault(client_key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= 60:
                return False
            q.append(now)
            if len(_rate) > 5000:
                _rate.clear()
        return True

    def hit(self, path, ref_host, user_agent, client_key, own_host=""):
        if not user_agent or BOT.search(user_agent) or not self._soft_limit(client_key):
            return False
        path = _clean_path(path)
        lang, section = classify(path)
        d = _day(self.clock())
        self.store.incr(f"vis:{d}:total", KEEP)
        self.store.incr(f"vis:{d}:s:{section}", KEEP)
        if lang:
            self.store.incr(f"vis:{d}:l:{lang}", KEEP)
        self._bump(f"vis:{d}:pages", path, 300)
        ref = _clean_ref(ref_host)
        if ref and ref != own_host:
            self._bump(f"vis:{d}:refs", ref, 100)
        return True

    def _bump(self, key, item, cap):
        """Approximate top-N counter in one JSON value (a lost update under heavy concurrency only undercounts)."""
        m = self.store.get(key) or {}
        m = m if isinstance(m, dict) else {}
        if item not in m and len(m) >= cap:
            low = min(m, key=m.get)
            m.pop(low)
        m[item] = int(m.get(item, 0)) + 1
        self.store.set(key, m, KEEP)

    def report(self, days=30):
        from store import mget
        out, pages, refs = [], {}, {}
        now = self.clock()
        dlist = [_day(now - i * 86_400) for i in range(days)]
        per = ["total"] + [f"s:{s}" for s in SECTIONS] + [f"l:{l}" for l in LANGS] + ["pages", "refs"]
        vals = mget(self.store, [f"vis:{d}:{p}" for d in dlist for p in per])
        for i, d in enumerate(dlist):
            v = dict(zip(per, vals[i * len(per):(i + 1) * len(per)]))
            row = {"day": d, "total": int(v["total"] or 0), "sections": {s: int(v[f"s:{s}"] or 0) for s in SECTIONS},
                   "langs": {l: int(v[f"l:{l}"] or 0) for l in LANGS}}
            out.append(row)
            for src, acc in ((v["pages"], pages), (v["refs"], refs)):
                for k, c in (src if isinstance(src, dict) else {}).items():
                    acc[k] = acc.get(k, 0) + int(c)
        top = lambda m, n: [{"k": k, "n": v} for k, v in sorted(m.items(), key=lambda x: -x[1])[:n]]
        return {"days": out, "top_pages": top(pages, 25), "top_referrers": top(refs, 15),
                "total_30d": sum(r["total"] for r in out[:30]), "total_7d": sum(r["total"] for r in out[:7])}
