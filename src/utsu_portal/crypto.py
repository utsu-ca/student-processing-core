"""Field-level encryption at rest for sensitive documents (ID/face scans, receipts, audit snapshots)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import Store

PREFIX = "enc:v1:"
SENSITIVE: dict[str, tuple[str, ...]] = {
    "media": ("face", "id_card"),
    "agm_media": ("face", "id_card"),
    "receipts": ("data_url",),
    "audit": ("blob",),
}


class Crypter:
    """`keys` is a comma-separated list of Fernet keys: the first encrypts, all decrypt (so keys can be rotated).
    Without keys a key is derived from the portal secret, which is safe but ties data protection to that one secret."""

    def __init__(self, keys: str | None, fallback_secret: str) -> None:
        ks = [k.strip() for k in (keys or "").split(",") if k.strip()]
        if not ks:
            raw = HKDF(hashes.SHA256(), 32, None, b"utsu-portal-data-v1").derive(fallback_secret.encode())
            ks = [base64.urlsafe_b64encode(raw).decode()]
        self._f = MultiFernet([Fernet(k) for k in ks])

    def enc(self, s: str) -> str:
        return PREFIX + self._f.encrypt(s.encode()).decode()

    def dec(self, s: str) -> str:
        if not s.startswith(PREFIX):
            return s
        try:
            return self._f.decrypt(s[len(PREFIX):].encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("cannot decrypt stored data: wrong or missing PORTAL_DATA_KEY") from e


class EncryptingStore(Store):
    def __init__(self, inner: Store, crypter: Crypter) -> None:
        self.inner, self.c = inner, crypter

    def _out(self, col: str, doc: dict | None) -> dict | None:
        if doc:
            for f in SENSITIVE.get(col, ()):
                if isinstance(doc.get(f), str):
                    doc[f] = self.c.dec(doc[f])
        return doc

    def _in(self, col: str, doc: dict) -> dict:
        out = dict(doc)
        for f in SENSITIVE.get(col, ()):
            if isinstance(out.get(f), str):
                out[f] = self.c.enc(out[f])
        return out

    def get(self, col, id):
        return self._out(col, self.inner.get(col, id))

    def put(self, col, id, doc):
        self.inner.put(col, id, self._in(col, doc))

    def delete(self, col, id):
        self.inner.delete(col, id)

    def create(self, col, id, doc):
        return self.inner.create(col, id, self._in(col, doc))

    def replace(self, col, id, doc, expected_rev):
        return self.inner.replace(col, id, self._in(col, doc), expected_rev)

    def list(self, col, **equals):
        return [self._out(col, d) for d in self.inner.list(col, **equals)]


def fingerprint(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]
