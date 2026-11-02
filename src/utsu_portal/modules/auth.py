from __future__ import annotations

import base64
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import security
from ..context import Context
from ..security import clean_text

name = "auth"
USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
EMAIL = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[^@\s<>]{2,}$")


class Name(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, v):
        return clean_text(v, 100) if isinstance(v, str) else v


class Register(Name):
    username: str
    email: str
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    preferred_name: str = ""
    student_number: str = Field(pattern=r"^\d{7,10}$")


class Username(BaseModel):
    username: str


class Recover(Username):
    student_number_trunc: str = Field(pattern=r"^\d{4}$")


class CodeIn(Username):
    code: str


class Preferred(Name):
    preferred_name: str = Field(min_length=1)


class Verification(Name):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    division_codes: list[str] = Field(min_length=1, max_length=5)
    face: str
    id_card: str
    challenge: str


class RecoveryChange(Verification):
    username: str
    student_number_trunc: str = Field(pattern=r"^\d{4}$")
    new_email: str | None = None
    otp: str | None = None


class RecoveryOtp(Recover):
    new_email: str


def _image(ctx: Context, data_url: str) -> str:
    m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", data_url or "")
    if not m:
        raise HTTPException(422, "image must be a base64 jpeg/png/webp data URL")
    if len(base64.b64decode(m.group(2), validate=True)) > ctx.settings.max_image_bytes:
        raise HTTPException(413, "image too large")
    return data_url


def _divisions(ctx: Context, codes: list[str]) -> list[str]:
    codes = [clean_text(c, 10).upper() for c in codes]
    bad = [c for c in codes if c not in ctx.settings.division_codes]
    if bad:
        raise HTTPException(422, f"unknown division code(s): {bad}")
    return codes


def _submit_verification(ctx: Context, uid: str, kind: str, body: Verification, changes: dict) -> dict:
    if security.read_token(ctx.settings.secret, body.challenge, "chal") is None:
        raise HTTPException(422, "live-scan challenge missing or expired; capture again")
    face, idc = _image(ctx, body.face), _image(ctx, body.id_card)
    divs = _divisions(ctx, body.division_codes)
    for v in ctx.store.list("verifications", uuid=uid, status="pending"):
        raise HTTPException(409, "a verification request is already pending")
    vid = security.secrets.token_hex(12)
    rec = {"id": vid, "uuid": uid, "kind": kind, "status": "pending", "submitted_at": ctx.now(),
           "division_codes": divs, "first_name": body.first_name, "last_name": body.last_name, "changes": changes}
    ctx.store.put("media", vid, {"face": face, "id_card": idc})
    ctx.store.put("verifications", vid, rec)
    return {"id": vid, "status": "pending"}


class AuthModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api")
        S = ctx.store
        user_dep = ctx.require_user()

        @r.post("/auth/register", status_code=201)
        def register(b: Register):
            if not USERNAME.match(b.username):
                raise HTTPException(422, "username must be 3-64 chars of letters, digits . _ -")
            if not EMAIL.match(b.email):
                raise HTTPException(422, "invalid email")
            uid = ctx.uid(b.username)
            sid = security.student_id_uuid(ctx.settings.secret, b.student_number)
            ekey = ctx.email_key(b.email)
            if S.get("accounts", uid):
                raise HTTPException(409, "username already registered")
            if not S.create("student_ids", sid, {"uuid": uid}):
                raise HTTPException(409, "student number already registered")
            if not S.create("emails", ekey, {"uuid": uid}):
                S.delete("student_ids", sid)
                raise HTTPException(409, "email already registered")
            admin = clean_text(b.username).lower() in {a.lower() for a in ctx.settings.admins}
            S.put("users", uid, {"uuid": uid, "email": b.email.lower(), "preferred_name": b.preferred_name or b.first_name,
                                 "first_name": b.first_name, "last_name": b.last_name,
                                 "student_number": security.truncate_student_number(b.student_number)})
            S.put("accounts", uid, {"uuid": uid, "status": "pending_email", "role": "admin" if admin else "applicant",
                                    "email_verified": False, "sid_uuid": sid, "email_key": ekey})
            ctx.send_otp("email", uid, b.email)
            ctx.audit.record(uid, "account.registered", f"user:{uid}", None, S.get("users", uid), {"role": "admin" if admin else "applicant"})
            return {"uuid": uid, "status": "pending_email"}

        @r.post("/auth/verify-email")
        def verify_email(b: CodeIn):
            uid = ctx.uid(b.username)
            acct = S.get("accounts", uid)
            if acct is None or ctx.check_otp("email", uid, b.code) is None:
                raise HTTPException(400, "invalid or expired code")
            acct["email_verified"] = True
            if acct["status"] == "pending_email":
                acct["status"] = "verified" if acct["role"] == "admin" else "pending_review"
            S.put("accounts", uid, acct)
            ctx.audit.record(uid, "account.email_verified", f"user:{uid}", None, {"status": acct["status"]})
            return {"status": acct["status"]}

        @r.post("/auth/login")
        def login(b: Username):
            uid = ctx.uid(b.username)
            user, acct = S.get("users", uid), S.get("accounts", uid)
            if user and acct:  # identical response either way: no account enumeration
                ctx.send_otp("login", uid, user["email"])
            return {"detail": "if the account exists, a code was emailed"}

        @r.post("/auth/token")
        def token(b: CodeIn):
            uid = ctx.uid(b.username)
            acct = S.get("accounts", uid)
            if acct is None or ctx.check_otp("login", uid, b.code) is None:
                raise HTTPException(400, "invalid or expired code")
            if not acct["email_verified"]:
                acct["email_verified"] = True  # the login code proves control of the registered email
                if acct["status"] == "pending_email":
                    acct["status"] = "verified" if acct["role"] == "admin" else "pending_review"
                S.put("accounts", uid, acct)
            ctx.audit.record(uid, "auth.login", f"user:{uid}")
            return {"token": ctx.identity.issue(uid), "status": acct["status"]}

        @r.get("/me")
        def me(acct: dict = user_dep):
            u = S.get("users", acct["uuid"])
            return {"user": u, "status": acct["status"], "role": acct["role"]}

        @r.patch("/me")
        def update_me(b: Preferred, acct: dict = user_dep):
            u = S.get("users", acct["uuid"])
            old = u["preferred_name"]
            u["preferred_name"] = b.preferred_name  # the only user-mutable field
            S.put("users", acct["uuid"], u)
            ctx.audit.record(acct["uuid"], "user.preferred_name_changed", f"user:{acct['uuid']}", {"preferred_name": old}, {"preferred_name": u["preferred_name"]})
            return {"user": u}

        @r.get("/verification/challenge")
        def challenge():
            nonce = security.secrets.token_hex(3)
            return {"challenge": security.sign_token(ctx.settings.secret, nonce, 600, "chal"), "phrase": nonce.upper()}

        @r.post("/verification", status_code=201)
        def verification(b: Verification, acct: dict = user_dep):
            if acct["status"] == "verified":
                raise HTTPException(409, "already verified")
            if acct["status"] == "rejected":
                raise HTTPException(403, "verification was rejected; contact an administrator")
            u = S.get("users", acct["uuid"])
            if (b.first_name, b.last_name) != (u["first_name"], u["last_name"]):
                raise HTTPException(422, "names must match the registered names; use account recovery to change them")
            return _submit_verification(ctx, acct["uuid"], "activation", b, {})

        @r.get("/verification")
        def verification_status(acct: dict = user_dep):
            return [{k: v[k] for k in ("id", "kind", "status", "submitted_at")} | {"note": v.get("note", "")}
                    for v in S.list("verifications", uuid=acct["uuid"])]

        # ---- account recovery -------------------------------------------------
        def _recoverable(b: Recover) -> tuple[dict, dict]:
            uid = ctx.uid(b.username)
            u, a = S.get("users", uid), S.get("accounts", uid)
            if not u or not a or u["student_number"] != b.student_number_trunc:
                raise HTTPException(400, "details do not match an account")
            return u, a

        @r.post("/recovery/start")
        def recovery_start(b: Recover):
            uid = ctx.uid(b.username)
            u = S.get("users", uid)
            if u and u["student_number"] == b.student_number_trunc:
                ctx.send_otp("login", uid, u["email"])
            return {"detail": "if the details match, a code was emailed; exchange it at /api/auth/token"}

        @r.post("/recovery/email-otp")
        def recovery_email_otp(b: RecoveryOtp):
            u, a = _recoverable(b)
            if not EMAIL.match(b.new_email) or S.get("emails", ctx.email_key(b.new_email)):
                raise HTTPException(422, "invalid or unavailable email")
            ctx.send_otp("newemail", a["uuid"], b.new_email, {"email": b.new_email.lower()})
            return {"detail": "code sent to the new address"}

        @r.post("/recovery/request", status_code=201)
        def recovery_request(b: RecoveryChange):
            u, a = _recoverable(b)
            changes: dict = {}
            if (b.first_name, b.last_name) != (u["first_name"], u["last_name"]):
                changes["first_name"], changes["last_name"] = b.first_name, b.last_name
            if b.new_email:
                p = ctx.check_otp("newemail", a["uuid"], b.otp or "")
                if not p or p["email"] != b.new_email.lower():
                    raise HTTPException(400, "new email not verified")
                changes["email"] = p["email"]
            if not changes:
                raise HTTPException(422, "no change requested; use /api/recovery/start to regain access")
            return _submit_verification(ctx, a["uuid"], "recovery", b, changes)

        return r
