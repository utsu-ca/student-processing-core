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
