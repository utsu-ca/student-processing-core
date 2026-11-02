import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_portal import activate, login, register  # noqa: E402

from utsu_portal import create_app, security
from utsu_portal.authsvc import create_auth_app
from utsu_portal.identity import RemoteIdentity
from utsu_portal.mailer import OutboxMailer
from utsu_portal.settings import Settings
from utsu_portal.store import MemoryStore

SVC = "svc-" + "t" * 40


def make(auth_pem=None):
    mail = OutboxMailer()
    astore, pstore = MemoryStore(), MemoryStore()
    key = security.load_signing_key(auth_pem, "a" * 40)
    auth = create_auth_app(Settings(secret="a" * 40, admins=("boss",), service_token=SVC), astore, mail)
    ac = TestClient(auth)
    # the auth service signs with a key derived from ITS secret; the portal only receives the public half
    pub = security.public_pem(auth.state.ctx.identity.key)

    def fetch(path, body=None):
        r = ac.post(path, json=body, headers={"Authorization": "Bearer " + SVC}) if body is not None else ac.get(path, headers={"Authorization": "Bearer " + SVC})
        return None if r.status_code == 404 else r.json()

    portal = create_app(Settings(secret="p" * 40), pstore, mail, identity=RemoteIdentity(pub, fetch))
    return ac, TestClient(portal), mail, astore, pstore


def test_split_deployment_roundtrip():
    ac, pc, mail, astore, pstore = make()
    register(ac, mail, "boss", "boss@x.ca", sn="9999999999", first="Bo", last="Ss")
    ah = login(ac, mail, "boss")
    register(ac, mail, "ada", "ada@x.ca")
    uh = login(ac, mail, "ada")
    # a token minted by the auth service works on the portal...
    assert pc.get("/api/me/submissions", headers=uh).status_code == 200
    assert pc.get("/api/admin/audit", headers=uh).status_code == 403
    assert pc.get("/api/admin/audit", headers=ah).status_code == 200
    # ...the portal itself has no identity surface and stores no accounts
    assert pc.post("/api/auth/register", json={}).status_code in (404, 405)
    assert pc.get("/api/admin/verifications", headers=ah).status_code == 404
    assert not pstore.list("accounts") and not pstore.list("users")
    assert astore.list("accounts") and not astore.list("form_versions")
    f = pc.get("/api/forms/agm_rsvp/static").json()
    body = {"version": f["version"], "hash": f["hash"], "data": {}}
    assert pc.put("/api/forms/agm_rsvp/submission", json=body, headers=uh).status_code == 403  # not verified yet
    activate(ac, ah, uh)
    pc.app.state.ctx.identity._cache.clear()  # status lookups are cached for RemoteIdentity.CACHE_TTL seconds
    assert pc.put("/api/forms/agm_rsvp/submission", json={"version": f["version"], "hash": f["hash"], "data": {}}, headers=uh).status_code != 403


def test_portal_cannot_mint_or_replay_tokens():
    ac, pc, mail, *_ = make()
    register(ac, mail, "ada", "ada@x.ca")
    uid = ac.app.state.ctx.uid("ada")
    forged = security.sign_user_token(security.load_signing_key(None, "p" * 40), uid, 600)  # portal's own derived key
    assert pc.get("/api/me/submissions", headers={"Authorization": "Bearer " + forged}).status_code == 401
    old = security.sign_token("p" * 40, uid, 600)  # legacy HMAC format
    assert pc.get("/api/me/submissions", headers={"Authorization": "Bearer " + old}).status_code == 401
    tok = login(ac, mail, "ada")["Authorization"][7:]
    assert pc.get("/api/me/submissions", headers={"Authorization": "Bearer " + tok[:-2] + "AA"}).status_code == 401


def test_internal_api_requires_service_token():
    ac, *_ = make()
    assert ac.get("/internal/accounts/x").status_code == 404
    assert ac.get("/internal/ping", headers={"Authorization": "******"}).status_code == 404
    assert ac.get("/internal/ping", headers={"Authorization": "Bearer " + SVC}).status_code == 200
    # disabled entirely when no service token is configured
    bare = TestClient(create_auth_app(Settings(secret="a" * 40), MemoryStore(), OutboxMailer()))
    assert bare.get("/internal/ping", headers={"Authorization": "Bearer " + SVC}).status_code == 404


def test_portal_fails_closed_when_auth_down():
    ac, pc, mail, *_ = make()
    register(ac, mail, "ada", "ada@x.ca")
    tok = login(ac, mail, "ada")
    def boom(*a, **k):
        raise ConnectionError
    pc.app.state.ctx.identity.fetch = boom
    pc.app.state.ctx.identity._cache.clear()
    assert pc.get("/api/me/submissions", headers=tok).status_code == 401
    assert pc.get("/readyz").status_code == 503


def test_remote_config_requires_keys():
    with pytest.raises(RuntimeError):
        create_app(Settings(secret="p" * 40, auth_url="https://auth.internal"), MemoryStore())
