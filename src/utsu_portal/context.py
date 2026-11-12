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


# Portal-wide roles issued by the auth service. `admin` implies every other role and is the only one that can grant roles.
ROLES = {
    "admin": "everything, including granting roles",
    "verifier": "review identity (face + ID) verifications, portal and AGM",
    "forms_editor": "create/edit/publish forms; view and set submission statuses",
    "checkin": "AGM check-in and attendance",
    "finance": "club transactions and grants",
    "clubs": "club registration decisions",
    "auditor": "read, verify and export the audit log",
}


def roles_of(acct: dict) -> set[str]:
    """Effective roles of an account record (or its projection). The legacy `role: admin` field still counts."""
    r = set(acct.get("roles") or []) & set(ROLES)
    if acct.get("role") == "admin":
        r.add("admin")
    return r


@dataclass
class Context:
    store: Store
    settings: Settings
    mailer: Mailer
    bus: EventBus = field(default_factory=EventBus)
    identity: Any = None  # Identity (local store or remote auth service)
    anchor: Any = None  # AnchorProvider
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

    def send_otp(self, purpose: str, subject: str, to: str, payload: dict | None = None) -> bool:
        """Issue a code unless the account is cooling down or over its per-window cap. Wrong-guess counts carry over to the
        new code within the window, so re-requesting codes cannot reset the guess budget. Returns False when throttled
        (callers must answer identically either way)."""
        s, now, key = self.settings, self.now(), f"{purpose}:{subject}"
        code, seen = security.new_otp(), {}

        def issue(o):
            if o and now - o.get("window", 0) < s.otp_window:
                window, sends, attempts = o["window"], o.get("sends", 0), 0 if o.get("used") else o["attempts"]
                if now - o.get("sent", 0) < s.otp_cooldown or sends >= s.otp_max_sends or attempts >= s.otp_attempts:
                    seen["ok"] = False
                    return None
            else:
                window, sends, attempts = now, 0, 0
            seen["ok"] = True
            return {"hash": security.hash_otp(s.secret, key, code), "exp": now + s.otp_ttl, "attempts": attempts,
                    "window": window, "sends": sends + 1, "sent": now, "payload": payload or {}}

        self.store.update("otps", key, issue)
        if not seen.get("ok"):
            return False
        self.mailer.send(to, "Your UTSU verification code", f"Your code is {code}. It expires in {self.settings.otp_ttl // 60} minutes.")
        return True

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

    def require_role(self, *roles: str, allow_query_token: bool = False) -> Callable:
        """Allow verified accounts holding `admin` (superset) or any of `roles`. No roles given = admin only."""
        want = {"admin", *roles}

        def dep(authorization: str | None = Header(None), token: str | None = Query(None) if allow_query_token else None) -> dict:
            a = self._account(authorization, token if allow_query_token else None)
            if a["status"] != "verified" or not (roles_of(a) & want):
                raise HTTPException(403, "insufficient role")
            return a
        return Depends(dep)

    def require_admin(self, allow_query_token: bool = False) -> Callable:
        return self.require_role(allow_query_token=allow_query_token)
