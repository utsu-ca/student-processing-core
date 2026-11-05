import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_portal import activate, setup_admin_and_user  # noqa: E402

from utsu_portal import audit as A, create_app, maintenance as M
from utsu_portal.anchor import AnchorProvider, LocalLedgerProvider
from utsu_portal.mailer import OutboxMailer
from utsu_portal.settings import Settings
from utsu_portal.store import MemoryStore

DAY = 86_400


def boot(**kw):
    mail, store = OutboxMailer(), MemoryStore()
    app = create_app(Settings(secret="s" * 40, admins=("boss",), **kw), store, mail)
    return TestClient(app), app.state.ctx, mail, store


def fill(ctx, start, n, step=DAY):
    t = [start]
    ctx.now = lambda: t[0]
    for i in range(n):
        ctx.audit.record("a", "x.change", f"t{i}", {"v": i}, {"v": i + 1})
        t[0] += step
    return t


def test_months_split_and_chain_spans_tables():
    _, ctx, _, store = boot()
    jan = int(time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, 0)))
    fill(ctx, jan, 40)
    ms = [m["month"] for m in ctx.audit.months(("hot",))]
    assert len(ms) >= 2 and all(store.list(A.coll(m)) for m in ms)
    assert ctx.audit.verify()["ok"] and ctx.audit.verify()["checked"] == 40


def test_checkpoint_chain_and_tamper_detection():
    _, ctx, _, store = boot(anchor="local")
    t = fill(ctx, int(time.time()) - 5 * DAY, 3, 60)
    c1 = M.checkpoint(ctx)
    assert c1 and c1["count"] >= 3 and c1["anchor"]["status"] in ("submitted", "confirmed")
    assert M.checkpoint(ctx) is None  # only quiet system entries since: skipped
    fill(ctx, t[0], 2, 60)
    c2 = M.checkpoint(ctx)
    assert c2["prev"] == c1["digest"] and M.verify_hot(ctx)["ok"]
    e = ctx.audit.entries()[1]
    raw = store.get(A.coll(A.month_of(e["ts"])), e["id"])
    store.put(A.coll(A.month_of(e["ts"])), e["id"], {**raw, "actor": "evil"})
    assert M.verify_hot(ctx)["ok"] is False


def test_failing_provider_stays_pending_and_is_retried():
    class Flaky(AnchorProvider):
        name, enabled, fail = "flaky", True, True

        def submit(self, digest, meta):
            if self.fail:
                raise RuntimeError("chain down")
            return {"status": "submitted", "ref": "tx1"}

    _, ctx, _, _ = boot()
    ctx.anchor = p = Flaky()
    fill(ctx, int(time.time()) - DAY, 2, 60)
    ck = M.checkpoint(ctx)
    assert ck["anchor"]["status"] == "pending"
    p.fail = False
    assert M.retry_anchors(ctx) == 1
    assert ctx.store.get("audit_checkpoints", ck["id"])["anchor"]["status"] == "submitted"


def test_archive_then_drop_keeps_verification_and_reads():
    _, ctx, _, store = boot()
    ctx.anchor = LocalLedgerProvider(store)
    old = int(time.mktime((2026, 1, 10, 0, 0, 0, 0, 0, 0)))
    fill(ctx, old, 3, DAY)
    t = fill(ctx, old + 200 * DAY, 3, DAY)
    first = ctx.audit.entries()[0]
    M.checkpoint(ctx, force=True)
    month = A.month_of(first["ts"])
    res = M.archive(ctx, now=t[0])
    assert month in res["archived"] and month in res["dropped"]
    assert store.list(A.coll(month)) == []  # hot table gone, archive remains
    assert ctx.audit.get(first["seq"])["actor"] == "a"
    assert ctx.audit.verify()["ok"] and M.verify_hot(ctx)["ok"]
    ar = store.get("audit_archive", month)
    store.put("audit_archive", month, {**ar, "sha256": "0" * 64})
    assert M.verify_hot(ctx)["ok"] is False


def test_nothing_dropped_without_anchored_checkpoint():
    _, ctx, _, store = boot()
    ctx.anchor = LocalLedgerProvider(store)
    old = int(time.mktime((2026, 1, 10, 0, 0, 0, 0, 0, 0)))
    fill(ctx, old, 2, DAY)
    t = fill(ctx, old + 200 * DAY, 1)
    res = M.archive(ctx, now=t[0])
    assert res["archived"] and not res["dropped"]


def test_run_due_claims_once_and_admin_endpoints():
    c, ctx, mail, _ = boot(anchor="local")
    ah, uh = setup_admin_and_user(c, mail)
    first = M.run_due(ctx)
    assert first["checkpoint"] and first["verify"]["ok"]
    assert M.run_due(ctx) == {}
    assert c.post("/api/admin/audit/maintenance/run", headers=uh).status_code == 403
    assert c.post("/api/admin/audit/maintenance/run?force=true", headers=ah).status_code == 200
    assert c.get("/api/admin/audit/checkpoints", headers=ah).json()
    assert c.get("/api/admin/audit/tables", headers=ah).json()


def test_db_access_entries_carry_requester():
    c, ctx, mail, _ = boot(audit_db_access=True)
    ah, uh = setup_admin_and_user(c, mail)
    me = {"uuid": ctx.identity.subject(uh["Authorization"].removeprefix("Bearer ").strip())}
    c.get("/api/forms", headers=uh)
    rows = [e for e in ctx.audit.entries() if e["action"] == "db.access"]
    assert rows and any(e["actor"] == me["uuid"] for e in rows)
    r = next(e for e in rows if e["actor"] == me["uuid"])
    assert r["meta"]["ops"] and "value" not in str(r["meta"]).lower()
    assert ctx.audit.verify()["ok"]
