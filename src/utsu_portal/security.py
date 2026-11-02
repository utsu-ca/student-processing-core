from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import unicodedata
import uuid

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(s: str, max_len: int = 2000, multiline: bool = False) -> str:
    """NFC-normalise, drop control/bidi characters, trim and cap length."""
    s = unicodedata.normalize("NFC", str(s))
    if not multiline:
        s = re.sub(r"\s+", " ", s)
    s = _CTRL.sub("", s).strip()
    return s[:max_len]


def _hid(secret: str, kind: str, value: str) -> str:
    d = hmac.new(secret.encode(), f"{kind}:{value}".encode(), hashlib.sha256).digest()[:16]
    return str(uuid.UUID(bytes=d, version=5))


def username_uuid(secret: str, username: str) -> str:
    return _hid(secret, "user", clean_text(username).lower())


def student_id_uuid(secret: str, student_number: str) -> str:
    return _hid(secret, "sid", re.sub(r"\D", "", student_number))


def truncate_student_number(student_number: str) -> str:
    return re.sub(r"\D", "", student_number)[-4:]


def sign_token(secret: str, sub: str, ttl: int, scope: str = "user") -> str:
    body = f"{sub}.{int(time.time()) + ttl}.{scope}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{body}.{sig}".encode()).decode()


def read_token(secret: str, token: str, want_scope: str = "user") -> str | None:
    try:
        sub, exp, scope, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(".", 3)
        body = f"{sub}.{exp}.{scope}"
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:40]
        if hmac.compare_digest(sig, good) and int(exp) > time.time() and scope == want_scope:
            return sub
    except Exception:
        pass
    return None


def new_otp() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def hash_otp(secret: str, subject: str, code: str) -> str:
    return hmac.new(secret.encode(), f"otp:{subject}:{code}".encode(), hashlib.sha256).hexdigest()


# ---- asymmetric session tokens (EdDSA): services that only *verify* never hold signing material ----
import json as _json
from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _HKDF

TOKEN_ISS, TOKEN_AUD = "utsu-auth", "utsu-portal"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_signing_key(pem: str | None, secret: str) -> Ed25519PrivateKey:
    """Explicit PEM key (recommended for a standalone auth service); otherwise derived from the portal secret so replicas agree."""
    if pem:
        k = _ser.load_pem_private_key(pem.encode(), None)
        if not isinstance(k, Ed25519PrivateKey):
            raise ValueError("PORTAL_AUTH_SIGNING_KEY must be an Ed25519 private key")
        return k
    return Ed25519PrivateKey.from_private_bytes(_HKDF(_hashes.SHA256(), 32, None, b"utsu-auth-token-v1").derive(secret.encode()))


def public_pem(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(_ser.Encoding.PEM, _ser.PublicFormat.SubjectPublicKeyInfo).decode()


def load_public_keys(pems: str) -> list[Ed25519PublicKey]:
    """One or more concatenated PEM public keys (several during key rotation)."""
    keys = []
    for blk in pems.split("-----END PUBLIC KEY-----"):
        if "BEGIN PUBLIC KEY" in blk:
            k = _ser.load_pem_public_key((blk + "-----END PUBLIC KEY-----").strip().encode())
            if not isinstance(k, Ed25519PublicKey):
                raise ValueError("auth public key must be Ed25519")
            keys.append(k)
    if not keys:
        raise ValueError("no public key found")
    return keys


def sign_user_token(key: Ed25519PrivateKey, sub: str, ttl: int) -> str:
    now = int(time.time())
    body = _b64u(_json.dumps({"sub": sub, "iat": now, "exp": now + ttl, "iss": TOKEN_ISS, "aud": TOKEN_AUD}, separators=(",", ":")).encode())
    return f"{body}.{_b64u(key.sign(body.encode()))}"


def read_user_token(keys: list[Ed25519PublicKey], raw: str) -> str | None:
    try:
        body, sig = raw.split(".")
        for k in keys:
            try:
                k.verify(_unb64u(sig), body.encode())
                break
            except Exception:
                continue
        else:
            return None
        c = _json.loads(_unb64u(body))
        if c["iss"] == TOKEN_ISS and c["aud"] == TOKEN_AUD and int(c["exp"]) > time.time() and isinstance(c["sub"], str):
            return c["sub"]
    except Exception:
        pass
    return None
