"""E-Bike Doctor PRO – shared server-side licensing (Android web edition + Windows edition).

Design (see LICENSZERVER_HU.md):
- One purchase = one licence = one customer. Products are separate: EBD-WIN (Windows), EBD-AND (Android, own-website APK).
- Unique, random licence keys (95 bits + check character). Only the SHA-256 hash of a key is stored, never the key itself.
- Exactly ONE active device per licence. A second device is refused (409 device_limit) unless the user explicitly transfers
  the licence (device change / reinstall on new hardware); transfers are rate-limited (MAX_TRANSFERS_30D).
- Devices are identified by a salted hash computed on the device (no raw hardware IDs reach the server).
- The server returns a digitally signed, short-lived OFFLINE LEASE (ECDSA P-256, LEASE_DAYS, default 14). The apps verify it
  with the embedded PUBLIC key, so PRO features keep working during temporary internet outages. The private key exists only
  in the server environment (LEASE_SIGNING_KEY_PEM) – never in an app.
- Refund / chargeback → licence revoked: refresh fails, the licence-bound AI token is rejected immediately, the offline lease
  expires by itself (at most LEASE_DAYS).
- There is no master key: every licence is an individual record; owner licences are ordinary records with owner=True.
- AI: the licence-bound AI token's quota subject is the LICENCE (not the install) → reinstalling does not reset the daily 50.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32 (no I, L, O, U)
PRODUCTS = {
    "EBD-WIN": {"prefix": "EBDW", "name": "E-Bike Doctor PRO – Windows"},
    "EBD-AND": {"prefix": "EBDA", "name": "E-Bike Doctor PRO – Android (web)"},
}
TTL = 20 * 365 * 86_400
DAY = 86_400


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class LicenseError(Exception):
    def __init__(self, code, status, message):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


class StrictStore:
    """Licence/shop data must NEVER silently fall back to the in-memory store (FailSafeStore does that for quota/cache):
    a memory write would be lost on restart, and a memory 'lic:seq' restarting at 1 could overwrite a real licence once
    the remote store is back. On any storage error the request fails with 503 (clients/webhooks retry)."""

    def __init__(self, store):
        self.s = getattr(store, "primary", store)
        self.kind = getattr(self.s, "kind", "?")

    def _c(self, name, *a):
        try:
            return getattr(self.s, name)(*a)
        except Exception:
            raise LicenseError("store_unavailable", 503, "Licence storage temporarily unavailable - please retry") from None

    def get(self, key):
        return self._c("get", key)

    def set(self, key, value, ttl):
        return self._c("set", key, value, ttl)

    def incr(self, key, ttl, amount=1):
        return self._c("incr", key, ttl, amount)


# ----------------------------------------------------------------------------------------------------------- keys
def _check_char(body):
    return ALPHABET[sum((i + 1) * ALPHABET.index(c) for i, c in enumerate(body)) % 32]


def generate_key(product):
    body = "".join(secrets.choice(ALPHABET) for _ in range(19))
    body += _check_char(body)
    p = PRODUCTS[product]["prefix"]
    return p + "-" + "-".join(body[i:i + 5] for i in range(0, 20, 5))


def normalize_key(key):
    k = re.sub(r"[\s\-_]", "", (key or "").upper()).replace("O", "0").replace("I", "1").replace("L", "1")
    return k


def parse_key(key):
    """Returns (product, normalized) or raises LicenseError(invalid_key)."""
    k = normalize_key(key)
    for prod, info in PRODUCTS.items():
        pre = info["prefix"]
        if k.startswith(pre) and len(k) == len(pre) + 20:
            body = k[len(pre):]
            if all(c in ALPHABET for c in body) and _check_char(body[:19]) == body[19]:
                return prod, pre + body
    raise LicenseError("invalid_key", 404, "Invalid licence key")


def key_hash(normalized):
    pepper = os.getenv("LICENSE_KEY_PEPPER", "")
    return hashlib.sha256((pepper + "|" + normalized).encode()).hexdigest()


def _key_cipher():
    """AES-256-GCM for the re-send copy of a licence key (3.1). The key is derived from a server secret, so a leaked database
    alone reveals no licence key. Without any secret no copy is stored (re-send then explains how to get a new key)."""
    secret = os.getenv("LICENSE_KEY_ENC_SECRET", "").strip() or os.getenv("LICENSE_KEY_PEPPER", "").strip()
    if not secret:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(hashlib.sha256(("ebd-key-enc|" + secret).encode()).digest())


def encrypt_key(key, lid):
    c = _key_cipher()
    if not c:
        return ""
    nonce = secrets.token_bytes(12)
    return _b64u(nonce + c.encrypt(nonce, key.encode(), lid.encode()))


def decrypt_key(blob, lid):
    c = _key_cipher()
    if not c or not blob:
        return None
    try:
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        return c.decrypt(raw[:12], raw[12:], lid.encode()).decode()
    except Exception:
        return None


def email_hash(email):
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()


def mask_email(email):
    e = (email or "").strip()
    if "@" not in e:
        return ""
    user, dom = e.split("@", 1)
    return (user[:2] + "***@" + dom) if user else "***@" + dom


def valid_device_hash(d):
    return isinstance(d, str) and re.fullmatch(r"[a-f0-9]{64}", d or "") is not None


# ----------------------------------------------------------------------------------------------------------- lease signing
class LeaseSigner:
    """ECDSA P-256 / SHA-256, IEEE P1363 signature – identical format to the Windows/Android verifiers."""

    def __init__(self, pem=None):
        from cryptography.hazmat.primitives import serialization
        pem = pem or os.getenv("LEASE_SIGNING_KEY_PEM", "").replace("\\n", "\n").strip()
        self._key = None
        if pem:
            try:   # a malformed env value must not take the whole API (AI for existing apps) down
                self._key = serialization.load_pem_private_key(pem.encode(), password=None)
            except (ValueError, TypeError):
                import logging
                logging.getLogger("ebike-doctor.licensing").error("LEASE_SIGNING_KEY_PEM is not a valid PEM private key - licence server disabled")
        self.available = self._key is not None

    def public_spki_b64(self):
        from cryptography.hazmat.primitives import serialization
        return base64.b64encode(self._key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def sign(self, payload: dict) -> str:
        if not self._key:
            raise LicenseError("not_configured", 503, "Licence signing is not configured on the server")
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        der = self._key.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return "EBDL1-" + _b64u(data) + "." + _b64u(sig)


def _b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def verify_lease(token, public_spki_b64):
    """Reference verifier (tests). Returns payload dict or None."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.exceptions import InvalidSignature
    if not token or not token.startswith("EBDL1-") or "." not in token:
        return None
    body, sig = token[6:].split(".", 1)
    pad = lambda s: s + "=" * (-len(s) % 4)
    try:
        data = base64.urlsafe_b64decode(pad(body)); raw = base64.urlsafe_b64decode(pad(sig))
        pub = serialization.load_der_public_key(base64.b64decode(public_spki_b64))
        pub.verify(utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), data, ec.ECDSA(hashes.SHA256()))
        return json.loads(data)
    except (InvalidSignature, ValueError):
        return None


# ----------------------------------------------------------------------------------------------------------- service
class LicenseService:
    def __init__(self, store, quota_signer, lease_signer=None, clock=time.time):
        self.store, self.quota_signer, self.clock = store, quota_signer, clock
        self.lease = lease_signer if lease_signer is not None else LeaseSigner()

    # ---- storage helpers
    def _rec_key(self, lid):
        return "lic:r:" + lid

    def _get(self, lid):
        r = self.store.get(self._rec_key(lid))
        return r if isinstance(r, dict) else None

    def _put(self, rec):
        self.store.set(self._rec_key(rec["lid"]), rec, TTL)

    def _log(self, rec, event, **kw):
        rec.setdefault("history", []).append({"t": int(self.clock()), "e": event, **kw})
        rec["history"] = rec["history"][-50:]

    # ---- creation (webhook / admin)
    def create(self, product, email="", name="", owner=False, test=False, source="admin", order_id=""):
        if product not in PRODUCTS:
            raise LicenseError("bad_product", 422, "Unknown product")
        if order_id:
            existing = self.store.get("lic:order:" + str(order_id) + ":" + product)
            if existing:
                rec = self._get(existing)
                if rec:
                    return None, rec           # idempotent: webhook retry must not create a second licence
        seq = self.store.incr("lic:seq", TTL)
        lid = f"L{seq:06d}"
        key = generate_key(product)
        prod, norm = parse_key(key)
        rec = {"lid": lid, "product": product, "status": "active", "owner": bool(owner), "test": bool(test),
               "email_hash": email_hash(email) if email else "", "email_masked": mask_email(email), "name": (name or "")[:120],
               "source": source, "order_id": str(order_id or ""), "created": int(self.clock()), "key_hint": norm[-4:],
               "key_hash": key_hash(norm), "key_enc": encrypt_key(key, lid), "lang": "", "device": None, "transfers": [],
               "activations": 0}
        self._log(rec, "created", source=source)
        self.store.set("lic:k:" + rec["key_hash"], lid, TTL)
        if email:
            lst = self.store.get("lic:email:" + rec["email_hash"]) or []
            self.store.set("lic:email:" + rec["email_hash"], (lst if isinstance(lst, list) else []) + [lid], TTL)
        if order_id:
            self.store.set("lic:order:" + str(order_id) + ":" + product, lid, TTL)
        self._put(rec)
        return key, rec

    def find_by_key(self, key):
        prod, norm = parse_key(key)
        lid = self.store.get("lic:k:" + key_hash(norm))
        rec = self._get(lid) if lid else None
        if not rec:
            raise LicenseError("invalid_key", 404, "Invalid licence key")
        return rec

    # ---- client operations
    def _lease_for(self, rec, device_hash):
        now = int(self.clock())
        payload = {"v": 1, "lid": rec["lid"], "prod": rec["product"], "dev": device_hash, "iat": now,
                   "exp": now + _int("LEASE_DAYS", 14) * DAY, "ed": "OWNER" if rec.get("owner") else "PRO",
                   "owner": bool(rec.get("owner")), "name": rec.get("name", "")[:60]}
        return {"lease": self.lease.sign(payload), "lease_expires": payload["exp"],
                "ai_token": self.quota_signer.issue(f"lic-{rec['lid']}-{device_hash[:8]}"),
                "license": {"id": rec["lid"], "product": rec["product"], "edition": payload["ed"], "name": rec.get("name", ""),
                            "transfers_left": self._transfers_left(rec)}}

    def _transfers_left(self, rec):
        recent = [t for t in rec.get("transfers", []) if self.clock() - t < 30 * DAY]
        return max(0, _int("MAX_TRANSFERS_30D", 2) - len(recent))

    def activate(self, key, product, device_hash, device_name="", app_version="", transfer=False):
        if not valid_device_hash(device_hash):
            raise LicenseError("bad_device", 422, "Invalid device identifier")
        rec = self.find_by_key(key)
        if rec["product"] != product:
            raise LicenseError("wrong_product", 409, "This licence key belongs to another product")
        if rec["status"] != "active":
            raise LicenseError("revoked", 403, "This licence has been revoked")
        dev = rec.get("device")
        now = int(self.clock())
        if dev and dev.get("hash") != device_hash:
            if not transfer:
                raise LicenseError("device_limit", 409, "This licence is already active on another device")
            if self._transfers_left(rec) <= 0:
                raise LicenseError("transfer_limit", 429, "Too many device changes in the last 30 days")
            rec["transfers"] = [t for t in rec.get("transfers", []) if now - t < 30 * DAY] + [now]
            self._log(rec, "transferred", from_dev=dev.get("hash", "")[:8], to_dev=device_hash[:8])
            dev = None
        if not dev:
            rec["device"] = {"hash": device_hash, "name": (device_name or "")[:80], "since": now, "last_seen": now, "app": (app_version or "")[:20]}
            rec["activations"] = int(rec.get("activations", 0)) + 1
            self._log(rec, "activated", dev=device_hash[:8])
        else:
            rec["device"].update({"last_seen": now, "app": (app_version or "")[:20] or dev.get("app", "")})
        self._put(rec)
        return self._lease_for(rec, device_hash)

    def refresh(self, key, product, device_hash, app_version=""):
        """Periodic check: same as activate on the bound device, but never takes over a licence."""
        return self.activate(key, product, device_hash, app_version=app_version, transfer=False)

    def deactivate(self, key, device_hash):
        rec = self.find_by_key(key)
        dev = rec.get("device")
        if not dev or dev.get("hash") != device_hash:
            raise LicenseError("not_active_here", 409, "This licence is not active on this device")
        rec["device"] = None
        self._log(rec, "deactivated", dev=device_hash[:8])
        self._put(rec)
        return {"ok": True}

    # ---- AI access hook (QuotaManager.subject_resolver)
    def resolve_ai_subject(self, iid):
        """'lic-L000123-abcdef12' → 'lic-L000123' if the licence is active on that device, otherwise None (deny)."""
        m = re.fullmatch(r"lic-(L\d{6})-([a-f0-9]{8})", iid or "")
        if not m:
            return None
        rec = self._get(m.group(1))
        if not rec or rec.get("status") != "active" or not rec.get("device") or not rec["device"].get("hash", "").startswith(m.group(2)):
            return None
        return "lic-" + m.group(1)

    # ---- admin
    def revoke(self, lid, reason="admin"):
        rec = self._require(lid)
        rec["status"] = "revoked"; rec["revoked_at"] = int(self.clock()); rec["revoke_reason"] = reason[:120]
        self._log(rec, "revoked", reason=reason[:60]); self._put(rec)
        return rec

    def reinstate(self, lid):
        rec = self._require(lid)
        rec["status"] = "active"; self._log(rec, "reinstated"); self._put(rec)
        return rec

    def reset_device(self, lid):
        rec = self._require(lid)
        rec["device"] = None; rec["transfers"] = []; self._log(rec, "device_reset"); self._put(rec)
        return rec

    def reissue_key(self, lid):
        """Lost/leaked key: a new key replaces the old one (old key stops working immediately)."""
        rec = self._require(lid)
        self.store.set("lic:k:" + rec["key_hash"], "", 60)
        key = generate_key(rec["product"])
        _, norm = parse_key(key)
        rec["key_hash"], rec["key_hint"], rec["key_enc"] = key_hash(norm), norm[-4:], encrypt_key(key, lid)
        self.store.set("lic:k:" + rec["key_hash"], lid, TTL)
        self._log(rec, "key_reissued"); self._put(rec)
        return key, rec

    def _require(self, lid):
        rec = self._get(lid) if re.fullmatch(r"L\d{6}", lid or "") else None
        if not rec:
            raise LicenseError("not_found", 404, "Licence not found")
        return rec

    def get(self, lid):
        return self._require(lid)

    def recover_key(self, rec):
        """Plain key for the re-send mail (only to the purchase e-mail address); None for licences without a stored copy."""
        key = decrypt_key(rec.get("key_enc", ""), rec["lid"])
        try:
            return key if key and key_hash(parse_key(key)[1]) == rec.get("key_hash") else None
        except LicenseError:
            return None

    def by_email(self, email):
        ids = self.store.get("lic:email:" + email_hash(email)) or []
        return [r for r in (self._get(i) for i in (ids if isinstance(ids, list) else [])) if r]

    def all(self, limit=2000):
        n = int(self.store.get("lic:seq") or 0)
        out = []
        for i in range(n, max(0, n - limit), -1):
            r = self._get(f"L{i:06d}")
            if r:
                out.append(r)
        return out

    def search(self, q="", limit=200):
        q = (q or "").strip()
        recs = self.all()
        if not q:
            return recs[:limit]
        if "@" in q:
            ids = set(self.store.get("lic:email:" + email_hash(q)) or [])
            return [r for r in recs if r["lid"] in ids][:limit]
        try:
            return [self.find_by_key(q)]
        except LicenseError:
            pass
        ql = q.lower()
        return [r for r in recs if ql in r["lid"].lower() or ql in r.get("name", "").lower() or ql in r.get("order_id", "").lower()][:limit]

    def stats(self):
        recs = self.all()
        out = {p: {"total": 0, "active": 0, "revoked": 0, "activated_devices": 0, "owner": 0, "test": 0} for p in PRODUCTS}
        for r in recs:
            s = out[r["product"]]
            s["total"] += 1
            s["revoked" if r["status"] != "active" else "active"] += 1
            s["activated_devices"] += 1 if r.get("device") else 0
            s["owner"] += 1 if r.get("owner") else 0
            s["test"] += 1 if r.get("test") else 0
        return out


def public_view(rec):
    """Admin listing – never contains the key or key hash."""
    return {k: rec.get(k) for k in ("lid", "product", "status", "owner", "test", "email_masked", "name", "source", "order_id",
                                    "created", "revoked_at", "revoke_reason", "key_hint", "activations")} | {
        "device": ({"name": rec["device"].get("name", ""), "id": rec["device"]["hash"][:8], "since": rec["device"].get("since"),
                    "last_seen": rec["device"].get("last_seen"), "app": rec["device"].get("app", "")} if rec.get("device") else None),
        "transfers_30d": len([t for t in rec.get("transfers", []) if time.time() - t < 30 * DAY]),
        "history": rec.get("history", [])[-20:]}
