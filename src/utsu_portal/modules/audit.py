"""Admin API for the audit log: browse, compare revisions (Wikipedia-style diff), verify integrity, export."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Response

from .. import audit as A
from ..context import Context

name = "audit"


class AuditModule:
    name = name

    def __init__(self, prefix: str = "/api/admin/audit") -> None:
        self.prefix = prefix  # the standalone auth service uses a different prefix so ingress can route both

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix=self.prefix)
        admin = ctx.require_role("auditor")
        log: A.AuditLog = ctx.audit

        def entry(eid: str) -> dict:
            d = log.get(int(eid)) if eid.isdigit() else None
            if not d:
                raise HTTPException(404, "no such audit entry")
            return d

        @r.get("")
        def list_(actor: str | None = None, action: str | None = None, target: str | None = None,
                  before_seq: int | None = None, limit: int = 50, a: dict = admin):
            rows = [d for d in log.entries() if (not actor or d["actor"] == actor) and (not action or d["action"].startswith(action))
                    and (not target or d["target"] == target) and (before_seq is None or d["seq"] < before_seq)]
            page = rows[-max(1, min(limit, 200)):][::-1]
            log.record(a["uuid"], "audit.read", meta={"returned": len(page)})
            return {"entries": [log.public(d) for d in page], "next_before_seq": page[-1]["seq"] if len(page) == limit else None}

        @r.get("/verify")
        def verify(_: dict = admin):
            return log.verify()

        @r.get("/checkpoints")
        def checkpoints(_: dict = admin):
            return sorted(ctx.store.list("audit_checkpoints"), key=lambda c: c["to_seq"], reverse=True)[:100]

        @r.get("/checks")
        def checks(_: dict = admin):
            return sorted(ctx.store.list("audit_checks"), key=lambda c: c["at"], reverse=True)[:20]

        @r.get("/tables")
        def tables(_: dict = admin):
            return [{k: v for k, v in m.items() if k != "id"} for m in log.months(("hot", "archived", "dropped"))]

        @r.post("/maintenance/run")
        def run(force: bool = False, a: dict = ctx.require_admin()):
            from .. import maintenance
            log.record(a["uuid"], "audit.maintenance_run", meta={"force": force})
            return maintenance.run_due(ctx, force)

        @r.get("/archive/{month}")
        def archive(month: str, a: dict = admin):
            if not re.fullmatch(r"\d{4}_\d{2}", month) or not ctx.store.get("audit_archive", month):
                raise HTTPException(404, "no such archive")
            log.record(a["uuid"], "audit.read", f"archive:{month}")
            return {"entries": [log.public(d) for d in log.read_archive(month)]}

        @r.get("/export")
        def export(a: dict = admin):
            log.record(a["uuid"], "audit.export")
            return Response(log.export_gzip(), media_type="application/gzip",
                            headers={"Content-Disposition": 'attachment; filename="audit.ndjson.gz"', "Cache-Control": "no-store"})

        @r.get("/compare")
        def compare(a: str, b: str, _: dict = admin):
            """Diff the resulting state of two entries (e.g. two revisions of the same form)."""
            return A.diff(log.snapshots(entry(a))[1], log.snapshots(entry(b))[1])

        @r.get("/{eid}/diff")
        def entry_diff(eid: str, _: dict = admin):
            d = entry(eid)
            before, after = log.snapshots(d)
            return {"entry": log.public(d), **A.diff(before, after)}

        return r
