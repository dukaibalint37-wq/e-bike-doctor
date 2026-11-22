"""Creates the password of the statistics page /stats (password only, no 2FA; run locally, nothing secret is printed).

  python tools/stats_setup.py <output_dir>

Writes into <output_dir> (keep it in C:\\EBD_SIGNING, never commit; existing files are never overwritten):
  stats-password.txt   the login password (20 random characters, easy to type: no look-alike characters)
  stats-env.json       STATS_PASSWORD_HASH and STATS_SESSION_SECRET for the server environment
"""
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import admin_auth  # noqa: E402

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
existing = [n for n in ("stats-password.txt", "stats-env.json") if os.path.exists(os.path.join(out, n))]
if existing:
    sys.exit(f"REFUSED: {out} already contains {existing}. Existing credentials are never overwritten.")
alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
password = "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))
env = {"STATS_PASSWORD_HASH": admin_auth.hash_password(password), "STATS_SESSION_SECRET": secrets.token_urlsafe(48)}
for name, content in (("stats-password.txt", password + "\n"), ("stats-env.json", json.dumps(env, indent=1))):
    with open(os.path.join(out, name), "x", encoding="utf-8") as f:
        f.write(content)
print("written:", os.path.join(out, "stats-password.txt"), "|", os.path.join(out, "stats-env.json"))
