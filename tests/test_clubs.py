import base64
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from fastapi.testclient import TestClient

from test_portal import activate, code, login, register  # noqa: F401
from utsu_portal import create_app
from utsu_portal.mailer import OutboxMailer
from utsu_portal.modules.clubs import split_tax
from utsu_portal.settings import Settings

PDF = lambda b: "data:application/pdf;base64," + base64.b64encode(b).decode()


@pytest.fixture
def env():
    mail = OutboxMailer()
    c = TestClient(create_app(Settings(secret="s" * 32, admins=("boss", "boss2")), mailer=mail))
    heads = {}
    for i, u in enumerate(("boss", "boss2")):
        register(c, mail, u, f"{u}@x.ca", sn=f"900000000{i}", first="Bo", last="Ss")
        heads[u] = login(c, mail, u)
    register(c, mail, "ada", "ada@x.ca")
    heads["ada"] = login(c, mail, "ada")
    activate(c, heads["boss"], heads["ada"])
    return c, heads


def approved_club(c, h):
    cid = c.post("/api/clubs", json={"name": "Chess Club", "description": "We play chess weekly.", "category": "games"}, headers=h["ada"]).json()["id"]
    assert c.post(f"/api/admin/clubs/{cid}/decision", json={"approve": True}, headers=h["boss"]).json()["status"] == "pending"
    assert c.post(f"/api/admin/clubs/{cid}/decision", json={"approve": True}, headers=h["boss"]).status_code == 409
    assert c.post(f"/api/admin/clubs/{cid}/decision", json={"approve": True}, headers=h["boss2"]).json()["status"] == "approved"
    return cid


def receipt(c, h, cid, data=b"%PDF-1"):
    return c.post(f"/api/clubs/{cid}/receipts", json={"data_url": PDF(data)}, headers=h["ada"])


def tx(c, h, cid, rid, amount=11300, kind="expense", **kw):
    return c.post(f"/api/clubs/{cid}/transactions", json={"kind": kind, "amount_cents": amount, "category": "supplies", "description": "boards",
                                                          "date": "2026-10-01", "receipt_ids": [rid], **kw}, headers=h["ada"])


def test_split_tax():
    assert split_tax(11300, 1300, True) == (10000, 1300, 11300)
    assert split_tax(10000, 1300, False) == (10000, 1300, 11300)
    assert split_tax(500, 0, True) == (500, 0, 500)


def test_registration_needs_two_admins_and_unique_name(env):
    c, h = env
    cid = approved_club(c, h)
    assert c.post("/api/clubs", json={"name": "chess  club!", "description": "dup dup dup dup", "category": "games"}, headers=h["ada"]).status_code == 409
    assert c.get(f"/api/clubs/{cid}", headers=h["boss"]).status_code == 200
    assert c.get(f"/api/clubs/{cid}", headers=h["boss2"]).json()["status"] == "approved"


def test_unapproved_club_cannot_transact(env):
    c, h = env
    cid = c.post("/api/clubs", json={"name": "Go Club", "description": "We play go weekly.", "category": "games"}, headers=h["ada"]).json()["id"]
    rid = receipt(c, h, cid).json()["id"]
    assert tx(c, h, cid, rid).status_code == 409


def test_expense_flow_receipts_and_tax(env):
    c, h = env
    cid = approved_club(c, h)
    r = receipt(c, h, cid)
    assert r.status_code == 201
    assert receipt(c, h, cid).status_code == 409  # duplicate file
    assert c.post(f"/api/clubs/{cid}/receipts", json={"data_url": "data:text/html;base64,AAAA"}, headers=h["ada"]).status_code == 422
    rid = r.json()["id"]
    no = c.post(f"/api/clubs/{cid}/transactions", json={"kind": "expense", "amount_cents": 100, "category": "x1", "description": "abc",
                                                      "date": "2026-10-01"}, headers=h["ada"])
    assert no.status_code == 422  # receipt required
    t = tx(c, h, cid, rid).json()
    assert (t["net_cents"], t["tax_cents"], t["required_approvals"], t["status"]) == (10000, 1300, 1, "pending")
    assert tx(c, h, cid, rid).status_code == 422  # receipt already used
    # no funds yet
    assert c.post(f"/api/admin/transactions/{t['id']}/decision", json={"approve": True}, headers=h["boss"]).status_code == 409
    assert c.post(f"/api/admin/clubs/{cid}/grants", json={"amount_cents": 50000}, headers=h["boss"]).status_code == 201
    # the 409 above must not have recorded the approval
    assert c.post(f"/api/admin/transactions/{t['id']}/decision", json={"approve": True}, headers=h["boss"]).json()["status"] == "approved"
    s = c.get(f"/api/clubs/{cid}/summary", headers=h["ada"]).json()
    assert (s["expense_cents"], s["tax_paid_cents"], s["balance_cents"]) == (11300, 1300, 50000 - 11300)
    assert s["expenses_by_category"] == {"supplies": 11300}


def test_large_expense_needs_two_and_rejection_is_final(env):
    c, h = env
    cid = approved_club(c, h)
    c.post(f"/api/admin/clubs/{cid}/grants", json={"amount_cents": 500000}, headers=h["boss"])
    t = tx(c, h, cid, receipt(c, h, cid).json()["id"], amount=60000).json()
    assert t["required_approvals"] == 2
    assert c.post(f"/api/admin/transactions/{t['id']}/decision", json={"approve": True}, headers=h["boss"]).json()["status"] == "pending"
    assert c.post(f"/api/admin/transactions/{t['id']}/decision", json={"approve": False, "note": "no"}, headers=h["boss2"]).json()["status"] == "rejected"
    assert c.get(f"/api/clubs/{cid}/summary", headers=h["ada"]).json()["expense_cents"] == 0


def test_permissions(env):
    c, h = env
    cid = approved_club(c, h)
    assert c.post(f"/api/clubs/{cid}/members", json={"username": "nobody", "role": "treasurer"}, headers=h["ada"]).status_code == 422
    assert c.post("/api/admin/clubs/x/decision", json={"approve": True}, headers=h["ada"]).status_code == 403
    assert c.get(f"/api/clubs/{cid}/transactions", headers=h["boss"]).status_code == 200
