"""Production upgrade 3.0.1 -> 3.2.0 (Render srv-dbfh3jss728c73ak0hc0). RUN ONLY AFTER THE OWNER'S EXPLICIT APPROVAL.

  python tools/deploy_production_32.py plan                 -> shows what would change (no writes)
  python tools/deploy_production_32.py deploy --confirm     -> backup check, env additions, main fast-forward, wait, regression
  python tools/deploy_production_32.py rollback --confirm   -> main back to the tag eles-3.0.1-3.2-elott + env restore from the backup

Secrets are read from C:\\EBD_SIGNING and never printed. Existing variables (Gemini, Upstash, QUOTA_SECRET, lease key, pepper,
cost caps, AI mode ...) are NOT changed; only new 3.2 variables are added. No data migration is needed (3.2 reads 3.0 records)."""
import json, os, secrets, subprocess, sys, time, urllib.error, urllib.request

SVC = "srv-dbfh3jss728c73ak0hc0"
PROD = "https://e-bike-doctor.onrender.com"
BACKUP = r"C:\EBD_SIGNING\prod-backup-2026-11-13"
GIT = r"C:\Program Files\Git\cmd\git.exe"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG, BRANCH = "eles-3.0.1-3.2-elott", "backend-3.2-staging"
KEY = os.environ.get("RENDER_API_KEY", "")


def render(method, path, body=None):
    r = urllib.request.Request("https://api.render.com/v1" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": "Bearer " + KEY, "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as x:
        t = x.read().decode()
        return json.loads(t) if t else None


def current_env():
    return {x["envVar"]["key"]: x["envVar"]["value"] for x in render("GET", f"/services/{SVC}/env-vars?limit=100")}


def new_vars():
    adm = json.load(open(r"C:\EBD_SIGNING\admin-2fa-production\admin-env.json"))
    p = r"C:\EBD_SIGNING\admin-2fa-production\interest-secret.txt"
    if not os.path.exists(p):
        open(p, "w").write(secrets.token_hex(32))
    return {**adm, "INTEREST_SECRET": open(p).read().strip(), "CORS_ORIGINS": "https://e-bike-doctor-pro-website.pages.dev",
            "SITE_URL": "https://e-bike-doctor-pro-website.pages.dev", "SITE_HOST": "e-bike-doctor-pro-website.pages.dev", "ADMIN_HEADER_TOKEN": "1"}


def git(*a):
    return subprocess.run([GIT, "-C", REPO, *a], capture_output=True, text=True, check=True).stdout.strip()


def wait_deploy(commit):
    for _ in range(60):
        time.sleep(15)
        d = render("GET", f"/services/{SVC}/deploys?limit=1")[0]["deploy"]
        if d["commit"]["id"].startswith(commit[:7]) and d["status"] in ("live", "build_failed", "update_failed", "canceled"):
            return d["status"]
    return "timeout"


def regression():
    owners = json.load(open(r"C:\EBD_SIGNING\owner-licenses-server\owner-licenses.json"))
    export = json.load(open(os.path.join(BACKUP, "upstash-production-export.json")))["keys"]
    out = []
    h = json.loads(urllib.request.urlopen(PROD + "/health", timeout=90).read())
    out.append((h["version"] == "3.2.0" and h["license_server"] and h["ai_public"] and h["store"].startswith("upstash"), f"health {h['version']} {h['store']}"))
    for prod, o in owners.items():
        rec = json.loads(export.get("lic:r:" + o["lid"], {}).get("value") or "{}")
        dev = (rec.get("device") or {}).get("hash")
        if dev:
            r = urllib.request.Request(PROD + "/v1/license/refresh", data=json.dumps({"license_key": o["license_key"], "product": prod, "device_id": dev,
                                                                                      "app_version": "deploy-check"}).encode(), headers={"Content-Type": "application/json"})
            out.append((urllib.request.urlopen(r, timeout=60).status == 200, f"owner licence {o['lid']} refresh"))
    for path, want in (("/v1/admin/stats", 404), ("/v1/quota", 200), ("/v1/app/update?product=EBD-WIN", 200), ("/admin", 200)):
        try:
            s = urllib.request.urlopen(PROD + path, timeout=60).status
        except urllib.error.HTTPError as e:
            s = e.code
        out.append((s == want, f"GET {path} -> {s}"))
    for ok, msg in out:
        print(("PASS " if ok else "FAIL ") + msg)
    return all(ok for ok, _ in out)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    env = current_env()
    add = new_vars()
    if cmd == "plan":
        print("existing variables kept unchanged:", len(env))
        print("variables to ADD/SET:", sorted(add), "| already present:", sorted(k for k in add if k in env))
        git("fetch", "-q", "origin")
        print("main now:", git("rev-parse", "--short", "origin/main"), "-> will become", git("rev-parse", "--short", f"origin/{BRANCH}"),
              "| fast-forward:", subprocess.run([GIT, "-C", REPO, "merge-base", "--is-ancestor", "origin/main", f"origin/{BRANCH}"]).returncode == 0)
        print("backup files:", sorted(os.listdir(BACKUP)))
        return
    if "--confirm" not in sys.argv:
        sys.exit("refused: add --confirm (only after the owner's approval)")
    if cmd == "deploy":
        assert os.path.exists(os.path.join(BACKUP, "upstash-production-export.json")) and os.path.exists(os.path.join(BACKUP, "render-env-vars.json")), "backup missing"
        for k, v in add.items():
            render("PUT", f"/services/{SVC}/env-vars/{k}", {"value": v})
        git("fetch", "-q", "origin")
        target = git("rev-parse", f"origin/{BRANCH}")
        git("push", "-q", "origin", f"{target}:refs/heads/main")          # fast-forward only (no --force)
        print("deploy:", wait_deploy(target))
        print("REGRESSION:", "OK" if regression() else "FAILED -> run: rollback --confirm")
    elif cmd == "rollback":
        backup_env = json.load(open(os.path.join(BACKUP, "render-env-vars.json")))
        for k in add:
            if k not in backup_env:
                urllib.request.urlopen(urllib.request.Request(f"https://api.render.com/v1/services/{SVC}/env-vars/{k}", method="DELETE",
                                                              headers={"Authorization": "Bearer " + KEY}), timeout=60)
        for k, v in backup_env.items():
            render("PUT", f"/services/{SVC}/env-vars/{k}", {"value": v})
        tag = git("rev-parse", TAG)
        git("push", "-q", "--force-with-lease", "origin", f"{tag}:refs/heads/main")
        print("rollback deploy:", wait_deploy(tag))


if __name__ == "__main__":
    main()
