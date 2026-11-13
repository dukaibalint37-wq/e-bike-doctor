"""One-time Gmail API authorisation for the licence e-mails WITHOUT an own domain (sender = your own Gmail address).

  python tools/gmail_oauth_setup.py <oauth_client.json> <output_dir>

<oauth_client.json>: Google Cloud Console → APIs & Services → Credentials → Create OAuth client ID → "Desktop app" → Download JSON.
The script opens the Google consent page in your browser (scope: gmail.send only - it can SEND mail, it cannot read your mailbox),
receives the answer on http://127.0.0.1 and writes <output_dir>/gmail-oauth.json (client id/secret + refresh token).
Nothing secret is printed. Then: GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN + MAIL_FROM=<your gmail> on the server.
Important: set the OAuth consent screen's publishing status to "In production" (no Google review is needed for your own account),
otherwise Google expires the refresh token after 7 days.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

client = json.load(open(sys.argv[1], encoding="utf-8"))
client = client.get("installed") or client.get("web") or client
out_dir = sys.argv[2]
verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
state = secrets.token_urlsafe(16)
result = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        if q.get("state") == state and "code" in q:
            result["code"] = q["code"]
            msg = "OK - you can close this tab. / Kész, bezárhatod ezt a lapot."
        else:
            msg = "Authorisation failed. / Sikertelen engedélyezés."
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
        self.wfile.write(msg.encode())

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
redirect = f"http://127.0.0.1:{srv.server_port}"
auth = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "access_type": "offline", "prompt": "consent",
    "scope": "https://www.googleapis.com/auth/gmail.send", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
print("Opening the Google consent page in your browser ...")
webbrowser.open(auth)
while "code" not in result:
    srv.handle_request()
body = urllib.parse.urlencode({"code": result["code"], "client_id": client["client_id"], "client_secret": client["client_secret"],
                               "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).encode()
tok = json.loads(urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=30).read())
if "refresh_token" not in tok:
    sys.exit("No refresh token returned - remove the app's access at myaccount.google.com/permissions and run again.")
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "gmail-oauth.json"), "w", encoding="utf-8") as f:
    json.dump({"GMAIL_CLIENT_ID": client["client_id"], "GMAIL_CLIENT_SECRET": client["client_secret"], "GMAIL_REFRESH_TOKEN": tok["refresh_token"]}, f)
print("written:", os.path.join(out_dir, "gmail-oauth.json"), "(scope: gmail.send)")
