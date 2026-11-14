"""Resend (free plan) as the ALTERNATIVE sender of E-Bike Doctor PRO - Gmail API stays primary. STAGING ONLY.

  python tools/resend_setup.py check                -> domain status in Resend (DNS records verified?)  [read-only]
  python tools/resend_setup.py testmail <to>        -> sends the purchase e-mail template (HU/DE/EN, dummy key) via Resend   [3 mails]
  python tools/resend_setup.py staging --confirm    -> verified backup of the staging env, then ADDS (never overwrites):
                                                       RESEND_API_KEY, RESEND_FROM, MAIL_REPLY_TO (Gmail stays primary)

Key file (created by the owner, never printed): C:\\EBD_SIGNING\\resend-api-key.txt  (one line: re_...)
Sending domain (recommended by Resend: a subdomain): mail.ebikedoctorpro.com  -> sender licenc@mail.ebikedoctorpro.com
Production is never touched by this tool."""
import json, os, sys, time, urllib.error, urllib.request

KEY_FILE = r"C:\EBD_SIGNING\resend-api-key.txt"
DOMAIN = "mail.ebikedoctorpro.com"
FROM = f"E-Bike Doctor PRO <licenc@{DOMAIN}>"
REPLY_TO = "dukaibalint37@gmail.com"
STG = "srv-dbrjf2oae00c73frigrg"


def key():
    k = open(KEY_FILE, encoding="utf-8-sig").read().strip()
    assert k.startswith("re_"), "unexpected key format (expected re_...)"
    return k


def resend(method, path, body=None):
    r = urllib.request.Request("https://api.resend.com" + path, method=method, data=None if body is None else json.dumps(body).encode(),
                               headers={"Authorization": "Bearer " + key(), "Content-Type": "application/json", "User-Agent": "ebd-setup"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")


def render(method, path, body=None):
    req = urllib.request.Request("https://api.render.com/v1" + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + os.environ["RENDER_API_KEY"], "Accept": "application/json", "Content-Type": "application/json"})
    t = urllib.request.urlopen(req, timeout=60).read()
    return json.loads(t) if t else None


cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
if cmd == "check":
    s, d = resend("GET", "/domains")
    if s != 200:
        sys.exit(f"Resend API {s}: {d}")
    for x in d.get("data", []):
        print("domain", x["name"], "status", x.get("status"), "region", x.get("region"))
        s2, det = resend("GET", f"/domains/{x['id']}")
        for rec in (det.get("records") or []) if isinstance(det, dict) else []:
            print("   ", rec.get("record"), rec.get("type"), rec.get("name"), "->", rec.get("status"))
    if not any(x["name"] == DOMAIN for x in d.get("data", [])):
        print(f"NOTE: {DOMAIN} is not added in Resend yet (Resend dashboard -> Domains -> Add domain)")
elif cmd == "testmail":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import mail_templates as MT
    to = sys.argv[2]
    for lang in ("hu", "de", "en"):
        subj, text, html = MT.purchase("EBD-WIN", lang, "Resend teszt", "EBDW-TEST0-RESND-DUMMY-00000", "https://ebikedoctorpro.com/", True)
        s, r = resend("POST", "/emails", {"from": FROM, "to": [to], "reply_to": REPLY_TO, "subject": "[Resend teszt] " + subj, "text": text, "html": html})
        print(lang, "->", s, (r.get("id") if isinstance(r, dict) else r))
        time.sleep(1)
elif cmd == "staging":
    if "--confirm" not in sys.argv:
        sys.exit("refused: add --confirm")
    before = {x["envVar"]["key"]: x["envVar"]["value"] for x in render("GET", f"/services/{STG}/env-vars?limit=100")}
    bp = rf"C:\EBD_SIGNING\staging-3.1\render-staging-env-backup-{time.strftime('%Y%m%d-%H%M%S')}-resend.json"
    with open(bp, "x", encoding="utf-8") as f:
        json.dump(before, f, indent=1, sort_keys=True)
    assert json.load(open(bp, encoding="utf-8")) == before
    for k, v in {"RESEND_API_KEY": key(), "RESEND_FROM": FROM, "MAIL_REPLY_TO": REPLY_TO}.items():
        if k in before:
            print("SKIP (exists, not overwritten):", k); continue
        render("PUT", f"/services/{STG}/env-vars/{k}", {"value": v}); print("added", k)
    after = {x["envVar"]["key"]: x["envVar"]["value"] for x in render("GET", f"/services/{STG}/env-vars?limit=100")}
    assert not [k for k in before if after.get(k) != before[k]], "a pre-existing variable changed"
    render("POST", f"/services/{STG}/deploys", {"clearCache": "do_not_clear"})
    print("backup:", bp, "| staging redeploy started; expected mail chain: gmail -> resend")
