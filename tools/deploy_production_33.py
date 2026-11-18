"""Production 3.2.0 -> 3.3.0 (Stripe) on Render srv-dbfh3jss728c73ak0hc0, in SEPARATE steps. Secrets are read from
C:\\EBD_SIGNING and the Render API, compared by equality and NEVER printed.

  python tools/deploy_production_33.py plan                     READ-ONLY: production state, git fast-forward, live Stripe key file
                                                                and live Stripe account / webhook check (if the key file exists)
  python tools/deploy_production_33.py backup [--count-only]    new folder C:\\EBD_SIGNING\\prod-backup-<time>-manual (never overwritten):
                                                                Render service/env/deploys, git main + source zip, Upstash export
  python tools/deploy_production_33.py deploy --confirm         STEP 1 - code only, NO sales: backup, rollback tag, fast-forward main
                                                                to feature/stripe-checkout, regression. SHOP_MODE stays "test", no
                                                                Stripe key -> /v1/shop/stripe/checkout answers 503 (sales off).
  python tools/deploy_production_33.py activate --confirm       STEP 2 - LIVE SALES, ONLY with the owner's separate approval:
                                                                live keys from the key file, SHOP_MODE=live, redeploy, checks
  python tools/deploy_production_33.py deactivate --confirm     emergency stop: SHOP_MODE=test (live key no longer matches -> 503)
  python tools/deploy_production_33.py rollback <backup-folder> --confirm
                                                                main back to the tag eles-3.2.0-3.3-elott + Render env from the backup

Live key file (created by the owner, KEY=VALUE lines):  C:\\EBD_SIGNING\\stripe-live\\stripe-live.env
  STRIPE_SECRET_KEY=sk_live_...   (or a restricted rk_live_ key: Checkout Sessions write, Customers write, Charges/Refunds/Disputes read)
  STRIPE_PUBLISHABLE_KEY=pk_live_...
  STRIPE_WEBHOOK_SECRET=whsec_... (signing secret of the LIVE endpoint https://e-bike-doctor.onrender.com/v1/shop/stripe/webhook)
"""
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request

SVC, PROD = "srv-dbfh3jss728c73ak0hc0", "https://e-bike-doctor.onrender.com"
GIT = r"C:\Program Files\Git\cmd\git.exe"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCH, TAG = "feature/stripe-checkout", "eles-3.2.0-3.3-elott"
VERSION = "3.3.0"
KEYFILE = r"C:\EBD_SIGNING\stripe-live\stripe-live.env"
WEBHOOK_URL = PROD + "/v1/shop/stripe/webhook"
EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed",
          "charge.refunded", "charge.refund.updated", "charge.dispute.created", "charge.dispute.closed"}
KEY = os.environ.get("RENDER_API_KEY", "")
UA = {"User-Agent": "EBD-deploy-3.3"}
sys.path.insert(0, REPO)


def render(method, path, body=None):
    if not KEY:
        sys.exit("RENDER_API_KEY is not set")
    r = urllib.request.Request("https://api.render.com/v1" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": "Bearer " + KEY, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as x:
        t = x.read().decode()
        return json.loads(t) if t else None


def env():
    return {x["envVar"]["key"]: x["envVar"]["value"] for x in render("GET", f"/services/{SVC}/env-vars?limit=100")}


def git(*a):
    return subprocess.run([GIT, "-C", REPO, *a], capture_output=True, text=True, check=True).stdout.strip()


def http(method, path, body=None, headers=None):
    r = urllib.request.Request(PROD + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={**UA, "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=120) as x:
            return x.status, dict(x.headers), x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def health():
    s, _, b = http("GET", "/health")
    return json.loads(b) if s == 200 else {"status": s}


def live_keys():
    if not os.path.exists(KEYFILE):
        return None
    kv = {}
    for line in open(KEYFILE, encoding="utf-8-sig"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def check_keys(kv):
    """Formats + the live Stripe account + the live webhook endpoint (read-only Stripe calls). Returns (ok, messages)."""
    import stripe_pay
    out = []
    sk, pk, wh = kv.get("STRIPE_SECRET_KEY", ""), kv.get("STRIPE_PUBLISHABLE_KEY", ""), kv.get("STRIPE_WEBHOOK_SECRET", "")
    out.append((stripe_pay.key_mode(sk) == "live", "secret key is a LIVE key (sk_live_/rk_live_)"))
    out.append((stripe_pay.key_mode(pk) == "live", "publishable key is a LIVE key (pk_live_)"))
    out.append((wh.startswith("whsec_"), "webhook signing secret present (whsec_)"))
    if stripe_pay.key_mode(sk) == "live":
        c = stripe_pay.StripeClient(secret_key=sk)
        try:
            a = c.request("GET", "/account")
            out.append((bool(a.get("charges_enabled")), f"Stripe account charges_enabled={a.get('charges_enabled')} payouts_enabled={a.get('payouts_enabled')} country={a.get('country')}"))
        except stripe_pay.StripeError as e:
            out.append((e.status == 403, f"account read: {e.status} {e.code} (a restricted key may not read /account - OK)"))
        try:
            eps = [w for w in c.request("GET", "/webhook_endpoints", {"limit": 50})["data"] if w.get("url") == WEBHOOK_URL]
            ok = any(w.get("status") == "enabled" and (EVENTS <= set(w.get("enabled_events", [])) or "*" in w.get("enabled_events", [])) for w in eps)
            out.append((ok, f"live webhook {WEBHOOK_URL}: {'enabled with all 7 events' if ok else 'MISSING / disabled / events missing'}"))
        except stripe_pay.StripeError as e:
            out.append((False, f"webhook endpoint list: {e.status} {e.code} (create it in the Dashboard, live mode)"))
    return all(o for o, _ in out), out


def upstash_export(e, count_only=False):
    url, tok = e["UPSTASH_REDIS_REST_URL"].rstrip("/"), e["UPSTASH_REDIS_REST_TOKEN"]

    def call(cmds):
        r = urllib.request.Request(url + "/pipeline", data=json.dumps(cmds).encode(), method="POST",
                                   headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        return [x.get("result") for x in json.loads(urllib.request.urlopen(r, timeout=120).read())]
    prefix = e.get("STORE_KEY_PREFIX", "")
    keys, cur = [], "0"
    while True:
        cur, batch = call([["SCAN", cur, "COUNT", "1000"]])[0]
        keys += [k for k in batch if (k.startswith(prefix) if prefix else not k.startswith("stg:"))]   # production data only
        if str(cur) == "0":
            break
    if count_only:
        return {"keys": len(keys)}
    out = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        types = call([["TYPE", k] for k in chunk])
        getters = {"string": "GET", "hash": "HGETALL", "list": "LRANGE", "set": "SMEMBERS", "zset": "ZRANGE"}
        cmds = []
        for k, t in zip(chunk, types):
            g = getters.get(t, "GET")
            cmds.append([g, k, "0", "-1"] if g in ("LRANGE",) else [g, k, "0", "-1", "WITHSCORES"] if g == "ZRANGE" else [g, k])
        vals, ttls = call(cmds), call([["PTTL", k] for k in chunk])
        for k, t, v, p in zip(chunk, types, vals, ttls):
            out[k] = {"type": t, "value": v, "pttl": p}
    return {"exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "keys": out}


def backup(label):
    e = env()
    folder = os.path.join(r"C:\EBD_SIGNING", time.strftime("prod-backup-%Y-%m-%d-%H%M-") + label)
    os.makedirs(folder)                                    # raises if it exists: a backup is never overwritten
    git("fetch", "-q", "origin")
    main = git("rev-parse", "origin/main")
    files = {"render-service.json": render("GET", f"/services/{SVC}"), "render-env-vars.json": e,
             "render-last-deploys.json": render("GET", f"/services/{SVC}/deploys?limit=5"), "upstash-production-export.json": upstash_export(e)}
    for name, data in files.items():
        with open(os.path.join(folder, name), "x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    open(os.path.join(folder, "GIT_MAIN_COMMIT.txt"), "x").write(main + "\n")
    ver = health().get("version", "unknown")
    git("archive", "--format=zip", "-o", os.path.join(folder, f"backend-main-{ver}.zip"), main)
    sums = [f"{hashlib.sha256(open(os.path.join(folder, n), 'rb').read()).hexdigest()}  {n}" for n in sorted(os.listdir(folder))]
    open(os.path.join(folder, "SHA256SUMS.txt"), "x").write("\n".join(sums) + "\n")
    print(f"backup: {folder} | {len(files['upstash-production-export.json']['keys'])} Upstash keys | {len(e)} env vars | main {main[:7]} | backend {ver}")
    return folder


def wait_deploy(commit=None, since=None):
    for _ in range(60):
        time.sleep(15)
        d = render("GET", f"/services/{SVC}/deploys?limit=1")[0]["deploy"]
        if (commit and not d["commit"]["id"].startswith(commit[:7])) or (since and d["id"] == since):
            continue
        if d["status"] in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            return d["status"]
    return "timeout"


def regression(folder, expect_mode):
    out = []
    h = health()
    out.append((h.get("version") == VERSION and h.get("license_server") is True, f"health {h.get('version')} license_server={h.get('license_server')}"))
    for prod in ("EBD-WIN", "EBD-AND"):
        s, _, b = http("GET", "/v1/app/update?product=" + prod)
        want = json.load(open(os.path.join(REPO, "updates", {"EBD-WIN": "windows.json", "EBD-AND": "android-web.json"}[prod]), encoding="utf-8"))
        got = json.loads(b) if s == 200 else {}
        out.append((s == 200 and got.get("payload") == want["payload"] and got.get("signature") == want["signature"],
                    f"signed update manifest {prod} unchanged (HTTP {s})"))
    # owner licences still refresh on their registered device (data kept)
    owners = json.load(open(r"C:\EBD_SIGNING\owner-licenses-server\owner-licenses.json"))
    export = json.load(open(os.path.join(folder, "upstash-production-export.json"), encoding="utf-8"))["keys"]
    for prod, o in owners.items():
        rec = json.loads((export.get("lic:r:" + o["lid"]) or {}).get("value") or "{}")
        dev = (rec.get("device") or {}).get("hash")
        if dev:
            s, _, _ = http("POST", "/v1/license/refresh", {"license_key": o["license_key"], "product": prod, "device_id": dev, "app_version": "deploy-check"})
            out.append((s == 200, f"owner licence {o['lid']} refresh -> {s}"))
    s, h2, _ = http("POST", "/v1/shop/stripe/checkout", {"product": "windows", "lang": "hu", "consent": True}, {"Origin": "https://ebikedoctorpro.com"})
    if expect_mode == "off":
        out.append((s == 503, f"Stripe checkout is OFF (503) -> {s}"))
    else:
        out.append((s == 200, f"Stripe LIVE checkout session created -> {s} (open session, no charge; it expires by itself)"))
    s, _, _ = http("POST", "/v1/shop/stripe/webhook", {"id": "evt_forged"}, {"Stripe-Signature": "t=1,v1=00"})
    out.append((s == 401, f"forged Stripe webhook rejected -> {s}"))
    s, _, _ = http("POST", "/v1/shop/stripe/confirm", {"session_id": "cs_bad../../x"})
    out.append((s == 422, f"thank-you confirm endpoint present (invalid id 422) -> {s}"))
    s, _, b = http("POST", "/v1/shop/stripe/confirm", {"session_id": "cs_live_" + "0" * 20})
    out.append((s == 200, f"thank-you confirm answers without Stripe key / unknown session -> {s} {b[:40]}"))
    s, _, _ = http("POST", "/v1/license/activate", {"license_key": "EBDW-AAAAA-BBBBB-CCCCC-DDDDD", "product": "EBD-WIN", "device_id": "a" * 64, "app_version": "x"})
    out.append((s == 404, f"licence server answers (bogus key 404) -> {s}"))
    for ok, msg in out:
        print(("PASS " if ok else "FAIL ") + msg)
    return all(ok for ok, _ in out)


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "plan"
    if cmd == "plan":
        e, h = env(), health()
        git("fetch", "-q", "origin")
        ff = subprocess.run([GIT, "-C", REPO, "merge-base", "--is-ancestor", "origin/main", f"origin/{BRANCH}"]).returncode == 0
        print(f"production backend {h.get('version')} | SHOP_MODE={e.get('SHOP_MODE')} | main {git('rev-parse', '--short', 'origin/main')} -> "
              f"{git('rev-parse', '--short', 'origin/' + BRANCH)} ({BRANCH}) | fast-forward: {ff}")
        print("STEP 1 (deploy) env changes: none (Stripe stays off: no key, SHOP_MODE=test)")
        print("STEP 2 (activate) env: ADD STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET, OWNER_NOTIFY_EMAIL"
              f" | SET SHOP_MODE test->live | present already: {[k for k in ('STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY', 'STRIPE_WEBHOOK_SECRET', 'OWNER_NOTIFY_EMAIL') if k in e]}")
        kv = live_keys()
        if kv is None:
            print(f"live key file: MISSING ({KEYFILE}) - needed only for STEP 2")
        else:
            ok, msgs = check_keys(kv)
            for o, m in msgs:
                print(("OK   " if o else "MISS ") + m)
        print("Upstash production keys:", upstash_export(e, count_only=True)["keys"])
        return
    if cmd == "backup":
        if "--count-only" in a:
            print("Upstash production keys:", upstash_export(env(), count_only=True)["keys"]); return
        backup("manual"); return
    if "--confirm" not in a:
        sys.exit("refused: add --confirm (only after the owner's approval)")
    if cmd == "deploy":
        folder = backup("pre-3.3")
        git("fetch", "-q", "origin")
        main_sha, target = git("rev-parse", "origin/main"), git("rev-parse", f"origin/{BRANCH}")
        if subprocess.run([GIT, "-C", REPO, "merge-base", "--is-ancestor", main_sha, target]).returncode != 0:
            sys.exit("refused: not a fast-forward")
        if subprocess.run([GIT, "-C", REPO, "rev-parse", "-q", "--verify", "refs/tags/" + TAG], capture_output=True).returncode != 0:
            git("-c", "user.name=Dukai Balint", "-c", "user.email=dukaibalint37@gmail.com", "tag", "-a", TAG, main_sha, "-m", "Production before 3.3 (rollback point)")
            git("push", "-q", "origin", TAG)
        git("push", "-q", "origin", f"{target}:refs/heads/main")                  # fast-forward only (no --force)
        print("deploy:", wait_deploy(commit=target))
        print("REGRESSION:", "OK" if regression(folder, "off") else f"FAILED -> rollback {folder} --confirm")
    elif cmd == "activate":
        kv = live_keys()
        if kv is None:
            sys.exit(f"refused: live key file missing ({KEYFILE})")
        ok, msgs = check_keys(kv)
        for o, m in msgs:
            print(("OK   " if o else "MISS ") + m)
        if not ok:
            sys.exit("refused: live keys / webhook not ready")
        if health().get("version") != VERSION:
            sys.exit("refused: run STEP 1 (deploy) first")
        e = env()
        folder = backup("pre-activate")
        new = {"STRIPE_SECRET_KEY": kv["STRIPE_SECRET_KEY"], "STRIPE_PUBLISHABLE_KEY": kv["STRIPE_PUBLISHABLE_KEY"],
               "STRIPE_WEBHOOK_SECRET": kv["STRIPE_WEBHOOK_SECRET"], "OWNER_NOTIFY_EMAIL": e.get("MAIL_REPLY_TO", ""), "SHOP_MODE": "live"}
        last = render("GET", f"/services/{SVC}/deploys?limit=1")[0]["deploy"]["id"]
        for k, v in new.items():
            render("PUT", f"/services/{SVC}/env-vars/{k}", {"value": v})
        render("POST", f"/services/{SVC}/deploys", {})
        print("redeploy:", wait_deploy(since=last))
        print("LIVE CHECK:", "OK" if regression(folder, "live") else f"FAILED -> deactivate --confirm (or rollback {folder} --confirm)")
    elif cmd == "deactivate":
        last = render("GET", f"/services/{SVC}/deploys?limit=1")[0]["deploy"]["id"]
        render("PUT", f"/services/{SVC}/env-vars/SHOP_MODE", {"value": "test"})
        render("POST", f"/services/{SVC}/deploys", {})
        print("redeploy:", wait_deploy(since=last), "| live sales OFF (SHOP_MODE=test)")
    elif cmd == "rollback":
        folder = a[1]
        backup_env = json.load(open(os.path.join(folder, "render-env-vars.json"), encoding="utf-8"))
        for k in set(env()) - set(backup_env):
            urllib.request.urlopen(urllib.request.Request(f"https://api.render.com/v1/services/{SVC}/env-vars/{k}", method="DELETE",
                                                          headers={"Authorization": "Bearer " + KEY}), timeout=60)
        for k, v in backup_env.items():
            render("PUT", f"/services/{SVC}/env-vars/{k}", {"value": v})
        tag = git("rev-parse", TAG + "^{commit}")
        git("push", "-q", "--force-with-lease", "origin", f"{tag}:refs/heads/main")
        print("rollback deploy:", wait_deploy(commit=tag))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
