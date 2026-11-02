"""Separate ID verifier for AGM registrants who join online on the day.

It is deliberately independent of the account-activation review: it only answers "has this registrant freshly proven who
they are for the AGM?" and records a short-lived grant that the AGM module / SSO eligibility check consults.
"""
from __future__ import annotations

import base64
import re
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import security
from ..context import Context

name = "agm_verify"


def rsvp(ctx: Context, uid: str) -> dict | None:
    s = ctx.store.get("submissions", f"agm_rsvp:{uid}")
    return s if s and s["status"] != "rejected" else None


def is_online(ctx: Context, uid: str) -> bool:
    s = rsvp(ctx, uid)
    return bool(s and s["data"].get("attendance") == "Online")


def has_grant(ctx: Context, uid: str) -> bool:
    g = ctx.store.get("agm_grants", uid)
    return bool(g and g["exp"] > ctx.now())


class ScanIn(BaseModel):
    face: str
    id_card: str
    challenge: str


class Decision(BaseModel):
    approve: bool
    note: str = ""


class AgmVerifyModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api")
        S, cfg = ctx.store, ctx.settings
        verified, admin = ctx.require_verified(), ctx.require_admin()

        def grant(uid: str) -> None:
            S.put("agm_grants", uid, {"uuid": uid, "exp": ctx.now() + cfg.agm_grant_ttl, "at": ctx.now()})

        def img(d: str) -> str:
            m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", d or "")
            if not m:
                raise HTTPException(422, "image must be a base64 jpeg/png/webp data URL")
            if len(base64.b64decode(m.group(2), validate=True)) > cfg.max_image_bytes:
                raise HTTPException(413, "image too large")
            return d

        @r.get("/agm/verify/challenge")
        def challenge(_: dict = verified):
            nonce = secrets.token_hex(3)
            return {"challenge": security.sign_token(cfg.secret, nonce, 600, "chal"), "phrase": nonce.upper()}

        @r.post("/agm/verify", status_code=201)
        def submit(b: ScanIn, a: dict = verified):
            uid = a["uuid"]
            if not is_online(ctx, uid):
                raise HTTPException(409, "you are not registered to attend online")
            if has_grant(ctx, uid):
                return {"status": "approved"}
            if security.read_token(cfg.secret, b.challenge, "chal") is None:
                raise HTTPException(422, "live-scan challenge missing or expired; capture again")
            face, idc = img(b.face), img(b.id_card)
            if any(S.list("agm_checks", uuid=uid, status="pending")):
                raise HTTPException(409, "a check is already pending")
            if cfg.agm_verify_mode == "auto":  # images are validated but never kept
                grant(uid)
                return {"status": "approved"}
            cid = secrets.token_hex(12)
            S.put("agm_media", cid, {"face": face, "id_card": idc})
            S.put("agm_checks", cid, {"id": cid, "uuid": uid, "status": "pending", "submitted_at": ctx.now()})
            return {"id": cid, "status": "pending"}

        @r.get("/agm/verify")
        def status(a: dict = verified):
            if has_grant(ctx, a["uuid"]):
                return {"status": "approved"}
            checks = sorted(S.list("agm_checks", uuid=a["uuid"]), key=lambda c: c["submitted_at"])
            return {"status": checks[-1]["status"] if checks else "none", "note": checks[-1].get("note", "") if checks else ""}

        @r.get("/admin/agm/verifications")
        def pending(_: dict = admin):
            out = []
            for c in S.list("agm_checks", status="pending"):
                u = ctx.identity.user(c["uuid"]) or {}
                out.append({**c, "first_name": u.get("first_name"), "last_name": u.get("last_name"), "student_number": u.get("student_number")})
            return out

        @r.get("/admin/agm/verifications/{cid}/media")
        def media(cid: str, _: dict = admin):
            m = S.get("agm_media", cid)
            if not m:
                raise HTTPException(404, "media gone")
            return m

        @r.post("/admin/agm/verifications/{cid}/decision")
        def decide(cid: str, b: Decision, a: dict = admin):
            def claim(c):  # optimistic lock: a check can be decided once
                if not c or c["status"] != "pending":
                    raise HTTPException(404, "no pending check")
                if c["uuid"] == a["uuid"]:
                    raise HTTPException(403, "cannot review your own check")
                return {**c, "status": "approved" if b.approve else "rejected", "note": b.note[:500], "decided_by": a["uuid"], "decided_at": ctx.now()}

            c = S.update("agm_checks", cid, claim)
            S.delete("agm_media", cid)
            ctx.audit.record(a["uuid"], "agm.verification_decided", f"user:{c['uuid']}", {"status": "pending"}, {"status": c["status"]},
                             {"check": cid, "note": b.note[:200]})
            if b.approve:
                grant(c["uuid"])
            return {"status": c["status"]}

        return r
