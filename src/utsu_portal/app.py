from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import formdef
from .context import Context
from .mailer import Mailer
from .modules.admin import AdminModule
from .modules.agm import AgmModule
from .modules.agm_verify import AgmVerifyModule
from .modules.auth import AuthModule
from .modules.clubs import ClubsModule
from .modules.sso import SSOModule
from .modules.forms import FormsModule
from .settings import FORMS_DIR, Settings
from .store import Store, open_store

STATIC = Path(__file__).parent / "static"
CORE_MODULES = (SSOModule(), AuthModule(), AgmVerifyModule(), AgmModule(), FormsModule(), AdminModule(), ClubsModule())


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
               extra_modules: tuple = ()) -> FastAPI:
    settings = settings or Settings.from_env()
    ctx = Context(store or open_store(settings.store), settings, mailer or Mailer())
    app = FastAPI(title="UTSU applicant portal")
    app.state.ctx = ctx
    if settings.seed_forms:
        seed_forms(ctx)
    for m in (*CORE_MODULES, *extra_modules):
        app.include_router(m.router(ctx))

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:")
        return resp

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
