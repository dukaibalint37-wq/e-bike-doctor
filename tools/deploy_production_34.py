"""Backend 3.4.0 (free trial EBIKE-TRIAL2 + promotion EBIKE-50): staging test, production deploy, switch-on, end-to-end
checks. Secrets are read from the Render API / local key files and NEVER printed (only variable names and booleans).

  python tools/deploy_production_34.py staging-setup            staging: test-mode coupon + TRIAL_* env (TRIAL_HOURS=0.05) + redeploy
  python tools/deploy_production_34.py e2e staging              trial + promo end-to-end against staging (incl. live expiry wait)
  python tools/deploy_production_34.py deploy --confirm         production: backup -> fast-forward main -> regression (features OFF)
  python tools/deploy_production_34.py enable --confirm         production: live coupon + TRIAL_* / STRIPE_COUPON_EBIKE50 -> redeploy
  python tools/deploy_production_34.py e2e production           trial + promo end-to-end against production (no payment: the
                                                                promo session is expired at once)
  python tools/deploy_production_34.py disable --confirm        emergency stop: TRIAL_ENABLED=0, coupon variable removed

Test e-mails go to plus-aliases of the owner's address (they arrive in the owner's own mailbox).
"""
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path[:0] = [HERE, REPO]
import deploy_production_33 as d33          # noqa: E402  (render(), backup(), regression(), wait_deploy(), git())

d33.VERSION = "3.4.0"
SERVICES = {"production": ("srv-dbfh3jss728c73ak0hc0", "https://e-bike-doctor.onrender.com"),
            "staging": ("srv-dbrjf2oae00c73frigrg", "https://e-bike-doctor-staging.onrender.com")}
SECRET_DIR = r"C:\EBD_SIGNING\trial"
OWNER_MAIL = "dukaibalint37@gmail.com"
COUPON = {"percent_off": "50", "duration": "once", "name": "EBIKE-50 (-50%)", "metadata[code]": "EBIKE-50", "metadata[shop]": "ebd-website"}


def env_of(svc):
    return {x["envVar"]["key"]: x["envVar"]["value"] for x in d33.render("GET", f"/services/{svc}/env-vars?limit=100")}


def put_env(svc, values):
    for k, v in values.items():
        d33.render("PUT", f"/services/{svc}/env-vars/{k}", {"value": v})


def redeploy(svc):
    last = d33.render("GET", f"/services/{svc}/deploys?limit=1")[0]["deploy"]["id"]
    d33.render("POST", f"/services/{svc}/deploys", {})
    for _ in range(80):
        time.sleep(15)
        d = d33.render("GET", f"/services/{svc}/deploys?limit=1")[0]["deploy"]
        if d["id"] != last and d["status"] in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            return d["status"]
    return "timeout"


def wait_commit(svc, commit):
    for _ in range(80):
        time.sleep(15)
        d = d33.render("GET", f"/services/{svc}/deploys?limit=1")[0]["deploy"]
        if d["commit"]["id"].startswith(commit[:7]) and d["status"] in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            return d["status"]
    return "timeout"


def trial_secret(name):
    """Per-environment link-signing secret, kept locally (so the e2e test can follow the e-mail link) and on Render."""
    os.makedirs(SECRET_DIR, exist_ok=True)
    path = os.path.join(SECRET_DIR, f"trial-secret-{name}.txt")
    if not os.path.exists(path):
        with open(path, "x", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(48))
    return open(path, encoding="utf-8").read().strip()


def stripe_call(key, method, path, params=None):
    data = urllib.parse.urlencode(params or {}).encode() if method == "POST" else None
    url = "https://api.stripe.com/v1" + path + ("?" + urllib.parse.urlencode(params) if method == "GET" and params else "")
    r = urllib.request.Request(url, data=data, method=method, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def ensure_coupon(key):
    """Reuses an existing valid EBIKE-50 coupon (metadata code) or creates it. Returns the coupon id (not secret)."""
    s, d = stripe_call(key, "GET", "/coupons", {"limit": 100})
    if s != 200:
        raise SystemExit(f"coupon list refused: HTTP {s} {d.get('error', {}).get('message', '')[:120]}")
    for c in d.get("data", []):
        if (c.get("metadata") or {}).get("code") == "EBIKE-50" and c.get("valid") and c.get("percent_off") == 50 and c.get("duration") == "once":
            print(f"coupon exists: {c['id']} ({c.get('name')}, livemode={c.get('livemode')})")
            return c["id"]
    s, c = stripe_call(key, "POST", "/coupons", COUPON)
    if s != 200:
        raise SystemExit(f"coupon create refused: HTTP {s} {c.get('error', {}).get('message', '')[:160]}")
    print(f"coupon created: {c['id']} ({c.get('name')}, {c.get('percent_off')}% {c.get('duration')}, livemode={c.get('livemode')})")
    return c["id"]


def live_key():
    kv = d33.live_keys()
    if not kv:
        raise SystemExit("live key file missing")
    return kv["STRIPE_SECRET_KEY"]


# ------------------------------------------------------------------------------------------------- end-to-end
class E2E:
    def __init__(self, name):
        self.name, (self.svc, self.base) = name, SERVICES[name]
        self.results = []

    def http(self, method, path, body=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None and not raw else (body if raw else None)
        h = {"User-Agent": "EBD-deploy-3.4", "Origin": "https://ebikedoctorpro.com", **({"Content-Type": "application/json"} if body is not None and not raw else {}), **(headers or {})}
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=120) as x:
                return x.status, {k.lower(): v for k, v in x.headers.items()}, x.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")

    def ok(self, cond, msg):
        self.results.append((bool(cond), msg))
        print(("PASS " if cond else "FAIL ") + msg)
        return cond

    def token(self, email, product):
        os.environ["TRIAL_SECRET"] = trial_secret(self.name)
        import trial
        from licensing import email_hash
        return trial.TrialService(None, None, None, None).token(email_hash(email), product)

    def lease_payload(self, lease):
        body = lease[6:].split(".")[0]
        return json.loads(__import__("base64").urlsafe_b64decode(body + "=" * (-len(body) % 4)))

    def run(self, wait_expiry):
        s, _, b = self.http("GET", "/health")
        h = json.loads(b) if s == 200 else {}
        self.ok(h.get("version") == "3.4.0", f"[{self.name}] health version {h.get('version')}")
        stamp = time.strftime("%m%d%H%M%S")
        email = OWNER_MAIL.replace("@", f"+ebdtrial{self.name[:4]}{stamp}@")
        dev_a, dev_b = secrets.token_hex(32), secrets.token_hex(32)
        # 1. request (button / code), always "check_email"
        s, _, b = self.http("POST", "/v1/trial/request", {"email": email, "product": "android", "lang": "hu", "code": "EBIKE-TRIAL2", "consent": True})
        self.ok(s == 200 and '"check_email"' in b, f"trial request -> {s}")
        s, hd, _ = self.http("POST", "/v1/trial/request", {"email": email, "product": "android", "lang": "hu", "code": "WRONG-CODE", "consent": True})
        self.ok(s == 422 and hd.get("x-license-error") == "invalid_code", f"wrong trial code refused -> {s} {hd.get('x-license-error')}")
        # 2. e-mail link: GET = button only, POST = licence
        t = self.token(email, "EBD-AND")
        s, _, b = self.http("GET", "/v1/trial/confirm?t=" + t)
        self.ok(s == 200 and "<form" in b, f"confirm link shows the start button (GET issues nothing) -> {s}")
        s, _, b = self.http("POST", "/v1/trial/confirm?t=" + t)
        m = re.search(r"EBDA-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", b)
        if not self.ok(s == 200 and m, f"trial licence issued on confirm -> {s}"):
            return self.summary()
        key = m.group(0)
        s, _, b2 = self.http("POST", "/v1/trial/confirm?t=" + t)
        self.ok(key in b2, "second click shows the same key (no second licence)")
        s, _, b = self.http("GET", "/v1/trial/confirm?t=" + t[:-3] + "xyz")
        self.ok(s == 404, f"tampered link refused -> {s}")
        # 3. installer for the entitled user
        s, _, b = self.http("POST", "/v1/shop/download-link", {"license_key": key, "product": "EBD-AND"})
        url = json.loads(b).get("url", "") if s == 200 else ""
        self.ok(s == 200 and "/v1/shop/download?t=" in url, f"download link for the trial key -> {s}")
        if url:
            r = urllib.request.Request(url, method="GET", headers={"User-Agent": "EBD-deploy-3.4"})
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k): return None
            try:
                urllib.request.build_opener(NoRedirect).open(r, timeout=120); st, loc = 200, ""
            except urllib.error.HTTPError as e:
                st, loc = e.code, e.headers.get("Location", "")
            self.ok(st == 302 and loc.startswith("https://"), f"download redirects to the private bucket (pre-signed) -> {st}")
        # 4. activation starts the window; lease = TRIAL, ends with the trial
        t0 = int(time.time())
        s, hd, b = self.http("POST", "/v1/license/activate", {"license_key": key, "product": "EBD-AND", "device_id": dev_a, "device_name": "e2e", "app_version": "e2e", "transfer": False})
        lease = json.loads(b).get("lease", "") if s == 200 else ""
        p = self.lease_payload(lease) if lease else {}
        hours = float(env_of(self.svc).get("TRIAL_HOURS", "48") or 48)
        self.ok(s == 200 and p.get("ed") == "TRIAL" and abs(p.get("exp", 0) - (t0 + hours * 3600)) < 120,
                f"activation -> {s}, edition {p.get('ed')}, ends in {round((p.get('exp', t0) - t0) / 3600, 2)} h (expected {hours})")
        s, _, b = self.http("POST", "/v1/license/refresh", {"license_key": key, "product": "EBD-AND", "device_id": dev_a, "app_version": "e2e"})
        p2 = self.lease_payload(json.loads(b).get("lease", "")) if s == 200 else {}
        self.ok(s == 200 and p2.get("exp") == p.get("exp"), f"refresh keeps the same end (not extended) -> {s}")
        for transfer in (False, True):
            s, hd, _ = self.http("POST", "/v1/license/activate", {"license_key": key, "product": "EBD-AND", "device_id": dev_b, "device_name": "e2e-2", "app_version": "e2e", "transfer": transfer})
            self.ok(s == 409 and hd.get("x-license-error") == "trial_device", f"other device refused (transfer={transfer}) -> {s} {hd.get('x-license-error')}")
        # 5. re-request protection: same e-mail → no new trial; new e-mail on the same device → trial_used
        email2 = email.replace("+ebdtrial", "+ebdtrial2x")
        self.http("POST", "/v1/trial/request", {"email": email2, "product": "android", "lang": "en", "code": "", "consent": True})
        s, _, b = self.http("POST", "/v1/trial/confirm?t=" + self.token(email2, "EBD-AND"))
        m2 = re.search(r"EBDA-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", b)
        if self.ok(s == 200 and m2, f"second e-mail gets its own trial key -> {s}"):
            s, hd, _ = self.http("POST", "/v1/license/activate", {"license_key": m2.group(0), "product": "EBD-AND", "device_id": dev_a, "device_name": "e2e", "app_version": "e2e", "transfer": False})
            self.ok(s == 409 and hd.get("x-license-error") == "trial_used", f"…but not on a device that already had a trial -> {s} {hd.get('x-license-error')}")
        s, _, b = self.http("POST", "/v1/trial/request", {"email": email, "product": "windows", "lang": "hu", "code": "", "consent": True})
        self.ok(s == 200, f"same e-mail again -> generic answer {s} (no new licence; the mail repeats the running key)")
        # 6. promotion
        s, _, b = self.http("POST", "/v1/shop/promo", {"code": "ebike-50", "product": "android"})
        q = json.loads(b) if s == 200 else {}
        self.ok(s == 200 and q.get("total_cents") == 250 and q.get("original_cents") == 500 and "coupon" not in q, f"promo quote 5.00 -> {q.get('total_cents')} cents ({s})")
        s, _, b = self.http("POST", "/v1/shop/promo", {"code": "ebike-trial2", "product": "android"})
        self.ok(s == 200 and json.loads(b).get("kind") == "trial", "trial code in the promo field -> trial form")
        s, hd, _ = self.http("POST", "/v1/shop/promo", {"code": "FREE100", "product": "android"})
        self.ok(s == 422, f"unknown promo refused -> {s}")
        s, _, b = self.http("POST", "/v1/shop/stripe/checkout", {"product": "android", "lang": "hu", "consent": True, "promo": "EBIKE-50"})
        url = json.loads(b).get("url", "") if s == 200 else ""
        sid = (re.search(r"(cs_(?:test|live)_[A-Za-z0-9]+)", url) or [None, ""])[1]
        self.ok(s == 200 and url.startswith("https://checkout.stripe.com/") and sid, f"discounted Stripe Checkout session opened -> {s}")
        if sid:
            key_ = live_key() if sid.startswith("cs_live_") else env_of(self.svc)["STRIPE_SECRET_KEY"]
            st, ss = stripe_call(key_, "GET", f"/checkout/sessions/{sid}")
            td = ss.get("total_details") or {}
            self.ok(st == 200 and ss.get("amount_subtotal") == 500 and td.get("amount_discount") == 250 and ss.get("amount_total") == 250
                    and (ss.get("metadata") or {}).get("promo") == "EBIKE-50",
                    f"Stripe shows 5.00 - 2.50 = {ss.get('amount_total')} {ss.get('currency')} (discount line before paying)")
            st, ex = stripe_call(key_, "POST", f"/checkout/sessions/{sid}/expire")
            self.ok(st == 200 and ex.get("status") == "expired", f"test session expired at once (no payment possible) -> {st}")
        s, hd, _ = self.http("POST", "/v1/shop/stripe/checkout", {"product": "android", "lang": "hu", "consent": True, "promo": "EBIKE-90"})
        self.ok(s == 422 and hd.get("x-license-error") == "invalid_promo", f"checkout with unknown code refused (no full-price session) -> {s}")
        # 7. expiry (staging: short window → real wait)
        if wait_expiry:
            left = p.get("exp", 0) - int(time.time()) + 20
            print(f"waiting {max(0, left)} s for the trial window to end …")
            time.sleep(max(0, left))
            s, hd, _ = self.http("POST", "/v1/license/refresh", {"license_key": key, "product": "EBD-AND", "device_id": dev_a, "app_version": "e2e"})
            self.ok(s == 403 and hd.get("x-license-error") == "trial_expired", f"after the window: refresh refused -> {s} {hd.get('x-license-error')}")
            s, hd, _ = self.http("POST", "/v1/shop/download-link", {"license_key": key, "product": "EBD-AND"})
            self.ok(s == 403 and hd.get("x-license-error") == "trial_expired", f"after the window: download refused -> {s}")
            s, hd, _ = self.http("POST", "/v1/license/activate", {"license_key": key, "product": "EBD-AND", "device_id": dev_a, "device_name": "reinstall", "app_version": "e2e", "transfer": False})
            self.ok(s == 403 and hd.get("x-license-error") == "trial_expired", f"reinstall + same key cannot restart it -> {s}")
        return self.summary()

    def summary(self):
        bad = [m for o, m in self.results if not o]
        print(f"E2E {self.name}: {len(self.results) - len(bad)}/{len(self.results)} passed" + (f" | FAILED: {bad}" if bad else ""))
        return not bad


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "staging-setup":
        svc = SERVICES["staging"][0]
        e = env_of(svc)
        sk = e.get("STRIPE_SECRET_KEY", "")
        if not sk.startswith("sk_test_"):
            raise SystemExit("staging must use a TEST Stripe key")
        coupon = ensure_coupon(sk)
        put_env(svc, {"TRIAL_ENABLED": "1", "TRIAL_SECRET": trial_secret("staging"), "TRIAL_HOURS": "0.05", "STRIPE_COUPON_EBIKE50": coupon,
                       "TRIAL_PER_IP_PER_DAY": "100"})                 # staging only: repeated e2e runs from one network
        print("staging env set: TRIAL_ENABLED, TRIAL_SECRET, TRIAL_HOURS=0.05, STRIPE_COUPON_EBIKE50 | redeploy:", redeploy(svc))
    elif cmd == "e2e":
        name = a[1] if len(a) > 1 else "staging"
        sys.exit(0 if E2E(name).run(wait_expiry=(name == "staging")) else 1)
    elif cmd in ("deploy", "enable", "disable"):
        if "--confirm" not in a:
            raise SystemExit("refused: add --confirm")
        svc, base = SERVICES["production"]
        if cmd == "deploy":
            folder = d33.backup("pre-3.4")
            # the signed manifests (2.0.1 / 3.0.2) and the bucket objects go live with the same deploy (env applies on deploy)
            put_env(svc, {"DOWNLOAD_OBJECT_EBD_WIN": "windows/EBikeDoctorPRO-Setup-2.0.1.exe",
                          "DOWNLOAD_OBJECT_EBD_AND": "android/EBikeDoctorPRO-3.0.2-web.apk"})
            d33.git("fetch", "-q", "origin")
            main_sha, target = d33.git("rev-parse", "origin/main"), d33.git("rev-parse", "origin/feature/trial-promo")
            if os.system(f'git -C "{REPO}" merge-base --is-ancestor {main_sha} {target}') != 0:
                raise SystemExit("refused: not a fast-forward")
            tag = "eles-3.3.0-3.4-elott"
            if os.system(f'git -C "{REPO}" rev-parse -q --verify refs/tags/{tag} >NUL 2>&1') != 0:
                d33.git("-c", "user.name=Dukai Balint", "-c", "user.email=dukaibalint37@gmail.com", "tag", "-a", tag, main_sha, "-m", "Production before 3.4 (rollback point)")
                d33.git("push", "-q", "origin", tag)
            d33.git("push", "-q", "origin", f"{target}:refs/heads/main")       # fast-forward only
            print("deploy:", wait_commit(svc, target))
            mode = "live" if env_of(svc).get("SHOP_MODE") == "live" else "off"
            print("REGRESSION:", "OK" if d33.regression(folder, mode) else f"FAILED -> rollback {folder}")
        elif cmd == "enable":
            folder = d33.backup("pre-enable-3.4")
            coupon = ensure_coupon(live_key())
            put_env(svc, {"TRIAL_ENABLED": "1", "TRIAL_SECRET": trial_secret("production"), "TRIAL_HOURS": "48", "STRIPE_COUPON_EBIKE50": coupon})
            print("production env set: TRIAL_ENABLED=1, TRIAL_SECRET, TRIAL_HOURS=48, STRIPE_COUPON_EBIKE50 | redeploy:", redeploy(svc))
            mode = "live" if env_of(svc).get("SHOP_MODE") == "live" else "off"
            print("REGRESSION:", "OK" if d33.regression(folder, mode) else f"FAILED -> disable --confirm (backup {folder})")
        else:
            put_env(svc, {"TRIAL_ENABLED": "0", "STRIPE_COUPON_EBIKE50": ""})
            print("trial + promo OFF | redeploy:", redeploy(svc))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
