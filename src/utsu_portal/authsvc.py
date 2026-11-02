"""Standalone authentication service: `uvicorn --factory utsu_portal.authsvc:create_auth_app`.

Owns accounts, users, OTPs, identity verification review and the session-signing key. Runs with its own store,
secret and audit log. The portal talks to it only through (a) Ed25519-signed bearer tokens it can verify offline and
(b) the `/internal` API below, which is gated by a service token and should be reachable only from the portal."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .app import build_app
from .context import Context
from .mailer import Mailer, mailer_from_env
from .modules.admin import AdminModule
from .modules.audit import AuditModule
from .modules.auth import AuthModule
from .settings import Settings
from .store import Store


class Lookup(BaseModel):
    username: str


class InternalModule:
    name = "internal"

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/internal", include_in_schema=False)
        want = ctx.settings.service_token

        def guard(authorization: str | None) -> None:
            got = (authorization or "").removeprefix("Bearer ").strip()
            if not want or not got or not hmac.compare_digest(got.encode(), want.encode()):
                raise HTTPException(404)  # indistinguishable from a missing route when unauthenticated

        def view(uid: str):
            a, u = ctx.store.get("accounts", uid), ctx.store.get("users", uid)
            if not a or not u:
                return None
            # minimal projection: no hashes, no email/student-id lookup keys
            return {"account": {k: a.get(k) for k in ("uuid", "status", "role", "email_verified")}, "user": u}

        @r.get("/ping")
        def ping(authorization: str | None = Header(None)):
            guard(authorization)
            return {"ok": True}

        @r.get("/accounts/{uid}")
        def account(uid: str, authorization: str | None = Header(None)):
            guard(authorization)
            d = view(uid)
            if not d:
                raise HTTPException(404)
            return d

        @r.post("/resolve")
        def resolve(b: Lookup, authorization: str | None = Header(None)):
            guard(authorization)
            uid = ctx.uid(b.username)
            d = view(uid)
            if not d:
                raise HTTPException(404)
            return {"uuid": uid, **d}

        return r


def create_auth_app(settings: Settings | None = None, store: Store | None = None, mailer: Mailer | None = None):
    settings = settings or Settings.from_env()
    if settings.service_token is not None and len(settings.service_token) < 32:
        raise RuntimeError("PORTAL_SERVICE_TOKEN must be at least 32 characters")
    mods = (AuthModule(), AdminModule(), AuditModule("/api/admin/auth-audit"), InternalModule())
    return build_app(settings, store, mailer or mailer_from_env(), mods, seed=False, ui=False)
