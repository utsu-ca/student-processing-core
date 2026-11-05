"""Blockchain anchoring. A provider publishes one 32-byte checkpoint digest somewhere an operator cannot rewrite.
Only the digest leaves the system (never log content), and providers can be disabled with `PORTAL_ANCHOR=none`.

`PORTAL_ANCHOR`: `none` (default) | `local` (dev/test ledger in the store) | `ots` (OpenTimestamps calendar, Bitcoin-backed,
no keys or fees) | `http` (POST to `PORTAL_ANCHOR_URL`, a gateway you run that signs and sends e.g. an EVM transaction) |
`pkg.module:factory` returning an `AnchorProvider`."""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.request


class AnchorProvider:
    name = "none"
    enabled = False

    def submit(self, digest: str, meta: dict) -> dict:
        """Publish `digest` (hex). Returns {'status': 'submitted'|'confirmed', 'ref': ..., 'proof': optional base64}."""
        raise NotImplementedError

    def confirm(self, record: dict) -> dict | None:
        """Optionally upgrade a 'submitted' record (e.g. wait for block inclusion). None = no change."""
        return None


class NullProvider(AnchorProvider):
    pass


class LocalLedgerProvider(AnchorProvider):
    """Development ledger: a second hash chain in the same store. Proves the plumbing, not independence."""
    name, enabled = "local", True

    def __init__(self, store) -> None:
        self.store = store

    def submit(self, digest, meta):
        def add(h):
            h = h or {"id": "tip", "n": 0, "tip": ""}
            return {**h, "n": h["n"] + 1, "tip": hashlib.sha256(f"{h['tip']}|{digest}".encode()).hexdigest()}
        tip = self.store.update("anchor_ledger", "tip", add)
        return {"status": "confirmed", "ref": f"local:{tip['n']}:{tip['tip'][:16]}"}


class OpenTimestampsProvider(AnchorProvider):
    name, enabled = "ots", True
    CALENDAR = "https://alice.btc.calendar.opentimestamps.org"

    def __init__(self, calendar: str | None = None, timeout: float = 10.0) -> None:
        self.url, self.timeout = (calendar or self.CALENDAR).rstrip("/"), timeout
        if not self.url.startswith("https://"):
            raise ValueError("OpenTimestamps calendar must be https")

    def submit(self, digest, meta):
        req = urllib.request.Request(self.url + "/digest", data=bytes.fromhex(digest), headers={"Accept": "application/vnd.opentimestamps.v1", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (https enforced above)
            proof = r.read(1_000_000)
        return {"status": "submitted", "ref": f"ots:{self.url}", "proof": base64.b64encode(proof).decode()}  # pending proof; upgrade with `ots upgrade`


class HttpGatewayProvider(AnchorProvider):
    """POST {"digest": hex, ...meta} with a bearer token to your own relay and store the `{"tx": ...}` it returns."""
    name, enabled = "http", True

    def __init__(self, url: str, token: str | None = None, timeout: float = 15.0) -> None:
        if not url.startswith("https://"):
            raise ValueError("PORTAL_ANCHOR_URL must be https")
        self.url, self.token, self.timeout = url, token, timeout

    def submit(self, digest, meta):
        h = {"Content-Type": "application/json", **({"Authorization": "Bearer " + self.token} if self.token else {})}
        req = urllib.request.Request(self.url, data=json.dumps({"digest": digest, **meta}).encode(), headers=h)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
            out = json.loads(r.read(100_000))
        return {"status": out.get("status", "submitted"), "ref": str(out.get("tx", ""))[:200]}


def provider_from_settings(settings, store) -> AnchorProvider:
    spec = (settings.anchor or "none").strip()
    if spec == "none":
        return NullProvider()
    if spec == "local":
        return LocalLedgerProvider(store)
    if spec == "ots":
        return OpenTimestampsProvider(settings.anchor_url)
    if spec == "http":
        return HttpGatewayProvider(settings.anchor_url or "", settings.anchor_token)
    if ":" in spec:
        import importlib

        mod, _, fn = spec.partition(":")
        return getattr(importlib.import_module(mod), fn)()
    raise ValueError(f"unknown PORTAL_ANCHOR {spec!r}")
