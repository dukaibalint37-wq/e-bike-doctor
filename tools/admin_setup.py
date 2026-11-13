"""Creates the owner admin 2FA credentials (run locally; nothing is printed except file paths).

  python tools/admin_setup.py <output_dir> [account_label]

Writes into <output_dir> (keep it in C:\\EBD_SIGNING, never commit):
  admin-password.txt       the login password (24 random characters)
  admin-totp-uri.txt       otpauth:// URI for the authenticator app (Google Authenticator, Microsoft Authenticator, Aegis ...)
  admin-totp-qr.png        the same as a QR code (only if the 'qrcode' package is installed)
  admin-env.json           ADMIN_PASSWORD_HASH, ADMIN_TOTP_SECRET, ADMIN_SESSION_SECRET for the server environment
"""
import base64
import json
import os
import secrets
import string
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import admin_auth  # noqa: E402

out = sys.argv[1]
label = sys.argv[2] if len(sys.argv) > 2 else "admin"
os.makedirs(out, exist_ok=True)
_existing = [n for n in ("admin-password.txt", "admin-totp-uri.txt", "admin-env.json", "admin-totp-qr.png") if os.path.exists(os.path.join(out, n))]
if _existing:   # never overwrite existing credentials (they may already be in use on a server)
    sys.exit(f"REFUSED: {out} already contains {_existing}. Use a NEW, empty folder; existing credentials are never overwritten.")
alphabet = string.ascii_letters + string.digits + "-_.!?"
password = "".join(secrets.choice(alphabet) for _ in range(24))
totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
uri = "otpauth://totp/" + urllib.parse.quote(f"E-Bike Doctor PRO:{label}") + "?" + urllib.parse.urlencode(
    {"secret": totp_secret, "issuer": "E-Bike Doctor PRO", "algorithm": "SHA1", "digits": 6, "period": 30})
env = {"ADMIN_PASSWORD_HASH": admin_auth.hash_password(password), "ADMIN_TOTP_SECRET": totp_secret, "ADMIN_SESSION_SECRET": secrets.token_urlsafe(48)}
for name, content in (("admin-password.txt", password + "\n"), ("admin-totp-uri.txt", uri + "\n"), ("admin-env.json", json.dumps(env, indent=1))):
    with open(os.path.join(out, name), "x", encoding="utf-8") as f:     # 'x' = fail if the file exists
        f.write(content)
try:
    import qrcode
    qrcode.make(uri).save(os.path.join(out, "admin-totp-qr.png"))
    qr = "admin-totp-qr.png"
except ImportError:
    qr = "(qrcode package missing - add the URI manually)"
print("written to", out, ":", "admin-password.txt, admin-totp-uri.txt, admin-env.json,", qr)
