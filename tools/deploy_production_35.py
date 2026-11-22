"""Backend 3.5.0 (permanent statistics page /stats): staging test, production deploy, end-to-end statistics check, rollback.
Secrets are read from the Render API / local key files and NEVER printed.

  python tools/deploy_production_35.py staging --confirm       STATS_* env on staging, push the staging branch, wait, e2e
  python tools/deploy_production_35.py check staging|production
  python tools/deploy_production_35.py e2e staging                trial + promo end-to-end test (3.4) against 3.5.0
  python tools/deploy_production_35.py deploy --confirm        production: backup -> rollback tag -> STATS_* env -> fast-forward
                                                               main -> wait live -> regression -> statistics check
  python tools/deploy_production_35.py rollback --confirm      main back to the tag (code only; the STATS_* variables stay, unused)
The statistics check never creates customer data: it uses the owner licences (shown only with "saját licencekkel").
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path[:0] = [HERE, REPO]
import deploy_production_33 as d33          # noqa: E402
import deploy_production_34 as d34          # noqa: E402

d33.VERSION = "3.5.0"
BRANCH = "feature/stats-3.5"
TAG = "eles-3.4.0-stats-3.5-elott"
STATS_DIR = r"C:\EBD_SIGNING\stats"
OWNERS = r"C:\EBD_SIGNING\owner-licenses-server\owner-licenses.json"


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=True).stdout.strip()


def stats_env():
    path = os.path.join(STATS_DIR, "stats-env.json")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}: run tools/stats_setup.py {STATS_DIR}")
    return json.load(open(path, encoding="utf-8"))


def password():
    """Read from STATS_PASSWORD_FILE (the owner may keep the file elsewhere) - never printed."""
    path = os.environ.get("STATS_PASSWORD_FILE", "").strip() or os.path.join(STATS_DIR, "stats-password.txt")
    return open(path, encoding="utf-8").read().strip()


class Check:
    def __init__(self, name):
        self.name, (self.svc, self.base) = name, d34.SERVICES[name]
        self.results, self.cookie = [], ""

    def http(self, method, path, body=None, headers=None, cookie=True):
        h = {"User-Agent": "EBD-deploy-3.5", "Origin": "https://ebikedoctorpro.com", **({"Content-Type": "application/json"} if body is not None else {}),
             **({"Cookie": "ebd_stats=" + self.cookie} if cookie and self.cookie else {}), **(headers or {})}
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try:
            with urllib.request.build_opener(NoRedirect).open(r, timeout=120) as x:
                return x.status, {k.lower(): v for k, v in x.headers.items()}, x.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")

    def ok(self, cond, msg):
        self.results.append((bool(cond), msg)); print(("PASS " if cond else "FAIL ") + msg)
        return cond

    def report(self, owner=0, test=0):
        s, _, b = self.http("GET", f"/v1/stats/report?owner={owner}&test={test}")
        return s, (json.loads(b) if s == 200 else {})

    def run(self):
        s, _, b = self.http("GET", "/health")
        self.ok(s == 200 and json.loads(b).get("version") == d33.VERSION, f"[{self.name}] health version {json.loads(b).get('version') if s == 200 else s}")
        s, h, b = self.http("GET", "/stats")
        self.ok(s == 200 and "Statisztika" in b and "sha256-" in h.get("content-security-policy", "") and "noindex" in h.get("x-robots-tag", ""),
                f"statistics page served with strict CSP -> {s}")
        s, _, _ = self.http("GET", "/v1/stats/report", cookie=False)
        self.ok(s == 404, f"report without login refused -> {s}")
        s, _, _ = self.http("POST", "/v1/stats/login", {"password": "wrong-" + str(time.time())}, cookie=False)
        self.ok(s == 401, f"wrong password refused -> {s}")
        s, h, _ = self.http("POST", "/v1/stats/login", {"password": password()}, cookie=False)
        m = re.search(r"ebd_stats=([^;]+)", h.get("set-cookie", ""))
        self.ok(s == 200 and m and "HttpOnly" in h.get("set-cookie", "") and "Secure" in h.get("set-cookie", "") and "SameSite=Strict" in h.get("set-cookie", ""),
                f"password login -> {s}, session cookie HttpOnly/Secure/SameSite")
        if not m:
            return self.summary()
        self.cookie = m.group(1)
        s, d0 = self.report(owner=1, test=1)
        self.ok(s == 200 and set(d0.get("periods", {})) == {"today", "week", "month", "year", "all", "range"} and d0.get("stats_start"),
                f"report -> {s}, periods + stats start {d0.get('stats_start')}")
        bf = d0.get("backfill") or {}
        self.ok(bool(bf.get("at")), f"history before the start added once (backfill events {bf.get('events')})")
        allm = d0.get("periods", {}).get("all", {}).get("metrics", {})
        print("   all-time (with test + owner):", json.dumps({"downloads": sum(allm.get("downloads", {}).get("started", {}).values()) +
                                                            sum(allm.get("downloads", {}).get("legacy_started", {}).values()),
                                                            "sales": allm.get("sales", {}).get("count"), "revenue": allm.get("sales", {}).get("revenue"),
                                                            "trials": allm.get("trials", {}).get("issued"), "activations": allm.get("activations", {}).get("first")}))
        # end-to-end recording with a licence that is hidden from the customer figures by default:
        # production = an OWNER licence, staging = a TEST-mode trial issued through the real trial flow
        if self.name == "production":
            owners = json.load(open(OWNERS))
            prod, o = next(iter(owners.items()))
            key = o["license_key"]
        else:
            prod = "EBD-AND"
            t0 = d0["periods"]["today"]["metrics"]["trials"]
            email = d34.OWNER_MAIL.replace("@", "+ebdstats" + time.strftime("%m%d%H%M%S") + "@")
            s, _, _ = self.http("POST", "/v1/trial/request", {"email": email, "product": "android", "lang": "hu", "code": "EBIKE-TRIAL2", "consent": True}, cookie=False)
            from licensing import email_hash
            s2, _, b = self.http("POST", "/v1/trial/confirm?t=" + d34.E2E("staging").token(email, "EBD-AND"), cookie=False)
            key = (re.search(r"EBDA-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}", b) or [""])[0]
            s3, _, _ = self.http("POST", "/v1/license/activate", {"license_key": key, "product": "EBD-AND", "device_id": os.urandom(32).hex(),
                                                                  "device_name": "stats-check", "app_version": "e2e", "transfer": False}, cookie=False)
            t1 = self.report(test=1)[1]["periods"]["today"]["metrics"]["trials"]
            self.ok(s == 200 and s2 == 200 and s3 == 200 and t1["requested"] == t0["requested"] + 1 and
                    sum(t1["issued"].values()) == sum(t0["issued"].values()) + 1 and sum(t1["activated"].values()) == sum(t0["activated"].values()) + 1,
                    f"trial request / issue / activation counted ({t0['requested']}->{t1['requested']}, "
                    f"{sum(t0['issued'].values())}->{sum(t1['issued'].values())}, {sum(t0['activated'].values())}->{sum(t1['activated'].values())})")
        before = self.report(owner=1, test=1)[1]["periods"]["today"]["metrics"]["downloads"]["started"][prod]
        before_cust = self.report()[1]["periods"]["today"]["metrics"]["downloads"]["started"][prod]
        s, _, b = self.http("POST", "/v1/shop/download-link", {"license_key": key, "product": prod}, cookie=False)
        url = json.loads(b).get("url", "") if s == 200 else ""
        s2, h2, _ = self.http("GET", url.replace(self.base, ""), cookie=False, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) EBD-deploy-check"}) if url else (0, {}, "")
        self.ok(s == 200 and s2 == 302 and h2.get("location", "").startswith("https://"), f"owner download link {s} -> file redirect {s2}")
        time.sleep(1)
        s, d1 = self.report(owner=1, test=1)
        after = d1["periods"]["today"]["metrics"]["downloads"]["started"][prod]
        self.ok(after == before + 1, f"the download is counted at once ({prod}: {before} -> {after})")
        after_cust = self.report()[1]["periods"]["today"]["metrics"]["downloads"]["started"][prod]
        self.ok(after_cust == before_cust, f"owner download not in the customer figures ({before_cust} -> {after_cust})")
        s, _, b = self.http("GET", "/v1/stats/stripe")
        sj = json.loads(b) if s == 200 else {}
        self.ok(s == 200 and "available" in sj, f"Stripe panel -> {s}, available={sj.get('available')} errors={sj.get('errors')}")
        s, _, _ = self.http("POST", "/v1/stats/logout", {})
        s2, _ = self.report()
        self.ok(s2 == 404, f"after logout the session is gone -> {s2}")
        return self.summary()

    def summary(self):
        bad = [m for o, m in self.results if not o]
        print(f"STATS CHECK {self.name}: {len(self.results) - len(bad)}/{len(self.results)} passed" + (f" | FAILED: {bad}" if bad else ""))
        return not bad


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "check":
        sys.exit(0 if Check(a[1] if len(a) > 1 else "staging").run() else 1)
    if cmd == "e2e":                                 # the 3.4 trial + promo end-to-end test against 3.5.0
        name = a[1] if len(a) > 1 else "staging"
        sys.exit(0 if d34.E2E(name).run(wait_expiry=(name == "staging")) else 1)
    if "--confirm" not in a:
        raise SystemExit(__doc__)
    git("fetch", "-q", "origin")
    if cmd == "staging":
        svc = d34.SERVICES["staging"][0]
        d34.put_env(svc, stats_env())
        sha = git("rev-parse", BRANCH)
        git("push", "-q", "origin", f"{BRANCH}:refs/heads/{BRANCH}")
        git("push", "-q", "--force-with-lease", "origin", f"{sha}:refs/heads/backend-3.1-shop-staging")
        print("staging env STATS_* set | branch ->", sha[:7], "| deploy:", d34.wait_commit(svc, sha))
    elif cmd == "deploy":
        svc = d34.SERVICES["production"][0]
        main_sha, sha = git("rev-parse", "origin/main"), git("rev-parse", BRANCH)
        if subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", main_sha, sha]).returncode != 0:
            raise SystemExit("refused: not a fast-forward")
        if git("rev-parse", "origin/backend-3.1-shop-staging") != sha:
            raise SystemExit("refused: staging does not run the commit to deploy")
        folder = d33.backup("pre-3.5")
        if subprocess.run(["git", "-C", REPO, "rev-parse", "-q", "--verify", "refs/tags/" + TAG], capture_output=True).returncode != 0:
            git("-c", "user.name=Dukai Balint", "-c", "user.email=dukaibalint37@gmail.com", "tag", "-a", TAG, main_sha, "-m", "Production before 3.5 (rollback point)")
            git("push", "-q", "origin", TAG)
        print("rollback tag:", TAG, "->", main_sha[:7])
        d34.put_env(svc, stats_env())                                       # applies with the deploy below
        git("push", "-q", "origin", f"{sha}:refs/heads/main")              # fast-forward only
        print("main ->", sha[:7], "| deploy:", d34.wait_commit(svc, sha))
        mode = "live" if d34.env_of(svc).get("SHOP_MODE") == "live" else "off"
        reg = d33.regression(folder, mode)
        print("REGRESSION:", "OK" if reg else f"FAILED -> rollback --confirm (backup {folder})")
        print("STATS:", "OK" if Check("production").run() else "FAILED")
    elif cmd == "rollback":
        svc = d34.SERVICES["production"][0]
        tag_sha = git("rev-parse", TAG + "^{commit}")
        git("push", "-q", "--force-with-lease=main:" + git("rev-parse", "origin/main"), "origin", f"{tag_sha}:refs/heads/main")
        print("main ->", tag_sha[:7], "| deploy:", d34.wait_commit(svc, tag_sha))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
