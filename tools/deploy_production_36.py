"""Backend 3.6.0 (purchase funnel): staging deploy + funnel end-to-end check, production deploy (after the owner's approval),
history classification, rollback. Secrets are never printed.

  python tools/deploy_production_36.py staging --confirm
  python tools/deploy_production_36.py check staging|production          statistics check (3.5) + funnel endpoint check
  python tools/deploy_production_36.py funnel-e2e staging                REAL Stripe TEST-mode checkouts on staging: declined card
                                                                         (proven failure + reason), paid with coupon, open, own
                                                                         tool, API-expired -> every step counted correctly
  python tools/deploy_production_36.py classify production <review.json> --confirm   evidence-based history classification
  python tools/deploy_production_36.py deploy --confirm                  backup -> tag -> fast-forward main -> regression -> checks
  python tools/deploy_production_36.py rollback --confirm
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path[:0] = [HERE, REPO]
import deploy_production_33 as d33          # noqa: E402
import deploy_production_34 as d34          # noqa: E402
import deploy_production_35 as d35          # noqa: E402

d33.VERSION = "3.6.0"
BRANCH = "feature/funnel-3.6"
TAG = "eles-3.5.0-funnel-3.6-elott"
SCRATCH_CDP = os.environ.get("EBD_CDP_SCRIPT", "")          # cdp_checkout.py (headless Edge, Stripe TEST mode only)


class FunnelCheck(d35.Check):
    def login(self):
        s, h, _ = self.http("POST", "/v1/stats/login", {"password": d35.password()}, cookie=False)
        m = re.search(r"ebd_stats=([^;]+)", h.get("set-cookie", ""))
        self.cookie = m.group(1) if m else ""
        return s == 200 and m

    def funnel(self, own=0):
        s, _, b = self.http("GET", f"/v1/stats/funnel?own={own}&refresh=1")
        return s, (json.loads(b) if s == 200 else {})

    def endpoint(self):
        self.ok(self.login(), f"[{self.name}] statistics login")
        s, f = self.funnel()
        self.ok(s == 200 and set(f.get("periods", {})) == {"today", "week", "month", "year", "all", "range"} and not f.get("stripe_errors"),
                f"funnel report -> {s}, Stripe errors: {f.get('stripe_errors')}")
        s2, _, _ = self.http("GET", "/v1/stats/funnel", cookie=False)
        self.ok(s2 == 404, f"funnel without login -> {s2}")
        return f

    def e2e(self):
        assert self.name == "staging", "real checkouts only on staging (Stripe TEST mode)"
        f0 = self.endpoint()
        t0 = f0["periods"]["today"]["metrics"]
        sk = d34.env_of(self.svc)["STRIPE_SECRET_KEY"]
        assert sk.startswith("sk_test_"), "staging must use a TEST key"
        browser = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EBD funnel check"

        def checkout(product, promo, ua):
            s, _, b = self.http("POST", "/v1/shop/stripe/checkout", {"product": product, "lang": "hu", "consent": True, "promo": promo},
                                cookie=False, headers={"User-Agent": ua})
            return (re.search(r"(cs_test_[A-Za-z0-9]+)", b) or [None, ""])[1] if s == 200 else ""
        sid_open = checkout("android", "EBIKE-50", browser)          # a visitor opens the page and leaves it open
        sid_own = checkout("windows", "", "EBD-deploy-3.6")          # our own tool
        sid_exp = checkout("windows", "", browser)                   # later expired by an API call (= test)
        self.ok(all((sid_open, sid_own, sid_exp)), "three staging checkout sessions opened")
        st, ex = d34.stripe_call(sk, "POST", f"/checkout/sessions/{sid_exp}/expire")
        self.ok(st == 200, f"one session expired by API -> {st}")
        paid_ok = failed_ok = False
        if SCRATCH_CDP and os.path.exists(SCRATCH_CDP):
            env = dict(os.environ, CDP_UA=browser)
            py = sys.executable
            r1 = subprocess.run([py, SCRATCH_CDP, "hu", "", "--pay", "--card=4000000000000002"], capture_output=True, text=True, env=env, timeout=400)
            failed_ok = "SESSION cs_test_" in r1.stdout or "errors on page" in r1.stdout
            print("   declined-card run:", "ok" if failed_ok else r1.stdout[-300:])
            r2 = subprocess.run([py, SCRATCH_CDP, "de", "EBIKE-50", "--pay"], capture_output=True, text=True, env=env, timeout=400)
            paid_ok = "redirected to the thank-you page: True" in r2.stdout
            print("   paid-with-coupon run:", "ok" if paid_ok else r2.stdout[-300:])
        time.sleep(8)
        s, f1 = self.funnel()
        t1 = f1["periods"]["today"]["metrics"]
        d = lambda k: sum(t1[k].values()) - sum(t0[k].values())
        self.ok(d("start_requests") == 4 if SCRATCH_CDP else d("start_requests") == 2, f"purchase starts counted (+{d('start_requests')}, own tool excluded)")
        self.ok(t1["start_unique_daily"] >= t0["start_unique_daily"], f"unique visitors (daily) {t0['start_unique_daily']} -> {t1['start_unique_daily']}")
        self.ok(t1["own"]["sessions"] - t0["own"]["sessions"] == 2, f"own tests apart: +{t1['own']['sessions'] - t0['own']['sessions']} ({t1['own']['by_reason']})")
        self.ok(d("open") >= 1, f"open session counted (+{d('open')})")
        self.ok(sum(t1["coupon_reached"].values()) > sum(t0["coupon_reached"].values()), "coupon page counted")
        if SCRATCH_CDP:
            self.ok(failed_ok and d("failed") == 1 and t1["reasons"], f"declined card = PROVEN failure (+{d('failed')}), reason {t1['reasons']}")
            self.ok(paid_ok and d("paid") == 1 and sum(t1["coupon_paid"].values()) - sum(t0["coupon_paid"].values()) == 1,
                    f"paid with EBIKE-50 (+{d('paid')}, coupon +{sum(t1['coupon_paid'].values()) - sum(t0['coupon_paid'].values())})")
        listed = {x["sid"] for x in f1["sessions"]}
        self.ok(sid_open[-8:] in listed, "session list shows the new session")
        return self.summary()


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "check":
        name = a[1] if len(a) > 1 else "staging"
        ok1 = d35.Check(name).run()
        fc = FunnelCheck(name); fc.endpoint(); ok2 = fc.summary()
        sys.exit(0 if ok1 and ok2 else 1)
    if cmd == "funnel-e2e":
        sys.exit(0 if FunnelCheck(a[1] if len(a) > 1 else "staging").e2e() else 1)
    if "--confirm" not in a:
        raise SystemExit(__doc__)
    git = d35.git
    git("fetch", "-q", "origin")
    if cmd == "staging":
        svc = d34.SERVICES["staging"][0]
        sha = git("rev-parse", BRANCH)
        git("push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}")
        git("push", "-q", "--force-with-lease", "origin", f"{sha}:refs/heads/backend-3.1-shop-staging")
        print("staging branch ->", sha[:7], "| deploy:", d34.wait_commit(svc, sha))
    elif cmd == "classify":
        name, path = a[1], a[2]
        items = json.load(open(path, encoding="utf-8"))
        svc, base = d34.SERVICES[name]
        token = d34.env_of(svc).get("ADMIN_TOKEN", "")
        c = d35.Check(name)
        s, _, b = c.http("POST", "/v1/admin/funnel/classify", {"items": [{"sid": i["sid"], "cls": i["cls"]} for i in items]}, cookie=False,
                         headers={"X-Admin-Token": token})
        print("classify ->", s, b[:120])
    elif cmd == "deploy":
        svc = d34.SERVICES["production"][0]
        main_sha, sha = git("rev-parse", "origin/main"), git("rev-parse", BRANCH)
        if subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", main_sha, sha]).returncode != 0:
            raise SystemExit("refused: not a fast-forward")
        if git("rev-parse", "origin/backend-3.1-shop-staging") != sha:
            raise SystemExit("refused: staging does not run the commit to deploy")
        folder = d33.backup("pre-3.6")
        if subprocess.run(["git", "-C", REPO, "rev-parse", "-q", "--verify", "refs/tags/" + TAG], capture_output=True).returncode != 0:
            git("-c", "user.name=Dukai Balint", "-c", "user.email=dukaibalint37@gmail.com", "tag", "-a", TAG, main_sha, "-m", "Production before 3.6 (rollback point)")
            git("push", "-q", "origin", TAG)
        print("rollback tag:", TAG, "->", main_sha[:7])
        git("push", "-q", "origin", f"{sha}:refs/heads/main")
        print("main ->", sha[:7], "| deploy:", d34.wait_commit(svc, sha))
        mode = "live" if d34.env_of(svc).get("SHOP_MODE") == "live" else "off"
        print("REGRESSION:", "OK" if d33.regression(folder, mode) else f"FAILED -> rollback --confirm (backup {folder})")
        ok1 = d35.Check("production").run()
        fc = FunnelCheck("production"); fc.endpoint()
        print("CHECKS:", "OK" if ok1 and fc.summary() else "FAILED")
    elif cmd == "rollback":
        svc = d34.SERVICES["production"][0]
        tag_sha = git("rev-parse", TAG + "^{commit}")
        git("push", "-q", "--force-with-lease=main:" + git("rev-parse", "origin/main"), "origin", f"{tag_sha}:refs/heads/main")
        print("main ->", tag_sha[:7], "| deploy:", d34.wait_commit(svc, tag_sha))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
