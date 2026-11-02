"""Identity boundary. The portal never touches accounts/users directly: it asks an `Identity`.

* `LocalIdentity`  – monolith: reads the shared store, signs and verifies tokens itself.
* `RemoteIdentity` – split deployment: verifies Ed25519 tokens offline with the auth service's PUBLIC key (so a
  compromised portal can not mint sessions) and asks the auth service for live account status over a
  service-authenticated internal API."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from . import security

log = logging.getLogger("utsu_portal.identity")


class Identity:
    def subject(self, raw: str) -> str | None: ...
    def account(self, uuid: str) -> dict | None: ...
    def user(self, uuid: str) -> dict | None: ...
    def resolve(self, username: str) -> dict | None: ...
    def ping(self) -> bool:
        return True

    def authenticate(self, raw: str) -> dict | None:
        sub = self.subject(raw) if raw else None
        acct = self.account(sub) if sub else None
        return acct if acct and acct.get("email_verified") else None


class LocalIdentity(Identity):
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.key = security.load_signing_key(ctx.settings.auth_signing_key, ctx.settings.secret)
        self.pub = [self.key.public_key()]

    def subject(self, raw):
        return security.read_user_token(self.pub, raw)

    def issue(self, uuid: str) -> str:
        return security.sign_user_token(self.key, uuid, self.ctx.settings.token_ttl)

    def account(self, uuid):
        return self.ctx.store.get("accounts", uuid)

    def user(self, uuid):
        return self.ctx.store.get("users", uuid)

    def resolve(self, username):
        uid = self.ctx.uid(username)
        a = self.account(uid)
        return {"uuid": uid, "account": a, "user": self.user(uid)} if a else None


Fetch = Callable[[str, dict | None], dict | None]


def http_fetch(base: str, service_token: str, timeout: float = 3.0) -> Fetch:
    base = base.rstrip("/")
    if not base.startswith("https://") and "localhost" not in base and "127.0.0.1" not in base and ".svc" not in base:
        raise ValueError("PORTAL_AUTH_URL must be https:// (or a cluster-internal .svc address)")

    def fetch(path: str, body: dict | None = None) -> dict | None:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + service_token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (scheme validated above)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    return fetch


class RemoteIdentity(Identity):
    CACHE_TTL = 15  # seconds a status lookup may be reused: bounds how long a revoked account/admin keeps access

    def __init__(self, public_pems: str, fetch: Fetch) -> None:
        self.pub, self.fetch, self._cache = security.load_public_keys(public_pems), fetch, {}

    def subject(self, raw):
        return security.read_user_token(self.pub, raw)

    def _get(self, uuid: str) -> dict | None:
        hit = self._cache.get(uuid)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            d = self.fetch(f"/internal/accounts/{uuid}", None)
        except Exception:
            log.exception("auth service unreachable")  # fail closed: no identity, no access
            self._cache.pop(uuid, None)
            return None
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[uuid] = (time.time() + self.CACHE_TTL, d)
        return d

    def account(self, uuid):
        d = self._get(uuid)
        return d and d["account"]

    def user(self, uuid):
        d = self._get(uuid)
        return d and d["user"]

    def resolve(self, username):
        try:
            return self.fetch("/internal/resolve", {"username": username})
        except Exception:
            log.exception("auth service unreachable")
            return None

    def ping(self):
        try:
            return bool(self.fetch("/internal/ping", None))
        except Exception:
            return False
