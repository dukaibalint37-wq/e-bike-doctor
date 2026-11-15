"""Switch the backend's private-download configuration (Cloudflare R2) on Render - staging first, production only after approval.

  python tools/render_download_switch.py check                         -> tests the credential file against R2 (read OK, write DENIED,
                                                                          new installers downloadable via 5-minute links)   [no Render call]
  python tools/render_download_switch.py plan  staging|production      -> READ-ONLY: which DOWNLOAD_* variables exist on the service and
                                                                          which would change (names + "same/different" only)
  python tools/render_download_switch.py apply staging|production --confirm
        -> 1) check  2) full env backup to a NEW file in C:\\EBD_SIGNING (never overwritten)  3) PUT only the DOWNLOAD_* variables below
           4) redeploy  5) /health + version check.  Other variables are never touched.
  python tools/render_download_switch.py rollback staging|production <backup.json> --confirm
        -> restores ONLY the DOWNLOAD_* variables from that backup (deletes the ones the backup did not have), redeploy.

  --objects=keep  : only the credentials (endpoint/bucket/key pair/region); DOWNLOAD_OBJECT_* stay as they are.
  default         : also DOWNLOAD_OBJECT_EBD_WIN / DOWNLOAD_OBJECT_EBD_AND -> the new 2.0.0 / 3.0.0 objects.

Credential file (KEY=VALUE, created by the owner, read-only Object token for ebd-downloads only):
  C:\\EBD_SIGNING\\download-bucket-production.txt   R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY [, R2_REGION]
Render API key: environment variable RENDER_API_KEY.  NOTHING secret is printed or logged; values are compared by equality only.
PRODUCTION REQUIREMENT: the production code must be backend >= 3.2 (s3presign). 3.0.1 only knows DOWNLOAD_URL_* -> apply refuses."""
import hashlib, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import s3presign  # noqa: E402

CRED = os.environ.get("EBD_DOWNLOAD_CRED", r"C:\EBD_SIGNING\download-bucket-production.txt")
SERVICES = {"staging": ("srv-dbrjf2oae00c73frigrg", "https://e-bike-doctor-staging.onrender.com", r"C:\EBD_SIGNING\staging-3.1"),
            "production": ("srv-dbfh3jss728c73ak0hc0", "https://e-bike-doctor.onrender.com", r"C:\EBD_SIGNING\prod-backup-2026-11-13")}
OBJECTS = {"DOWNLOAD_OBJECT_EBD_WIN": ("windows/EBikeDoctorPRO-Setup-2.0.0.exe", 70524268, "5df4f7c308d1360704293084fdcc54ec382b6421144328af5e13f1f3cf403aa4"),
           "DOWNLOAD_OBJECT_EBD_AND": ("android/EBikeDoctorPRO-3.0.0-web.apk", 1804343, "707ccc940df8c231f6009fe74ef86487ffcd3af1865c06d4342365fbb12f9df5")}
MANAGED = ["DOWNLOAD_S3_ENDPOINT", "DOWNLOAD_S3_BUCKET", "DOWNLOAD_S3_ACCESS_KEY_ID", "DOWNLOAD_S3_SECRET_ACCESS_KEY", "DOWNLOAD_S3_REGION", *OBJECTS]


def cred():
    if not os.path.exists(CRED):
        sys.exit(f"credential file missing: {CRED}")
    lines = [l.strip() for l in open(CRED, encoding="utf-8-sig") if l.strip()]
    kv = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip() for l in lines if "=" in l}
    # Cloudflare dashboard labels ("Access Key ID=...", "Secret Access Key=...") are accepted as well
    for label, k in (("access key id", "R2_ACCESS_KEY_ID"), ("access key", "R2_ACCESS_KEY_ID"), ("secret access key", "R2_SECRET_ACCESS_KEY")):
        for kk in list(kv):
            if kk.lower() == label and not kv.get(k):
                kv[k] = kv[kk]
    # endpoint/bucket are account settings, not secrets: taken (read-only) from the existing upload file if missing
    if not kv.get("R2_ENDPOINT") or not kv.get("R2_BUCKET"):
        ref = r"C:\EBD_SIGNING\download-bucket.txt"
        if os.path.exists(ref):
            rl = [l.strip() for l in open(ref, encoding="utf-8-sig") if l.strip()]
            rkv = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip() for l in rl if "=" in l}
            kv.setdefault("R2_ENDPOINT", rkv.get("R2_ENDPOINT", "")); kv.setdefault("R2_BUCKET", rkv.get("R2_BUCKET", ""))
            if not kv["R2_ENDPOINT"]: kv["R2_ENDPOINT"] = rkv.get("R2_ENDPOINT", "")
            if not kv["R2_BUCKET"]: kv["R2_BUCKET"] = rkv.get("R2_BUCKET", "")
            print("endpoint + bucket taken from", ref)
            ref_pair = (rkv.get("R2_ACCESS_KEY_ID"), rkv.get("R2_SECRET_ACCESS_KEY"))
            print("same key pair as the upload file (download-bucket.txt):", ref_pair == (kv.get("R2_ACCESS_KEY_ID"), kv.get("R2_SECRET_ACCESS_KEY")))
    miss = [k for k in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not kv.get(k)]
    if miss:
        sys.exit(f"credential file lacks: {miss} (expected KEY=VALUE lines)")
    return kv


def target(kv, objects=True):
    t = {"DOWNLOAD_S3_ENDPOINT": kv["R2_ENDPOINT"], "DOWNLOAD_S3_BUCKET": kv["R2_BUCKET"], "DOWNLOAD_S3_ACCESS_KEY_ID": kv["R2_ACCESS_KEY_ID"],
         "DOWNLOAD_S3_SECRET_ACCESS_KEY": kv["R2_SECRET_ACCESS_KEY"], "DOWNLOAD_S3_REGION": kv.get("R2_REGION", "auto") or "auto"}
    if objects:
        t.update({k: v[0] for k, v in OBJECTS.items()})
    return t


def s3(kv, method, path, query=""):
    host = urllib.parse.urlsplit(kv["R2_ENDPOINT"]).netloc
    h = s3presign.sign_headers(method, host, path, kv["R2_ACCESS_KEY_ID"], kv["R2_SECRET_ACCESS_KEY"], kv.get("R2_REGION", "auto") or "auto",
                               hashlib.sha256(b"").hexdigest(), query=query)
    url = f"https://{host}{urllib.parse.quote(path)}" + ("?" + query if query else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=h, data=b"" if method == "POST" else None), timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        t = e.read().decode(errors="replace")
        return e.code, (re.search(r"<Code>([^<]+)", t) or [None, t[:60]])[1]


def check():
    kv = cred(); b = kv["R2_BUCKET"]; host = urllib.parse.urlsplit(kv["R2_ENDPOINT"]).netloc; ok = True
    print("format: Access Key ID 32 hex:", bool(re.fullmatch(r"[0-9a-f]{32}", kv["R2_ACCESS_KEY_ID"])),
          "| Secret 64 hex:", bool(re.fullmatch(r"[0-9a-f]{64}", kv["R2_SECRET_ACCESS_KEY"])), "| bucket:", b)
    st, body = s3(kv, "GET", f"/{b}", "list-type=2&max-keys=100")
    print("read - list:", st, "| objects:", len(re.findall(rb"<Contents>", body)) if st == 200 else body); ok &= st == 200
    for var, (obj, size, sha) in OBJECTS.items():
        url = s3presign.presign_get(host, f"/{b}/{obj}", kv["R2_ACCESS_KEY_ID"], kv["R2_SECRET_ACCESS_KEY"], kv.get("R2_REGION", "auto") or "auto", 300,
                                    filename=obj.rsplit("/", 1)[1])
        try:
            data = urllib.request.urlopen(url, timeout=600).read(); good = len(data) == size and hashlib.sha256(data).hexdigest() == sha; code = 200
        except urllib.error.HTTPError as e:
            good, code = False, e.code
        print(f"read - 5-minute link {obj}: HTTP {code} | size+SHA-256 match: {good}"); ok &= good
    probe = f"/{b}/_permcheck/{uuid.uuid4().hex}.bin"
    st, body = s3(kv, "POST", probe, "uploads=")
    if st == 200:   # must not happen for a read-only token; abort immediately (same key!) so nothing is left behind
        uid = re.search(rb"<UploadId>([^<]+)", body).group(1).decode()
        ab, _ = s3(kv, "DELETE", probe, "uploadId=" + urllib.parse.quote(uid, safe=""))
        print("  (probe upload aborted:", ab, ")")
        print("write - multipart create: 200 -> TOKEN CAN WRITE (not read-only!)"); ok = False
    else:
        print("write - multipart create:", st, body, "-> denied (read-only, correct)" if st == 403 else ""); ok &= st == 403
    print("CHECK:", "OK" if ok else "FAILED")
    return ok


def render(method, path, body=None):
    key = os.environ.get("RENDER_API_KEY", "")
    if not key:
        sys.exit("RENDER_API_KEY is not set")
    r = urllib.request.Request("https://api.render.com/v1" + path, method=method, data=None if body is None else json.dumps(body).encode(),
                               headers={"Authorization": "Bearer " + key, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as x:
        t = x.read().decode()
        return json.loads(t) if t else None


def env_of(svc):
    return {x["envVar"]["key"]: x["envVar"]["value"] for x in render("GET", f"/services/{svc}/env-vars?limit=100")}


def health(url):
    try:
        return json.load(urllib.request.urlopen(url + "/health", timeout=60))
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


def plan(name, objects):
    svc, url, _ = SERVICES[name]
    env, t = env_of(svc), target(cred(), objects)
    h = health(url)
    print(f"{name}: {svc} | backend {h.get('version')} | variables on the service: {len(env)}")
    for k in MANAGED:
        if k not in t:
            print(f"  {k:32} {'present' if k in env else 'absent'} -> kept"); continue
        state = "absent -> ADD" if k not in env else ("same -> unchanged" if env[k] == t[k] else "different -> SET")
        print(f"  {k:32} {state}")
    legacy = [k for k in env if k.startswith("DOWNLOAD_URL_")]
    print("  legacy DOWNLOAD_URL_* present (fallback, kept):", legacy or "none")
    v = str(h.get("version", "0"))
    supports = tuple(int(x) for x in re.findall(r"\d+", v)[:2]) >= (3, 2)
    print("  backend supports private R2 links (>= 3.2):", supports)
    return env, t, supports


def apply(name, objects):
    if not check():
        sys.exit("refused: credential check failed")
    svc, url, bdir = SERVICES[name]
    env, t, supports = plan(name, objects)
    if not supports:
        sys.exit(f"refused: {name} runs a backend without private-R2 support - deploy 3.2 first (tools/deploy_production_32.py)")
    bp = os.path.join(bdir, f"render-{name}-env-backup-{time.strftime('%Y%m%d-%H%M%S')}-downloads.json")
    assert not os.path.exists(bp)
    json.dump(env, open(bp, "x", encoding="utf-8"), indent=1)
    assert json.load(open(bp, encoding="utf-8")) == env, "backup verification failed"
    print("backup:", bp)
    for k, v in t.items():
        if env.get(k) != v:
            render("PUT", f"/services/{svc}/env-vars/{k}", {"value": v}); print("set", k)
    after = env_of(svc)
    assert all(after.get(k) == v for k, v in t.items()), "verification after PUT failed"
    assert all(after.get(k) == env[k] for k in env if k not in t), "an unmanaged variable changed!"
    render("POST", f"/services/{svc}/deploys", {"clearCache": "do_not_clear"})
    print("redeploy started; waiting for /health ...")
    for _ in range(40):
        time.sleep(15)
        h = health(url)
        if h.get("status") == "ok":
            print("health:", h.get("status"), "| version:", h.get("version")); break
    print("next: download test with a TEST licence (staging) / owner licence (production); rollback:",
          f"python tools/render_download_switch.py rollback {name} \"{bp}\" --confirm")


def rollback(name, path):
    svc, url, _ = SERVICES[name]
    old = json.load(open(path, encoding="utf-8")); env = env_of(svc)
    for k in MANAGED:
        if k in old and env.get(k) != old[k]:
            render("PUT", f"/services/{svc}/env-vars/{k}", {"value": old[k]}); print("restored", k)
        elif k not in old and k in env:
            render("DELETE", f"/services/{svc}/env-vars/{k}"); print("removed", k)
    render("POST", f"/services/{svc}/deploys", {"clearCache": "do_not_clear"}); print("redeploy started")


def main():
    a = sys.argv[1:]
    objects = "--objects=keep" not in a
    if not a or a[0] == "check":
        sys.exit(0 if check() else 1)
    if a[0] == "plan" and len(a) > 1 and a[1] in SERVICES:
        plan(a[1], objects); return
    if a[0] in ("apply", "rollback") and len(a) > 1 and a[1] in SERVICES:
        if "--confirm" not in a:
            sys.exit("refused: add --confirm (only after the owner's approval)")
        apply(a[1], objects) if a[0] == "apply" else rollback(a[1], a[2])
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
