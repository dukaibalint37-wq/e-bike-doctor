"""Short-lived pre-signed GET URLs for an S3-compatible private bucket (Cloudflare R2, Backblaze B2, AWS S3).

AWS Signature Version 4, query-string authentication, implemented with the standard library only.
The bucket stays PRIVATE: a link works for DOWNLOAD_LINK_SECONDS (default 300 s) and only after the backend has checked
that the licence is active. Configuration (all server-side env vars, never in the website or the apps):
  DOWNLOAD_S3_ENDPOINT          e.g. https://<accountid>.r2.cloudflarestorage.com  or  https://s3.eu-central-003.backblazeb2.com
  DOWNLOAD_S3_REGION            R2: auto · B2: eu-central-003 · AWS: eu-central-1
  DOWNLOAD_S3_BUCKET            bucket name
  DOWNLOAD_S3_ACCESS_KEY_ID / DOWNLOAD_S3_SECRET_ACCESS_KEY   read-only key for that bucket
  DOWNLOAD_OBJECT_EBD_WIN / DOWNLOAD_OBJECT_EBD_AND           object keys (e.g. windows/E-Bike-Doctor-PRO-Setup-1.1.0.exe)
"""
import datetime
import hashlib
import hmac
import os
import urllib.parse


def _q(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign_get(host, path, access_key, secret_key, region, expires=300, now=None, filename=None, scheme="https"):
    """Returns a pre-signed GET URL for https://{host}{path}. `path` is the already-split object path ('/bucket/key')."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/s3/aws4_request"
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}", "X-Amz-Date": amz_date,
              "X-Amz-Expires": str(int(expires)), "X-Amz-SignedHeaders": "host"}
    if filename:
        params["response-content-disposition"] = f'attachment; filename="{filename}"'
    canonical_query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(params.items()))
    canonical_path = _q(path, safe="/-_.~")
    canonical_request = "\n".join(["GET", canonical_path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{canonical_path}?{canonical_query}&X-Amz-Signature={signature}"


def sign_headers(method, host, path, access_key, secret_key, region, payload_hash, now=None, extra=None, query=""):
    """Header-based SigV4 (used by tools/r2_upload.py for PUT/HEAD). Returns the headers to send."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    h = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date, **{k.lower(): v for k, v in (extra or {}).items()}}
    names = sorted(h)
    canonical_headers = "".join(f"{k}:{str(h[k]).strip()}\n" for k in names)
    signed = ";".join(names)
    canonical_request = "\n".join([method, _q(path, safe="/-_.~"), query, canonical_headers, signed, payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in h.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


def configured():
    return all(os.getenv(k, "").strip() for k in ("DOWNLOAD_S3_ENDPOINT", "DOWNLOAD_S3_BUCKET", "DOWNLOAD_S3_ACCESS_KEY_ID",
                                                  "DOWNLOAD_S3_SECRET_ACCESS_KEY"))


def object_url(product, expires=None):
    """Pre-signed URL for the product's object in the private bucket, or None when the bucket/object is not configured."""
    obj = os.getenv("DOWNLOAD_OBJECT_" + product.replace("-", "_"), "").strip().lstrip("/")
    if not configured() or not obj:
        return None
    endpoint = urllib.parse.urlsplit(os.getenv("DOWNLOAD_S3_ENDPOINT").strip())
    bucket = os.getenv("DOWNLOAD_S3_BUCKET").strip()
    expires = expires or int(os.getenv("DOWNLOAD_LINK_SECONDS", "300"))
    return presign_get(endpoint.netloc, f"/{bucket}/{obj}", os.getenv("DOWNLOAD_S3_ACCESS_KEY_ID").strip(),
                       os.getenv("DOWNLOAD_S3_SECRET_ACCESS_KEY").strip(), os.getenv("DOWNLOAD_S3_REGION", "auto").strip() or "auto",
                       expires=expires, filename=obj.rsplit("/", 1)[-1], scheme=endpoint.scheme or "https")
