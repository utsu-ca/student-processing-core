import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_authsvc import make  # noqa: E402
from test_portal import activate, login, register  # noqa: E402

from utsu_portal.context import ROLES


def setup():
    ac, pc, mail, astore, pstore = make()
    register(ac, mail, "boss", "boss@x.ca", sn="9999999999", first="Bo", last="Ss")
    ah = login(ac, mail, "boss")
    register(ac, mail, "ada", "ada@x.ca")
    uh = login(ac, mail, "ada")
    activate(ac, ah, uh)  # ada: verified applicant, no roles
    return ac, pc, mail, astore, ah, uh


def put(ac, h, user, roles):
    return ac.put("/api/admin/roles", json={"username": user, "roles": roles}, headers=h)


def test_roles_scope_access_and_are_issued_by_auth_service():
    ac, pc, mail, astore, ah, uh = setup()
    assert ac.get("/api/admin/verifications", headers=uh).status_code == 403
    assert put(ac, uh, "ada", ["admin"]).status_code == 403
    assert put(ac, ah, "ada", ["auditor"]).json()["roles"] == ["auditor"]
    pc.app.state.ctx.identity._cache.clear()
    # auditor reads the audit log on the portal, but cannot verify identities, edit forms or run maintenance
    assert pc.get("/api/admin/audit", headers=uh).status_code == 200
    assert pc.get("/api/admin/forms", headers=uh).status_code == 403
    assert ac.get("/api/admin/verifications", headers=uh).status_code == 403
    assert ac.post("/api/admin/auth-audit/maintenance/run", headers=uh).status_code == 403
    assert put(ac, ah, "ada", ["forms_editor", "checkin"]).status_code == 200
    pc.app.state.ctx.identity._cache.clear()
    assert pc.get("/api/admin/forms", headers=uh).status_code == 200
    assert pc.get("/api/admin/audit", headers=uh).status_code == 403  # revoked
    assert pc.get("/api/admin/agm/attendance", headers=uh).status_code == 200
    assert astore.get("accounts", ac.app.state.ctx.uid("ada"))["roles"] == ["checkin", "forms_editor"]
    # granting is audited with before/after
    entry = next(e for e in ac.app.state.ctx.audit.entries() if e["action"] == "role.set")
    assert entry["actor"] == ac.app.state.ctx.uid("boss")


def test_grant_rules():
    ac, pc, mail, astore, ah, uh = setup()
    assert put(ac, ah, "boss", ["verifier"]).status_code == 403  # no self changes
    assert put(ac, ah, "ada", ["root"]).status_code == 422  # unknown role
    assert put(ac, ah, "nobody", ["auditor"]).status_code == 404
    register(ac, mail, "eve", "eve@x.ca", sn="5555555555")  # pending review, not verified
    assert put(ac, ah, "eve", ["auditor"]).status_code == 409
    assert put(ac, ah, "ada", ["admin"]).status_code == 200
    assert set(ROLES) >= {"admin", "verifier"} and ac.get("/api/admin/roles", headers=ah).json().keys() == ROLES.keys()
    uh2 = login(ac, mail, "ada")
    assert put(ac, uh2, "boss", []).status_code == 200  # a second admin may demote the first
    assert put(ac, ah, "ada", []).status_code == 403  # ...who is no longer admin
    holders = ac.get("/api/admin/roles/holders", headers=uh2).json()
    assert [h["roles"] for h in holders] == [["admin"]]


def test_sole_remaining_admin_cannot_demote_self():
    ac, pc, mail, astore, ah, uh = setup()
    put(ac, ah, "ada", ["admin"])
    uh2 = login(ac, mail, "ada")
    assert put(ac, uh2, "boss", []).status_code == 200
    # ada is now the only admin; boss (no longer admin) cannot act, and ada cannot demote herself
    assert put(ac, uh2, "ada", []).status_code == 403


def test_service_token_rotation_accepts_old_and_new():
    from fastapi.testclient import TestClient

    from utsu_portal.authsvc import create_auth_app
    from utsu_portal.mailer import OutboxMailer
    from utsu_portal.settings import Settings
    from utsu_portal.store import MemoryStore

    old, new = "o" * 40, "n" * 40
    c = TestClient(create_auth_app(Settings(secret="a" * 40, service_token=f"{old},{new}"), MemoryStore(), OutboxMailer()))
    for t in (old, new):
        assert c.get("/internal/ping", headers={"Authorization": "Bearer " + t}).status_code == 200
    assert c.get("/internal/ping", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 404
