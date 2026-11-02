"""Shared application context handed to every module."""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, Query

from . import security
from .events import EventBus
from .mailer import Mailer
from .settings import Settings
from .store import Conflict, Store


@dataclass
class Context:
    store: Store
    settings: Settings
    mailer: Mailer
    bus: EventBus = field(default_factory=EventBus)
    identity: Any = None  # Identity (local store or remote auth service)
    audit: Any = None  # AuditLog, set by create_app
    limiter: Any = None
    services: dict = field(default_factory=dict)  # shared objects between modules (e.g. the SSO provider)
    # --- helpers -----------------------------------------------------------
    def now(self) -> int:
        return int(time.time())

    def uid(self, username: str) -> str:
        return security.username_uuid(self.settings.secret, username)

    def email_key(self, email: str) -> str:
        return hashlib.sha256(email.lower().encode()).hexdigest()

    def send_otp(self, purpose: str, subject: str, to: str, payload: dict | None = None) -> None:
        code = security.new_otp()
        self.store.put("otps", f"{purpose}:{subject}", {
            "hash": security.hash_otp(self.settings.secret, f"{purpose}:{subject}", code),
            "exp": self.now() + self.settings.otp_ttl, "attempts": 0, "payload": payload or {}})
        self.mailer.send(to, "Your UTSU verification code", f"Your code is {code}. It expires in {self.settings.otp_ttl // 60} minutes.")

    def check_otp(self, purpose: str, subject: str, code: str, consume: bool = True) -> dict | None:
        key = f"{purpose}:{subject}"
        seen: dict = {}

        def step(o):
            """Optimistic lock: the attempt is counted (and a correct code marked used) atomically, so neither parallel
            guesses nor a replayed correct code can get past the limits."""
            if not o or o.get("used") or o["exp"] < self.now() or o["attempts"] >= self.settings.otp_attempts:
                seen["ok"] = False
                return None
            ok = hmac.compare_digest(security.hash_otp(self.settings.secret, key, str(code)), o["hash"])
            seen["ok"], seen["payload"] = ok, o["payload"]
            if ok:
                return {**o, "used": True} if consume else None
            return {**o, "attempts": o["attempts"] + 1}

        try:
            self.store.update("otps", key, step)
        except Conflict:
            return None
        if not seen.get("ok"):
            return None
        if consume:
            self.store.delete("otps", key)
        return seen["payload"] or {}

    # --- auth dependencies ---------------------------------------------------
    def _account(self, authorization: str | None, token: str | None) -> dict:
        raw = token or (authorization or "").removeprefix("Bearer ").strip()
        acct = self.identity.authenticate(raw) if raw else None
        if not acct:
            raise HTTPException(401, "not authenticated")
        return acct

    def require_user(self) -> Callable:
        def dep(authorization: str | None = Header(None)) -> dict:
            return self._account(authorization, None)
        return Depends(dep)

    def require_verified(self) -> Callable:
        def dep(authorization: str | None = Header(None)) -> dict:
            a = self._account(authorization, None)
            if a["status"] != "verified":
                raise HTTPException(403, "account not verified")
            return a
        return Depends(dep)

    def require_admin(self, allow_query_token: bool = False) -> Callable:
        def dep(authorization: str | None = Header(None), token: str | None = Query(None) if allow_query_token else None) -> dict:
            a = self._account(authorization, token if allow_query_token else None)
            if a.get("role") != "admin" or a["status"] != "verified":
                raise HTTPException(403, "admin only")
            return a
        return Depends(dep)
