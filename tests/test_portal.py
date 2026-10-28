import base64

import pytest
from fastapi.testclient import TestClient

from utsu_portal import create_app, formdef
from utsu_portal.mailer import OutboxMailer
from utsu_portal.settings import Settings

IMG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


@pytest.fixture
def env():
    mail = OutboxMailer()
    app = create_app(Settings(secret="s" * 32, admins=("boss",)), mailer=mail)
    return TestClient(app), mail


def code(mail):
    return mail.outbox[-1][2].split("code is ")[1][:6]


def register(c, mail, user, email, sn="1234567890", first="Ada", last="Lovelace"):
    r = c.post("/api/auth/register", json={"username": user, "email": email, "first_name": first, "last_name": last, "student_number": sn})
    assert r.status_code == 201, r.text
    assert c.post("/api/auth/verify-email", json={"username": user, "code": code(mail)}).status_code == 200


def login(c, mail, user):
    c.post("/api/auth/login", json={"username": user})
    r = c.post("/api/auth/token", json={"username": user, "code": code(mail)})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def scan(c, h, **extra):
    ch = c.get("/api/verification/challenge").json()["challenge"]
    return {"first_name": "Ada", "last_name": "Lovelace", "division_codes": ["UTSG"], "face": IMG, "id_card": IMG, "challenge": ch, **extra}


def setup_admin_and_user(c, mail):
    register(c, mail, "boss", "boss@x.ca", sn="9999999999", first="Bo", last="Ss")
    ah = login(c, mail, "boss")
    register(c, mail, "ada", "ada@x.ca")
    uh = login(c, mail, "ada")
    return ah, uh


def activate(c, ah, uh):
    r = c.post("/api/verification", json=scan(c, uh), headers=uh)
    assert r.status_code == 201, r.text
    assert c.post(f"/api/admin/verifications/{r.json()['id']}/decision", json={"approve": True}, headers=ah).status_code == 200


def test_user_record_and_lifecycle(env):
    c, mail = env
    ah, uh = setup_admin_and_user(c, mail)
    me = c.get("/api/me", headers=uh).json()
    assert set(me["user"]) == {"uuid", "email", "preferred_name", "first_name", "last_name", "student_number"}
    assert me["user"]["student_number"] == "7890" and me["status"] == "pending_review"
    # unverified cannot submit
    f = c.get("/api/forms/agm_rsvp/static").json()
    body = {"version": f["version"], "hash": f["hash"], "data": {}}
    assert c.put("/api/forms/agm_rsvp/submission", json=body, headers=uh).status_code == 403
    activate(c, ah, uh)
    assert c.get("/api/me", headers=uh).json()["status"] == "verified"
    assert c.get("/api/admin/verifications", headers=uh).status_code == 403
    r = c.patch("/api/me", json={"preferred_name": "Countess"}, headers=uh)
    assert r.json()["user"]["preferred_name"] == "Countess" and r.json()["user"]["first_name"] == "Ada"


def test_submission_validation_and_status(env):
    c, mail = env
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    f = c.get("/api/forms/agm_rsvp/static").json()
    base = {"version": f["version"], "hash": f["hash"]}
    bad = c.put("/api/forms/agm_rsvp/submission", json={**base, "data": {"attendance": "Online", "division": "UTSG", "eligible": True, "proxy": True}}, headers=uh)
    assert bad.status_code == 422  # proxy_name required when proxy
    bad = c.put("/api/forms/agm_rsvp/submission", json={**base, "data": {"attendance": "Online", "division": "UTSG", "eligible": True, "x": 1}}, headers=uh)
    assert bad.status_code == 422
    ok = c.put("/api/forms/agm_rsvp/submission", json={**base, "data": {"attendance": "Online", "division": "UTSG", "eligible": True, "accessibility": "a\x00b\u202e  c"}}, headers=uh)
    assert ok.status_code == 200
    mine = c.get("/api/me/submissions", headers=uh).json()[0]
    assert mine["data"]["accessibility"] == "ab  c" and mine["status"] == "submitted"
    uid = c.get("/api/me", headers=uh).json()["user"]["uuid"]
    assert c.post(f"/api/admin/submissions/agm_rsvp/{uid}/status", json={"status": "accepted", "note": "ok"}, headers=ah).status_code == 200
    assert c.get("/api/me/submissions", headers=uh).json()[0]["status"] == "accepted"
    assert len(c.get("/api/admin/forms/agm_rsvp/submissions", headers=ah).json()) == 1


def test_form_versioning_and_publish(env):
    c, mail = env
    ah, _ = setup_admin_and_user(c, mail)
    y = c.get("/api/admin/forms/agm_rsvp", headers=ah).json()["draft_yaml"]
    # same version, changed definition -> error
    r = c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y.replace("Accessibility needs", "Access needs")}, headers=ah)
    assert r.status_code == 409 and "bump" in r.text
    # reformatting only is fine (same hash)
    assert c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y}, headers=ah).status_code == 200
    y2 = y.replace("version: 1", "version: 2").replace("Accessibility needs", "Access needs")
    r = c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y2}, headers=ah)
    assert r.status_code == 200
    old = c.get("/api/forms/agm_rsvp/static").json()
    assert old["version"] == 1  # draft is not live
    assert c.post("/api/admin/forms/agm_rsvp/publish", headers=ah).status_code == 200
    new = c.get("/api/forms/agm_rsvp/static").json()
    assert new["version"] == 2 and new["hash"] != old["hash"]
    # stale clients are told to reload
    _, uh = None, None
    assert c.post("/api/admin/forms/agm_rsvp/unpublish", headers=ah).status_code == 200
    assert c.get("/api/forms/agm_rsvp/static").status_code == 404
    # optimistic concurrency
    assert c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y2, "base_rev": 0}, headers=ah).status_code == 409


def test_stale_hash_rejected(env):
    c, mail = env
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    r = c.put("/api/forms/agm_rsvp/submission", json={"version": 1, "hash": "0" * 64, "data": {}}, headers=uh)
    assert r.status_code == 409


def test_recovery_flow(env):
    c, mail = env
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    assert c.post("/api/recovery/start", json={"username": "ada", "student_number_trunc": "0000"}).status_code == 200
    n = len(mail.outbox)
    c.post("/api/recovery/start", json={"username": "ada", "student_number_trunc": "7890"})
    assert len(mail.outbox) == n + 1
    c.post("/api/recovery/email-otp", json={"username": "ada", "student_number_trunc": "7890", "new_email": "new@x.ca"})
    assert mail.outbox[-1][0] == "new@x.ca"
    body = scan(c, uh, username="ada", student_number_trunc="7890", new_email="new@x.ca", otp=code(mail), last_name="Byron")
    r = c.post("/api/recovery/request", json=body)
    assert r.status_code == 201, r.text
    assert c.get("/api/me", headers=uh).json()["user"]["email"] == "ada@x.ca"  # unchanged until approved
    media = c.get(f"/api/admin/verifications/{r.json()['id']}/media", headers=ah)
    assert media.status_code == 200
    c.post(f"/api/admin/verifications/{r.json()['id']}/decision", json={"approve": True}, headers=ah)
    u = c.get("/api/me", headers=uh).json()["user"]
    assert (u["email"], u["last_name"]) == ("new@x.ca", "Byron")
    assert c.get(f"/api/admin/verifications/{r.json()['id']}/media", headers=ah).status_code == 404  # images purged


def test_duplicates_and_otp_lockout(env):
    c, mail = env
    setup_admin_and_user(c, mail)
    r = c.post("/api/auth/register", json={"username": "ADA", "email": "o@x.ca", "first_name": "A", "last_name": "B", "student_number": "1234567890"})
    assert r.status_code == 409
    c.post("/api/auth/login", json={"username": "ada"})
    good = code(mail)
    for _ in range(5):
        assert c.post("/api/auth/token", json={"username": "ada", "code": "000000" if good != "000000" else "111111"}).status_code == 400
    assert c.post("/api/auth/token", json={"username": "ada", "code": good}).status_code == 400


def test_schema_validation():
    with pytest.raises(formdef.FormError):
        formdef.parse_yaml("id: a\nversion: 1\ntitle: t\nfields:\n  - {id: x, type: select, label: X}\n")
    with pytest.raises(formdef.FormError):
        formdef.parse_yaml("id: a\nversion: 1\ntitle: t\nfields:\n  - {id: x, type: text, label: X, show_if: {field: y, equals: 1}}\n")
    a = formdef.parse_yaml("id: a\nversion: 1\ntitle: t\nfields:\n  - {id: x, type: text, label: X}\n")
    b = formdef.parse_yaml("fields: [{label: X, type: text, id: x}]\ntitle: t\nversion: 1\nid: a\n")
    assert formdef.schema_hash(a) == formdef.schema_hash(b)
