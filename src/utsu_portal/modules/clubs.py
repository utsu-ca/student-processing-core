"""Clubs: registration, multi-admin approval, ledger, receipts and tax (HST) reporting.

Money is always integer cents. Rates are basis points (1300 = 13%). Expenses only count in the ledger once approved.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections import defaultdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..context import Context
from ..security import clean_text

name = "clubs"
ROLES = ("owner", "treasurer", "executive")
FINANCE_ROLES = ("owner", "treasurer")
RECEIPT_TYPES = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}


def split_tax(amount: int, bps: int, inclusive: bool) -> tuple[int, int, int]:
    """Return (net, tax, gross) in cents, rounding half up. `amount` is gross when inclusive, else net."""
    if inclusive:
        tax = (amount * bps * 2 + (10_000 + bps)) // (2 * (10_000 + bps))
        return amount - tax, tax, amount
    tax = (amount * bps * 2 + 10_000) // 20_000
    return amount, tax, amount + tax


def record_decision(doc: dict, admin_uuid: str, approve: bool, note: str, required: int, now: int) -> str:
    """Generic approval mechanism: N distinct approvers, any rejection is final. Returns the resulting state."""
    if any(a["by"] == admin_uuid for a in doc["approvals"]):
        raise HTTPException(409, "you have already decided on this")
    doc["approvals"].append({"by": admin_uuid, "approve": approve, "note": clean_text(note, 500), "at": now})
    if not approve:
        return "rejected"
    return "approved" if sum(a["approve"] for a in doc["approvals"]) >= required else "pending"


class ClubIn(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=2000)
    category: str = Field(min_length=2, max_length=50)

    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, v):
        return clean_text(v, 2000, multiline=True) if isinstance(v, str) else v


class MemberIn(BaseModel):
    username: str
    role: str


class Decision(BaseModel):
    approve: bool
    note: str = ""


class TxIn(BaseModel):
    kind: str  # expense | income
    amount_cents: int = Field(gt=0, le=100_000_000)
    tax_inclusive: bool = True
    tax_bps: int | None = Field(None, ge=0, le=3000)  # 0 = exempt / zero-rated
    category: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=3, max_length=500)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    receipt_ids: list[str] = Field(default_factory=list, max_length=10)


class GrantIn(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    note: str = Field("", max_length=500)


class ReceiptIn(BaseModel):
    data_url: str


class ClubsModule:
    name = name

    def router(self, ctx: Context) -> APIRouter:
        r = APIRouter(prefix="/api")
        S, cfg = ctx.store, ctx.settings
        verified, admin = ctx.require_verified(), ctx.require_admin()

        def club(cid: str) -> dict:
            c = S.get("clubs", cid)
            if not c:
                raise HTTPException(404, "unknown club")
            return c

        def member(c: dict, uid: str, roles=ROLES) -> None:
            if c["members"].get(uid) not in roles:
                raise HTTPException(403, "not permitted for this club")

        def conflict(c: dict, admin_uuid: str) -> None:
            if admin_uuid in c["members"]:
                raise HTTPException(403, "admins who belong to a club cannot approve its items")

        def balance(cid: str) -> int:
            return sum((t["gross_cents"] if t["kind"] != "expense" else -t["gross_cents"])
                       for t in S.list("transactions", club_id=cid) if t["status"] == "approved")

        # ---------------- registration ----------------
        @r.post("/clubs", status_code=201)
        def register(b: ClubIn, a: dict = verified):
            key = re.sub(r"\W+", " ", b.name).lower().strip()
            if any(re.sub(r"\W+", " ", c["name"]).lower().strip() == key and c["status"] != "rejected" for c in S.list("clubs")):
                raise HTTPException(409, "a club with that name already exists")
            cid = secrets.token_hex(8)
            S.put("clubs", cid, {"id": cid, "name": b.name, "description": b.description, "category": b.category, "status": "pending",
                                 "members": {a["uuid"]: "owner"}, "approvals": [], "created_at": ctx.now()})
            ctx.audit.record(a["uuid"], "club.registered", f"club:{cid}", None, S.get("clubs", cid))
            return {"id": cid, "status": "pending"}

        @r.get("/clubs/mine")
        def mine(a: dict = verified):
            return [c for c in S.list("clubs") if a["uuid"] in c["members"]]

        @r.get("/clubs/{cid}")
        def get_club(cid: str, a: dict = ctx.require_user()):
            c = club(cid)
            if a["uuid"] not in c["members"] and a.get("role") != "admin":
                raise HTTPException(403, "not permitted for this club")
            return {**c, "balance_cents": balance(cid)}

        @r.post("/clubs/{cid}/members")
        def add_member(cid: str, b: MemberIn, a: dict = verified):
            c = club(cid)
            member(c, a["uuid"], ("owner",))
            if b.role not in ROLES[1:]:
                raise HTTPException(422, f"role must be one of {ROLES[1:]}")
            found = ctx.identity.resolve(b.username)
            uid, acct = (found["uuid"], found["account"]) if found else (None, None)
            if not acct or acct["status"] != "verified":
                raise HTTPException(422, "member must be a verified account")
            old_role = c["members"].get(uid)
            def setrole(cur):
                cur["members"][uid] = b.role
                return cur

            c = S.update("clubs", cid, setrole)
            ctx.audit.record(a["uuid"], "club.member_set", f"club:{cid}", {"role": old_role}, {"role": b.role}, {"member": uid})
            return {"members": c["members"]}

        @r.get("/admin/clubs")
        def admin_clubs(status: str | None = None, _: dict = admin):
            return S.list("clubs", **({"status": status} if status else {}))

        @r.post("/admin/clubs/{cid}/decision")
        def decide_club(cid: str, b: Decision, a: dict = admin):
            c = club(cid)
            if c["status"] != "pending":
                raise HTTPException(409, f"club is already {c['status']}")
            conflict(c, a["uuid"])
            def vote(cur):  # re-evaluated on a fresh copy if another admin voted meanwhile, so no vote is lost
                if cur["status"] != "pending":
                    raise HTTPException(409, f"club is already {cur['status']}")
                conflict(cur, a["uuid"])
                cur["status"] = record_decision(cur, a["uuid"], b.approve, b.note, cfg.club_registration_approvals, ctx.now())
                return cur

            c = S.update("clubs", cid, vote)
            ctx.audit.record(a["uuid"], "club.decision", f"club:{cid}", {"status": "pending"}, {"status": c["status"]}, {"approve": b.approve, "note": b.note[:200]})
            return {"status": c["status"], "approvals": len(c["approvals"])}

        # ---------------- receipts ----------------
        @r.post("/clubs/{cid}/receipts", status_code=201)
        def upload_receipt(cid: str, b: ReceiptIn, a: dict = verified):
            c = club(cid)
            member(c, a["uuid"], FINANCE_ROLES)
            m = re.fullmatch(r"data:(?:image/(jpeg|png|webp)|application/(pdf));base64,([A-Za-z0-9+/=]+)", b.data_url)
            if not m:
                raise HTTPException(422, "receipt must be a base64 jpeg/png/webp/pdf data URL")
            raw = base64.b64decode(m.group(3), validate=True)
            if len(raw) > cfg.max_receipt_bytes:
                raise HTTPException(413, "receipt too large")
            digest = hashlib.sha256(raw).hexdigest()
            for old in S.list("receipts", club_id=cid, sha256=digest):
                raise HTTPException(409, {"error": "duplicate receipt", "id": old["id"]})
            rid = secrets.token_hex(8)
            S.put("receipts", rid, {"id": rid, "club_id": cid, "sha256": digest, "data_url": b.data_url, "by": a["uuid"],
                                    "at": ctx.now(), "transaction_id": None})
            ctx.audit.record(a["uuid"], "receipt.uploaded", f"club:{cid}", None, None, {"receipt": rid, "sha256": digest})
            return {"id": rid, "sha256": digest}

        @r.get("/clubs/{cid}/receipts/{rid}")
        def get_receipt(cid: str, rid: str, a: dict = ctx.require_user()):
            c, rc = club(cid), S.get("receipts", rid)
            if a["uuid"] not in c["members"] and a.get("role") != "admin":
                raise HTTPException(403, "not permitted for this club")
            if not rc or rc["club_id"] != cid:
                raise HTTPException(404, "unknown receipt")
            return rc

        # ---------------- ledger ----------------
        @r.post("/clubs/{cid}/transactions", status_code=201)
        def add_tx(cid: str, b: TxIn, a: dict = verified):
            c = club(cid)
            member(c, a["uuid"], FINANCE_ROLES)
            if c["status"] != "approved":
                raise HTTPException(409, "club is not approved")
            if b.kind not in ("expense", "income"):
                raise HTTPException(422, "kind must be expense or income")
            bps = cfg.default_tax_bps if b.tax_bps is None else b.tax_bps
            net, tax, gross = split_tax(b.amount_cents, bps, b.tax_inclusive)
            rcpts = []
            for rid in dict.fromkeys(b.receipt_ids):
                rc = S.get("receipts", rid)
                if not rc or rc["club_id"] != cid or rc["transaction_id"]:
                    raise HTTPException(422, f"receipt {rid} is unknown or already used")
                rcpts.append(rc)
            if not rcpts:
                raise HTTPException(422, "at least one receipt is required")
            tid = secrets.token_hex(8)
            doc = {"id": tid, "club_id": cid, "kind": b.kind, "net_cents": net, "tax_cents": tax, "gross_cents": gross,
                   "tax_bps": bps, "category": clean_text(b.category, 50), "description": clean_text(b.description, 500),
                   "date": b.date, "receipt_ids": [x["id"] for x in rcpts], "created_by": a["uuid"], "created_at": ctx.now(),
                   "approvals": [], "status": "pending" if b.kind == "expense" else "approved",
                   "required_approvals": (cfg.expense_approvals_large if gross >= cfg.expense_large_cents else cfg.expense_approvals_small)
                   if b.kind == "expense" else 0}
            claimed = []

            def attach(rc):  # a receipt can back one transaction only: the optimistic claim decides races
                if not rc or rc["transaction_id"]:
                    raise HTTPException(422, f"receipt {rc and rc['id']} is unknown or already used")
                return {**rc, "transaction_id": tid}

            try:
                for rc in rcpts:
                    S.update("receipts", rc["id"], attach)
                    claimed.append(rc["id"])
            except HTTPException:
                for rid in claimed:
                    S.update("receipts", rid, lambda r: {**r, "transaction_id": None})
                raise
            S.put("transactions", tid, doc)
            ctx.audit.record(a["uuid"], "transaction.created", f"club:{cid}", None, doc, {"transaction": tid})
            return {k: doc[k] for k in ("id", "status", "net_cents", "tax_cents", "gross_cents", "required_approvals")}

        @r.get("/clubs/{cid}/transactions")
        def list_tx(cid: str, a: dict = ctx.require_user()):
            c = club(cid)
            if a["uuid"] not in c["members"] and a.get("role") != "admin":
                raise HTTPException(403, "not permitted for this club")
            return sorted(S.list("transactions", club_id=cid), key=lambda t: (t["date"], t["created_at"]))

        @r.get("/admin/transactions")
        def pending_tx(status: str = "pending", _: dict = admin):
            return S.list("transactions", status=status)

        @r.post("/admin/transactions/{tid}/decision")
        def decide_tx(tid: str, b: Decision, a: dict = admin):
            def vote(t):
                if not t or t["status"] != "pending":
                    raise HTTPException(404, "no pending transaction")
                conflict(club(t["club_id"]), a["uuid"])
                state = record_decision(t, a["uuid"], b.approve, b.note, t["required_approvals"], ctx.now())
                if state == "approved" and balance(t["club_id"]) < t["gross_cents"]:
                    raise HTTPException(409, "insufficient club balance; add a grant or income first")
                t["status"] = state
                return t

            t = S.update("transactions", tid, vote)
            state = t["status"]
            ctx.audit.record(a["uuid"], "transaction.decision", f"club:{t['club_id']}", {"status": "pending"}, {"status": state},
                             {"transaction": tid, "approve": b.approve, "note": b.note[:200]})
            return {"status": state, "approvals": sum(x["approve"] for x in t["approvals"]), "required": t["required_approvals"]}

        @r.post("/admin/clubs/{cid}/grants", status_code=201)
        def grant(cid: str, b: GrantIn, a: dict = admin):
            c = club(cid)
            if c["status"] != "approved":
                raise HTTPException(409, "club is not approved")
            conflict(c, a["uuid"])
            tid = secrets.token_hex(8)
            S.put("transactions", tid, {"id": tid, "club_id": cid, "kind": "grant", "net_cents": b.amount_cents, "tax_cents": 0,
                                        "gross_cents": b.amount_cents, "tax_bps": 0, "category": "grant", "description": clean_text(b.note, 500) or "Grant",
                                        "date": "", "receipt_ids": [], "created_by": a["uuid"], "created_at": ctx.now(),
                                        "approvals": [], "status": "approved", "required_approvals": 0})
            ctx.audit.record(a["uuid"], "grant.issued", f"club:{cid}", None, {"amount_cents": b.amount_cents}, {"transaction": tid})
            return {"id": tid}

        @r.get("/clubs/{cid}/summary")
        def summary(cid: str, a: dict = ctx.require_user()):
            """Tax summary: tax paid on approved expenses (input tax credit eligible) and tax collected on income."""
            c = club(cid)
            if a["uuid"] not in c["members"] and a.get("role") != "admin":
                raise HTTPException(403, "not permitted for this club")
            totals = defaultdict(int)
            by_cat: dict[str, int] = defaultdict(int)
            for t in S.list("transactions", club_id=cid):
                if t["status"] != "approved":
                    continue
                k = "expense" if t["kind"] == "expense" else "income"
                totals[f"{k}_gross"] += t["gross_cents"]
                totals[f"{k}_net"] += t["net_cents"]
                totals[f"{k}_tax"] += t["tax_cents"]
                if k == "expense":
                    by_cat[t["category"]] += t["gross_cents"]
            return {"income_cents": totals["income_gross"], "expense_cents": totals["expense_gross"],
                    "balance_cents": totals["income_gross"] - totals["expense_gross"],
                    "tax_paid_cents": totals["expense_tax"], "tax_collected_cents": totals["income_tax"],
                    "net_tax_position_cents": totals["income_tax"] - totals["expense_tax"],
                    "expenses_by_category": dict(by_cat),
                    "pending_expenses": sum(1 for t in S.list("transactions", club_id=cid, status="pending"))}

        return r
