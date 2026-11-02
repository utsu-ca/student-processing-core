"""Form templates (admin), published static forms (public) and submissions with status pages."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import formdef
from ..context import Context

name = "forms"
STATUSES = ("submitted", "under_review", "needs_info", "accepted", "rejected")


class DraftIn(BaseModel):
    yaml: str | None = None
    schema_: dict | None = Field(None, alias="schema")
    base_rev: int | None = None

    model_config = {"populate_by_name": True}


class SubmitIn(BaseModel):
    version: int
    hash: str
    data: dict


class StatusIn(BaseModel):
    status: str
    note: str = ""


class FormsModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api")
        S = ctx.store
        admin = ctx.require_admin()
        admin_q = ctx.require_admin(allow_query_token=True)
        verified = ctx.require_verified()

        def published(fid: str) -> dict:
            f = S.get("forms", fid)
            if not f or not f.get("published_version"):
                raise HTTPException(404, "form not published")
            return S.get("form_versions", f"{fid}@{f['published_version']}")

        # ---------- public / applicant ----------
        @r.get("/forms")
        def list_forms():
            out = []
            for f in S.list("forms"):
                if f.get("published_version"):
                    v = S.get("form_versions", f"{f['id']}@{f['published_version']}")
                    out.append({"id": f["id"], "title": v["schema"]["title"], "version": v["version"], "hash": v["hash"]})
            return sorted(out, key=lambda x: x["id"])

        @r.get("/forms/{fid}/static")
        def static_form(fid: str, response: Response, request: Request):
            v = published(fid)
            etag = f'"{v["hash"]}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag})
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "public, max-age=60"
            return {"schema": v["schema"], "version": v["version"], "hash": v["hash"], "yaml": v["yaml"]}

        @r.put("/forms/{fid}/submission")
        def submit(fid: str, b: SubmitIn, acct: dict = verified):
            v = published(fid)
            if (b.version, b.hash) != (v["version"], v["hash"]):
                raise HTTPException(409, {"error": "form changed; reload", "version": v["version"], "hash": v["hash"]})
            try:
                data = formdef.validate_submission(v["schema"], b.data)
            except formdef.FormError as e:
                raise HTTPException(422, e.errors)
            key = f"{fid}:{acct['uuid']}"
            prev = S.get("submissions", key)
            if prev and prev["status"] in ("accepted", "rejected"):
                raise HTTPException(409, "submission is final")
            hist = (prev or {}).get("history", []) + [{"at": ctx.now(), "status": "submitted", "note": ""}]
            S.put("submissions", key, {"id": key, "form_id": fid, "uuid": acct["uuid"], "version": v["version"], "hash": v["hash"],
                                       "data": data, "status": "submitted", "updated_at": ctx.now(), "history": hist})
            ctx.audit.record(acct["uuid"], "submission.saved", f"form:{fid}", (prev or {}).get("data"), data, {"version": v["version"], "revision": len(hist)})
            return {"status": "submitted"}

        @r.get("/me/submissions")
        def my_submissions(acct: dict = ctx.require_user()):
            return [{k: s[k] for k in ("form_id", "version", "status", "updated_at", "history", "data")}
                    | {"note": s["history"][-1].get("note", "")} for s in S.list("submissions", uuid=acct["uuid"])]

        # ---------- admin: templates ----------
        def _ensure(fid: str) -> dict:
            f = S.get("forms", fid)
            if not f:
                raise HTTPException(404, "unknown form")
            return f

        @r.get("/admin/forms")
        def admin_list(_: dict = admin):
            return [{"id": f["id"], "rev": f["rev"], "published_version": f.get("published_version")} for f in S.list("forms")]

        @r.get("/admin/forms/{fid}")
        def admin_get(fid: str, _: dict = admin):
            f = _ensure(fid)
            versions = sorted(v["version"] for v in S.list("form_versions", id=fid))
            return {**f, "versions": versions}

        @r.post("/admin/forms/convert")
        def convert(b: DraftIn, _: dict = admin):
            """YAML<->schema conversion for the WYSIWYG builder."""
            try:
                if b.yaml is not None:
                    return {"schema": formdef.parse_yaml(b.yaml)}
                return {"yaml": formdef.to_yaml(formdef.validate_schema(b.schema_))}
            except formdef.FormError as e:
                raise HTTPException(422, e.errors)

        @r.put("/admin/forms/{fid}/draft")
        def save_draft(fid: str, b: DraftIn, a: dict = admin):
            try:
                schema = formdef.parse_yaml(b.yaml) if b.yaml is not None else formdef.validate_schema(b.schema_)
            except formdef.FormError as e:
                raise HTTPException(422, e.errors)
            if schema["id"] != fid:
                raise HTTPException(422, "id in the definition must match the URL")
            h = formdef.schema_hash(schema)
            f = S.get("forms", fid) or {"id": fid, "rev": 0, "published_version": None}
            if b.base_rev is not None and b.base_rev != f["rev"]:
                raise HTTPException(409, {"error": "draft changed by someone else", "rev": f["rev"], "yaml": f.get("draft_yaml")})
            existing = S.get("form_versions", f"{fid}@{schema['version']}")
            if existing and existing["hash"] != h:
                raise HTTPException(409, f"version {schema['version']} is already published with a different definition; bump the version")
            old = f.get("draft_yaml")
            f.update(rev=f["rev"] + 1, draft_yaml=formdef.to_yaml(schema), draft_hash=h, draft_version=schema["version"], updated_by=a["uuid"])
            S.put("forms", fid, f)
            ctx.audit.record(a["uuid"], "form.draft_saved", f"form:{fid}", {"yaml": old}, {"yaml": f["draft_yaml"]}, {"rev": f["rev"], "version": schema["version"]})
            ctx.bus.publish(f"form:{fid}", {"rev": f["rev"], "yaml": f["draft_yaml"], "by": a["uuid"]})
            return {"rev": f["rev"], "hash": h, "version": schema["version"]}

        @r.post("/admin/forms/{fid}/publish")
        def publish(fid: str, a: dict = admin):
            f = _ensure(fid)
            schema = formdef.parse_yaml(f["draft_yaml"])
            h = formdef.schema_hash(schema)
            key = f"{fid}@{schema['version']}"
            ex = S.get("form_versions", key)
            if ex and ex["hash"] != h:
                raise HTTPException(409, "this version was published with a different definition; bump the version")
            cur = f.get("published_version")
            if cur and schema["version"] < cur:
                raise HTTPException(409, "version is older than the currently published one")
            if not ex:
                S.put("form_versions", key, {"id": fid, "version": schema["version"], "hash": h, "schema": schema,
                                             "yaml": f["draft_yaml"], "published_at": ctx.now(), "by": a["uuid"]})
            prev_pub = f.get("published_version")
            f["published_version"] = schema["version"]
            S.put("forms", fid, f)
            ctx.audit.record(a["uuid"], "form.published", f"form:{fid}", {"published_version": prev_pub},
                             {"published_version": f["published_version"], "yaml": f["draft_yaml"]}, {"hash": h})
            ctx.bus.publish(f"form:{fid}", {"rev": f["rev"], "published_version": f["published_version"]})
            return {"published_version": f["published_version"], "hash": h}

        @r.post("/admin/forms/{fid}/unpublish")
        def unpublish(fid: str, a: dict = admin):
            f = _ensure(fid)
            prev_pub = f.get("published_version")
            f["published_version"] = None
            S.put("forms", fid, f)
            ctx.audit.record(a["uuid"], "form.unpublished", f"form:{fid}", {"published_version": prev_pub}, {"published_version": None})
            ctx.bus.publish(f"form:{fid}", {"rev": f["rev"], "published_version": None})
            return {"published_version": None}

        @r.get("/admin/forms/{fid}/events")
        async def events(fid: str, request: Request, _: dict = admin_q):
            async def gen():
                yield ": connected\n\n"
                async for ev in ctx.bus.listen(f"form:{fid}", request.is_disconnected):
                    yield ": keepalive\n\n" if ev is None else f"data: {json.dumps(ev)}\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

        # ---------- admin: submissions ----------
        @r.get("/admin/forms/{fid}/submissions")
        def admin_submissions(fid: str, _: dict = admin):
            out = []
            for s in S.list("submissions", form_id=fid):
                u = ctx.identity.user(s["uuid"]) or {}
                out.append({**s, "applicant": {k: u.get(k) for k in ("preferred_name", "first_name", "last_name", "email", "student_number")}})
            return out

        @r.post("/admin/submissions/{fid}/{uid}/status")
        def set_status(fid: str, uid: str, b: StatusIn, a: dict = admin):
            s = S.get("submissions", f"{fid}:{uid}")
            if not s:
                raise HTTPException(404, "no such submission")
            if b.status not in STATUSES:
                raise HTTPException(422, f"status must be one of {STATUSES}")
            old_status = s["status"]
            s["status"] = b.status
            s["updated_at"] = ctx.now()
            s["history"].append({"at": ctx.now(), "status": b.status, "note": b.note[:500]})
            S.put("submissions", f"{fid}:{uid}", s)
            ctx.audit.record(a["uuid"], "submission.status", f"form:{fid}", {"status": old_status}, {"status": b.status}, {"applicant": uid, "note": b.note[:200]})
            return {"status": b.status}

        return r
