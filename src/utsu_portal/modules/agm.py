"""AGM registration/invitation panel, Zoom SSO wiring and the admin check-in screen."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..context import Context
from .agm_verify import has_grant, is_online, rsvp

name = "agm"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class CheckinIn(BaseModel):
    uuid: str


def qr_svg(text: str) -> str | None:
    try:
        import segno  # type: ignore
    except ImportError:
        return None
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False, nl=False)
    return buf.getvalue().decode()


class AgmModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api")
        S, cfg = ctx.store, ctx.settings
        verified, admin = ctx.require_verified(), ctx.require_admin()

        sso = ctx.services.get("sso")
        if sso and cfg.zoom_client_secret:  # Zoom may only sign on registrants who passed the separate online ID check
            def zoom_ok(acct, user):
                if not is_online(ctx, acct["uuid"]):
                    return "not registered to attend online"
                return None if has_grant(ctx, acct["uuid"]) else "online ID verification required"
            sso.register_client(cfg.zoom_client_id, cfg.zoom_client_secret, cfg.zoom_redirect_uris, zoom_ok)

        @r.get("/agm/invitation")
        def invitation(a: dict = verified):
            s = rsvp(ctx, a["uuid"])
            if not s:
                return {"registered": False, "form": "agm_rsvp"}
            online = s["data"].get("attendance") == "Online"
            out = {"registered": True, "mode": "online" if online else "in_person", "rsvp_status": s["status"],
                   "checked_in": S.get("agm_attendance", a["uuid"]) is not None}
            if online:
                g = has_grant(ctx, a["uuid"])
                out.update(id_verified=g, zoom_sso_url=cfg.zoom_sso_url if g else "")
            return out

        @r.get("/agm/qr")
        def qr(a: dict = verified):
            if not rsvp(ctx, a["uuid"]):
                raise HTTPException(409, "register for the AGM first")
            return {"payload": a["uuid"], "svg": qr_svg(a["uuid"])}

        @r.post("/agm/zoom/join")
        def zoom_join(a: dict = verified):
            if not is_online(ctx, a["uuid"]):
                raise HTTPException(409, "you are not registered to attend online")
            if not has_grant(ctx, a["uuid"]):
                raise HTTPException(403, "complete online ID verification first")
            if not cfg.zoom_url:
                raise HTTPException(503, "Zoom link not configured")
            if S.create("agm_attendance", a["uuid"], {"uuid": a["uuid"], "mode": "online", "at": ctx.now(), "by": a["uuid"]}):
                ctx.audit.record(a["uuid"], "agm.online_join", f"user:{a['uuid']}", None, {"mode": "online"})
            return {"zoom_url": cfg.zoom_url, "sso_url": cfg.zoom_sso_url}

        @r.post("/admin/agm/checkin")
        def checkin(b: CheckinIn, a: dict = admin):
            uid = b.uuid.strip().lower()
            u = UUID.match(uid) and ctx.identity.user(uid)
            acct = u and ctx.identity.account(uid)
            s = u and rsvp(ctx, uid)
            if not u or not acct or acct["status"] != "verified":
                raise HTTPException(404, "unknown or unverified attendee")
            if not s:
                raise HTTPException(409, f"{u['preferred_name']} {u['last_name']} has no AGM registration")
            seen = {}

            def arrive(rec):  # optimistic lock: two scanners reading the same code cannot both record the arrival
                seen["first"] = not rec or rec["mode"] != "in_person"  # an earlier online join does not count as arrival
                return {"uuid": uid, "mode": "in_person", "at": ctx.now(), "by": a["uuid"]} if seen["first"] else None

            rec, first = S.update("agm_attendance", uid, arrive), seen["first"]
            if first:
                ctx.audit.record(a["uuid"], "agm.checkin", f"user:{uid}", None, {"mode": "in_person"})
            d = s["data"]
            return {"uuid": uid, "name": f"{u['preferred_name']} {u['last_name']}".strip(),
                    "legal_name": f"{u['first_name']} {u['last_name']}", "pronouns": d.get("pronouns", ""),
                    "proxy": bool(d.get("proxy")), "already_checked_in": not first, "checked_in_at": rec["at"],
                    "message": (f"Checked in: {u['preferred_name']} {u['last_name']}" if first
                                else f"Already checked in: {u['preferred_name']} {u['last_name']}")}

        @r.get("/admin/agm/attendance")
        def attendance(_: dict = admin):
            return S.list("agm_attendance")

        return r
