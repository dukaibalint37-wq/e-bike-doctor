"""Creates a signed update manifest (run on the owner's PC; the private key never goes to the server or into an app).

  python tools/sign_update_manifest.py --key C:\\EBD_SIGNING\\update-manifest-signing\\private.pem --product EBD-WIN \
      --version 1.1.0 --file path\\to\\EBikeDoctorPRO-Setup-1.1.0.exe --out updates/windows.json \
      --notes-hu "..." --notes-de "..." --notes-en "..."

Format: {"payload": base64url(canonical JSON), "signature": base64url(ECDSA P-256 P1363)} – verifiers check the signature over the
exact payload bytes, then parse it. Payload: product, version, sha256, size, file_name, published, min_os, notes{hu,de,en}.
"""
import argparse, base64, datetime, hashlib, json, os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True); ap.add_argument("--product", required=True, choices=["EBD-WIN", "EBD-AND"])
    ap.add_argument("--version", required=True); ap.add_argument("--file", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--min-os", default=""); ap.add_argument("--notes-hu", default=""); ap.add_argument("--notes-de", default=""); ap.add_argument("--notes-en", default="")
    a = ap.parse_args()
    data = open(a.file, "rb").read()
    payload = {"product": a.product, "version": a.version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
               "file_name": os.path.basename(a.file), "published": datetime.date.today().isoformat(), "min_os": a.min_os,
               "notes": {"hu": a.notes_hu, "de": a.notes_de, "en": a.notes_en}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = serialization.load_pem_private_key(open(a.key, "rb").read(), password=None)
    r, s = utils.decode_dss_signature(key.sign(raw, ec.ECDSA(hashes.SHA256())))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump({"payload": b64u(raw), "signature": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}, open(a.out, "w", encoding="utf-8"), indent=2)
    print("manifest written:", a.out, payload["version"], payload["sha256"][:16], payload["size"])


if __name__ == "__main__":
    main()
