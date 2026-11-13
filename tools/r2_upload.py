"""Uploads the installers to the PRIVATE Cloudflare R2 (or any S3-compatible) bucket and verifies them.

  python tools/r2_upload.py <credentials.txt> <local_file> <object_key>

credentials.txt (C:\\EBD_SIGNING\\download-bucket-write.txt, one value per line):
  endpoint (https://<accountid>.r2.cloudflarestorage.com) / bucket / access key id / secret access key  [/ region, default auto]
Needs an Object Read & Write token for that bucket. After the upload it checks size and SHA-256 by downloading the object
through a short pre-signed link (the same mechanism the shop uses). Prints no secret.
"""
import hashlib
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import s3presign  # noqa: E402

cred_file, local, key = sys.argv[1], sys.argv[2], sys.argv[3].lstrip("/")
lines = [l.strip() for l in open(cred_file, encoding="utf-8-sig") if l.strip()]
kv = dict(l.split("=", 1) for l in lines if "=" in l and l.split("=", 1)[0].strip().isupper())
kv = {k.strip(): v.strip() for k, v in kv.items()}
if kv.get("R2_ENDPOINT"):      # KEY=VALUE format (R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY[, R2_REGION])
    endpoint, bucket, akid, secret = kv["R2_ENDPOINT"], kv["R2_BUCKET"], kv["R2_ACCESS_KEY_ID"], kv["R2_SECRET_ACCESS_KEY"]
    region = kv.get("R2_REGION", "auto")
else:                          # legacy: one value per line
    endpoint, bucket, akid, secret = lines[:4]
    region = lines[4] if len(lines) > 4 else "auto"
host = urllib.parse.urlsplit(endpoint).netloc
path = f"/{bucket}/{key}"
# never overwrite an existing object (an installer that buyers already download): refuse if the key exists
EMPTY = hashlib.sha256(b"").hexdigest()
try:
    urllib.request.urlopen(urllib.request.Request(f"https://{host}{urllib.parse.quote(path)}", method="HEAD",
                                                  headers=s3presign.sign_headers("HEAD", host, path, akid, secret, region, EMPTY)), timeout=30)
    sys.exit(f"REFUSED: object {key} already exists in the bucket - use a new object key (e.g. with the version number).")
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise
data = open(local, "rb").read()
digest = hashlib.sha256(data).hexdigest()
ctype = "application/vnd.android.package-archive" if key.endswith(".apk") else "application/octet-stream"
headers = s3presign.sign_headers("PUT", host, path, akid, secret, region, digest, extra={"content-type": ctype})
req = urllib.request.Request(f"https://{host}{urllib.parse.quote(path)}", data=data, method="PUT", headers={**headers, "Content-Type": ctype})
with urllib.request.urlopen(req, timeout=600) as r:
    print("PUT", key, r.status, f"{len(data) / 1e6:.1f} MB")
url = s3presign.presign_get(host, path, akid, secret, region, expires=300)
h = hashlib.sha256()
n = 0
with urllib.request.urlopen(url, timeout=600) as r:
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            break
        h.update(chunk); n += len(chunk)
ok = h.hexdigest() == digest and n == len(data)
print("verify:", "OK" if ok else "MISMATCH", "sha256", digest, "size", n)
try:
    urllib.request.urlopen(f"https://{host}{urllib.parse.quote(path)}", timeout=30)
    print("WARNING: the object is readable WITHOUT a signature - make the bucket private!")
except urllib.error.HTTPError as e:
    print("unsigned access refused:", e.code, "(bucket is private - correct)")
sys.exit(0 if ok else 1)
