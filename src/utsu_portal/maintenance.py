"""Scheduled audit upkeep: nightly checkpoint+anchor, archival, drop, weekly integrity check.

Run from a Kubernetes CronJob / Cloud Scheduler (`python -m utsu_portal.authsvc maintain`), or POST
/api/admin/auth-audit/maintenance. Every job is claimed with an optimistic lock, so any number of replicas or schedulers can
trigger it and each job runs once per interval."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging

from . import audit as A

log = logging.getLogger("utsu_portal.maintenance")
DAY = 86_400
SYSTEM = "system"
ARCHIVE_AFTER = 90 * DAY   # months older than ~3 months are compressed into one archive blob
DROP_AFTER = 180 * DAY     # ...and the hot table is dropped after ~6 months, once the archive is verified and anchored
INTERVALS = {"checkpoint": DAY, "archive": DAY, "verify": 7 * DAY}
QUIET = ("audit.checkpoint", "audit.archive", "audit.verify", "audit.drop", "audit.read", "db.access")


def checkpoint(ctx, force: bool = False) -> dict | None:
    """Commit everything since the last checkpoint to one Merkle root, chain it to the previous checkpoint, anchor it."""
    S, log_ = ctx.store, ctx.audit
    last = max(S.list("audit_checkpoints"), key=lambda c: c["to_seq"], default=None)
    since = last["to_seq"] if last else 0
    new = log_.entries(since)
    if not new or (not force and all(e["action"] in QUIET[:3] for e in new)):
        return None  # nothing meaningful happened: skip, saving compute and space
    root = A.merkle_root([e["hash"] for e in new])
    body = {"from_seq": new[0]["seq"], "to_seq": new[-1]["seq"], "count": len(new), "root": root, "head_hash": new[-1]["hash"],
            "prev": last["digest"] if last else "", "ts": ctx.now()}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ck = {**body, "id": f"{body['to_seq']:012d}", "digest": digest, "anchor": {"provider": ctx.anchor.name, "status": "disabled"}}
    if not S.create("audit_checkpoints", ck["id"], ck):
        return None  # another replica made this checkpoint
    anchor(ctx, ck["id"])
    ck = S.get("audit_checkpoints", ck["id"])
    log_.record(SYSTEM, "audit.checkpoint", f"seq:{ck['to_seq']}", meta={"root": root, "digest": digest, "anchor": ck["anchor"]["status"], "count": ck["count"]})
    return ck


def anchor(ctx, cid: str) -> None:
    """Publish a checkpoint digest (failures leave it 'pending' for the next run; the system never blocks on the chain)."""
    prov = ctx.anchor
    if not prov.enabled:
        return

    def mark(c, **a):
        return {**c, "anchor": {**c["anchor"], "provider": prov.name, **a}}

    ck = ctx.store.get("audit_checkpoints", cid)
    try:
        res = prov.submit(ck["digest"], {"to_seq": ck["to_seq"], "count": ck["count"]})
        ctx.store.update("audit_checkpoints", cid, lambda c: mark(c, **res, at=ctx.now()))
    except Exception:
        log.exception("anchoring failed; will retry")
        ctx.store.update("audit_checkpoints", cid, lambda c: mark(c, status="pending", at=ctx.now()))


def retry_anchors(ctx) -> int:
    n = 0
    for c in ctx.store.list("audit_checkpoints"):
        if ctx.anchor.enabled and c["anchor"]["status"] in ("pending", "disabled"):
            anchor(ctx, c["id"])
            n += 1
    return n


def archive(ctx, now: int | None = None) -> dict:
    """Compress months older than 3 months into one gzip blob (full rows incl. snapshots), then drop hot tables older than 6
    months that are verified, archived and covered by an anchored checkpoint."""
    S, now, done = ctx.store, now or ctx.now(), {"archived": [], "dropped": []}
    for m in ctx.audit.months(("hot", "archived")):
        month, end = m["month"], A.month_end(m["month"])
        if m["state"] == "hot" and now - end > ARCHIVE_AFTER:
            rows = sorted(S.list(A.coll(month)), key=lambda d: d["seq"])
            if rows and ctx.audit.verify(rows)["ok"]:
                raw = gzip.compress("\n".join(json.dumps(r, sort_keys=True) for r in rows).encode(), 9)
                S.put("audit_archive", month, {"id": month, "month": month, "first_seq": rows[0]["seq"], "last_seq": rows[-1]["seq"],
                      "first_prev": rows[0]["prev"], "last_hash": rows[-1]["hash"], "count": len(rows), "root": A.merkle_root([r["hash"] for r in rows]),
                      "sha256": hashlib.sha256(raw).hexdigest(), "blob": base64.b64encode(raw).decode(), "at": now})
                S.update("audit_months", month, lambda x: {**x, "state": "archived"})
                ctx.audit.record(SYSTEM, "audit.archive", f"month:{month}", meta={"rows": len(rows), "bytes": len(raw)})
                done["archived"].append(month)
                m["state"] = "archived"
        if m["state"] == "archived" and now - end > DROP_AFTER and _safe_to_drop(ctx, month):
            for r in S.list(A.coll(month)):
                S.delete(A.coll(month), r["id"])
            S.update("audit_months", month, lambda x: {**x, "state": "dropped"})
            ctx.audit.record(SYSTEM, "audit.drop", f"month:{month}")
            done["dropped"].append(month)
    return done


def _safe_to_drop(ctx, month: str) -> bool:
    ar = ctx.store.get("audit_archive", month)
    if not ar:
        return False
    try:
        rows = ctx.audit.read_archive(month)  # sha256 check inside
    except Exception:
        return False
    if len(rows) != ar["count"] or A.merkle_root([r["hash"] for r in rows]) != ar["root"]:
        return False
    cks = ctx.store.list("audit_checkpoints")
    covered = [c for c in cks if c["to_seq"] >= ar["last_seq"]]
    if not covered:
        return False
    first = min(covered, key=lambda c: c["to_seq"])
    return (not ctx.anchor.enabled) or first["anchor"]["status"] in ("submitted", "confirmed")


def verify_hot(ctx) -> dict:
    """Weekly: chain walk over every hot table, each checkpoint's Merkle root and chain, and archive checksums."""
    S, problems = ctx.store, []
    chain = ctx.audit.verify()
    if not chain["ok"]:
        problems.append({"chain": chain})
    prev = ""
    for c in sorted(S.list("audit_checkpoints"), key=lambda c: c["to_seq"]):
        body = {k: c[k] for k in ("from_seq", "to_seq", "count", "root", "head_hash", "prev", "ts")}
        if c["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != c["digest"]:
            problems.append({"checkpoint": c["id"], "reason": "checkpoint chain broken"})
        prev = c["digest"]
        rows = [e for e in ctx.audit.entries(c["from_seq"] - 1) if e["seq"] <= c["to_seq"]]  # partly archived/dropped ranges are covered by the archive root
        if rows and rows[0]["seq"] == c["from_seq"] and rows[-1]["seq"] == c["to_seq"] and (len(rows) != c["count"] or A.merkle_root([e["hash"] for e in rows]) != c["root"] or rows[-1]["hash"] != c["head_hash"]):
            problems.append({"checkpoint": c["id"], "reason": "entries differ from the checkpointed root"})
    for ar in S.list("audit_archive"):
        try:
            ctx.audit.read_archive(ar["month"])
        except Exception:
            problems.append({"archive": ar["month"], "reason": "checksum mismatch"})
    res = {"ok": not problems, "checked": chain.get("checked", 0), "problems": problems[:20], "at": ctx.now()}
    S.put("audit_checks", f"{ctx.now():012d}", {"id": f"{ctx.now():012d}", **res})
    ctx.audit.record(SYSTEM, "audit.verify", meta={"ok": res["ok"], "checked": res["checked"], "problems": len(problems)})
    return res


def _claim(ctx, job: str, force: bool) -> bool:
    seen = {}

    def go(j):
        j = j or {"id": job, "last": 0}
        seen["run"] = force or ctx.now() - j["last"] >= INTERVALS[job] - 300
        return {**j, "last": ctx.now()} if seen["run"] else None

    ctx.store.update("audit_jobs", job, go)
    return seen["run"]


def run_due(ctx, force: bool = False) -> dict:
    out: dict = {}
    if _claim(ctx, "checkpoint", force):
        out["checkpoint"] = (checkpoint(ctx, force) or {}).get("id")
        out["anchors_retried"] = retry_anchors(ctx)
    if _claim(ctx, "archive", force):
        out["archive"] = archive(ctx)
    if _claim(ctx, "verify", force):
        out["verify"] = verify_hot(ctx)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .authsvc import create_auth_app

    ap = argparse.ArgumentParser(description="Run due audit maintenance jobs (nightly checkpoint/archive, weekly verify).")
    ap.add_argument("--force", action="store_true", help="run every job now regardless of schedule")
    args = ap.parse_args(argv)
    res = run_due(create_auth_app().state.ctx, args.force)
    print(json.dumps(res, default=str))
    v = res.get("verify")
    return 1 if v and not v["ok"] else 0
