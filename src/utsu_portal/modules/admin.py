"""Admin review of account activation / recovery requests (face + id scans)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..context import Context

name = "admin"


class Decision(BaseModel):
    approve: bool
    note: str = ""


class AdminModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api/admin")
        S = ctx.store
        admin = ctx.require_admin()

        @r.get("/verifications")
        def pending(_: dict = admin):
            return [v for v in S.list("verifications", status="pending")]

        @r.get("/verifications/{vid}/media")
        def media(vid: str, _: dict = admin):
            m = S.get("media", vid)
            if not m:
                raise HTTPException(404, "media gone (decision already made)")
            return m

        @r.post("/verifications/{vid}/decision")
        def decide(vid: str, b: Decision, a: dict = admin):
            v = S.get("verifications", vid)
            if not v or v["status"] != "pending":
                raise HTTPException(404, "no pending request")
            if v["uuid"] == a["uuid"]:
                raise HTTPException(403, "cannot review your own request")
            acct, user = S.get("accounts", v["uuid"]), S.get("users", v["uuid"])
            snap = lambda: {"status": acct["status"], **{k: user.get(k) for k in ("email", "first_name", "last_name")}}
            before = snap()
            if b.approve:
                ch = v["changes"]
                if "email" in ch:
                    S.create("emails", ctx.email_key(ch["email"]), {"uuid": v["uuid"]}) or _conflict()
                    S.delete("emails", acct["email_key"])
                    acct["email_key"] = ctx.email_key(ch["email"])
                    user["email"] = ch["email"]
                if "first_name" in ch:
                    user["first_name"], user["last_name"] = ch["first_name"], ch["last_name"]
                acct["status"] = "verified"
                S.put("users", v["uuid"], user)
            elif v["kind"] == "activation":
                acct["status"] = "rejected"
            S.put("accounts", v["uuid"], acct)
            v.update(status="approved" if b.approve else "rejected", note=b.note[:500], decided_by=a["uuid"], decided_at=ctx.now())
            S.put("verifications", vid, v)
            ctx.audit.record(a["uuid"], "verification.decided", f"user:{v['uuid']}", before, snap(),
                             {"verification": vid, "kind": v["kind"], "approved": b.approve, "note": b.note[:200]})
            S.delete("media", vid)  # face / id images are never retained after the decision
            return {"status": v["status"]}

        def _conflict():
            raise HTTPException(409, "email now in use by another account")

        return r
