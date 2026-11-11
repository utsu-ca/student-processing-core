from __future__ import annotations

from pathlib import Path

import hashlib
import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import formdef, security
from .audit import AuditLog
from .crypto import Crypter, EncryptingStore
from .identity import LocalIdentity, RemoteIdentity, http_fetch
from .events import bus_from_settings
from .modules.audit import AuditModule
from .anchor import provider_from_settings
from .ratelimit import limiter_from_settings
from .context import Context
from .mailer import Mailer, mailer_from_env
from .modules.admin import AdminModule
from .modules.agm import AgmModule
from .modules.agm_verify import AgmVerifyModule
from .modules.auth import AuthModule
from .modules.clubs import ClubsModule
from .modules.roles import RolesModule
from .modules.sso import SSOModule
from .modules.forms import FormsModule
from .settings import FORMS_DIR, Settings
from .store import ACCESS, AccessLog, AuditedStore, Store, open_store

STATIC = Path(__file__).parent / "static"
CORE_MODULES = (SSOModule(), AuthModule(), AgmVerifyModule(), AgmModule(), FormsModule(), AdminModule(), ClubsModule(), AuditModule(), RolesModule())
REMOTE_MODULES = (SSOModule(), AgmVerifyModule(), AgmModule(), FormsModule(), ClubsModule(), AuditModule())  # no auth/admin-verification
RATE_LIMITED = ("/api/auth/", "/api/recovery", "/sso/token", "/sso/authorize")
log = logging.getLogger("utsu_portal")


def seed_forms(ctx: Context, directory: Path = FORMS_DIR) -> None:
    """Load bundled example forms. A changed file with an unchanged version is an error (version bump required)."""
    for p in sorted(directory.glob("*.yaml")):
        text = p.read_text(encoding="utf-8")
        schema = formdef.parse_yaml(text)
        h = formdef.schema_hash(schema)
        key = f"{schema['id']}@{schema['version']}"
        ex = ctx.store.get("form_versions", key)
        if ex and ex["hash"] != h:
            raise RuntimeError(f"{p.name}: definition changed but version {schema['version']} was not bumped")
        f = ctx.store.get("forms", schema["id"]) or {"id": schema["id"], "rev": 0, "published_version": None}
        if not ex:
            ctx.store.put("form_versions", key, {"id": schema["id"], "version": schema["version"], "hash": h, "schema": schema,
                                                 "yaml": text, "published_at": ctx.now(), "by": "seed"})
            f.update(rev=f["rev"] + 1, draft_yaml=text, draft_hash=h, draft_version=schema["version"],
                     published_version=schema["version"])
            ctx.store.put("forms", schema["id"], f)


def create_app(settings: Settings | None = None, store: Store | None = None, mailer: Mailer | None = None,
               extra_modules: tuple = (), identity=None) -> FastAPI:
    """The portal. With `PORTAL_AUTH_URL` (or an explicit `identity`) it holds no accounts and no signing key: identity is
    delegated to a separately deployed auth service (see `authsvc.create_auth_app`)."""
    settings = settings or Settings.from_env()
    if identity is None and settings.auth_url:
        if not (settings.auth_public_key and settings.service_token):
            raise RuntimeError("PORTAL_AUTH_URL requires PORTAL_AUTH_PUBLIC_KEY and PORTAL_SERVICE_TOKEN")
        identity = RemoteIdentity(settings.auth_public_key, http_fetch(settings.auth_url, settings.service_token))
    modules = CORE_MODULES if identity is None else REMOTE_MODULES
    return build_app(settings, store, mailer, (*modules, *extra_modules), identity, seed=settings.seed_forms, ui=True)


def build_app(settings: Settings, store: Store | None, mailer: Mailer | None, modules: tuple, identity=None,
              seed: bool = True, ui: bool = True) -> FastAPI:
    store = EncryptingStore(store or open_store(settings.store), Crypter(settings.data_key, settings.secret))
    if settings.audit_db_access:
        store = AuditedStore(store)
    ctx = Context(store, settings, mailer or mailer_from_env(), bus=bus_from_settings(settings))
    ctx.identity = identity or LocalIdentity(ctx)
    ctx.audit, ctx.limiter = AuditLog(ctx), limiter_from_settings(settings)
    ctx.anchor = provider_from_settings(settings, store)
    app = FastAPI(title="UTSU applicant portal", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    if seed:
        seed_forms(ctx)
    for m in modules:
        app.include_router(m.router(ctx))

    def actor_of(request: Request) -> str | None:
        raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return ctx.identity.subject(raw) if raw else None

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        path = request.url.path
        who = actor_of(request)
        pid = hmac.new(settings.secret.encode(), client_ip(request).encode(), hashlib.sha256).hexdigest()[:12]
        access = AccessLog(who or f"anonymous:{pid}") if settings.audit_db_access and not path.startswith(("/static", "/healthz", "/readyz")) else None
        token = ACCESS.set(access)  # every store call made while serving this request is attributed to the requester
        try:
            resp = await call_next(request)
        finally:
            ACCESS.reset(token)
        if access is not None and access.ops and not path.startswith("/api/admin/auth-audit"):
            try:  # one digest entry per request: which identity touched which collections/ids (never values)
                await run_in_threadpool(ctx.audit.record, access.requester, "db.access", getattr(request.scope.get("route"), "path", path),
                                        None, None, {"ops": access.ops, "method": request.method, "status": resp.status_code})
            except Exception:
                log.exception("access audit failed")
        watched = request.method not in ("GET", "HEAD", "OPTIONS") or path.startswith("/api/admin/")
        if watched and not path.startswith("/api/admin/audit") and not path.startswith("/static"):
            route = request.scope.get("route")
            try:  # no bodies, query strings or tokens are logged; IPs are keyed-hashed
                await run_in_threadpool(ctx.audit.record, who, f"http.{request.method}",
                                        getattr(route, "path", path), None, None,
                                        {"status": resp.status_code, "ip": pid, "params": {k: str(v)[:80] for k, v in request.path_params.items()}})
            except Exception:
                log.exception("audit write failed")
        return resp

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if int(request.headers.get("content-length") or 0) > settings.max_body_bytes:
            return JSONResponse({"detail": "request too large"}, 413)
        if request.method == "POST" and request.url.path.startswith(RATE_LIMITED):
            if not await run_in_threadpool(ctx.limiter.allow, f"auth:{client_ip(request)}", settings.rate_limit_auth, settings.rate_window):
                return JSONResponse({"detail": "too many requests"}, 429, headers={"Retry-After": str(settings.rate_window)})
        return await call_next(request)

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if settings.hsts:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith(("/api/", "/sso/")):
            h.setdefault("Cache-Control", "no-store")
        return resp

    if ui:
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        ok = {"store": True, "redis": ctx.bus.ping(), "identity": ctx.identity.ping()}
        try:
            ctx.store.get("health", "probe")
        except Exception:
            ok["store"] = False
        return JSONResponse(ok, 200 if all(ok.values()) else 503)

    if ui:
        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")

    return app
