import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_portal import activate, login, register, setup_admin_and_user  # noqa: E402
from test_stores import make  # noqa: E402

from utsu_portal import create_app
from utsu_portal.crypto import Crypter, EncryptingStore
from utsu_portal.mailer import OutboxMailer
from utsu_portal.settings import Settings
from utsu_portal.store import REV, Conflict, MemoryStore

KINDS = ["memory", "sqlite", "sql", "chunk"]


@pytest.mark.parametrize("kind", KINDS)
def test_replace_is_compare_and_swap(kind, tmp_path):
    s = make(kind, tmp_path)
    assert not s.replace("c", "a", {"n": 1}, 0)  # missing document
    s.update("c", "a", lambda d: {"n": 1})
    assert s.get("c", "a")[REV] == 1
    assert s.replace("c", "a", {"n": 2}, 1) and s.get("c", "a") == {"n": 2, REV: 2}
    assert not s.replace("c", "a", {"n": 3}, 1)  # stale revision loses
    assert s.get("c", "a")["n"] == 2


@pytest.mark.parametrize("kind", KINDS)
def test_update_retries_and_aborts(kind, tmp_path):
    s = make(kind, tmp_path)
    s.create("c", "a", {"n": 0})
    calls = []

    def bump(d):
        calls.append(1)
        if len(calls) == 1:  # a competing writer sneaks in after our read
            s.update("c", "a", lambda x: {**x, "n": x["n"] + 10})
        return {**d, "n": d["n"] + 1}

    assert s.update("c", "a", bump)["n"] == 11 and len(calls) == 2
    assert s.update("c", "a", lambda d: None)["n"] == 11  # None = leave unchanged
    with pytest.raises(KeyError):
        s.update("c", "a", lambda d: (_ for _ in ()).throw(KeyError()))
    assert s.get("c", "a")["n"] == 11


def test_update_gives_up_under_endless_contention():
    s = MemoryStore()
    s.update("c", "a", lambda d: {"n": 0})

    def always_lose(d):
        s.update("c", "a", lambda x: {**x, "n": x["n"] + 1})
        return {**d, "n": -1}

    with pytest.raises(Conflict):
        s.update("c", "a", always_lose, retries=3)


@pytest.mark.parametrize("kind", ["memory", "sql"])
def test_no_lost_updates_across_threads(kind, tmp_path):
    s = make(kind, tmp_path)
    s.create("c", "ctr", {"n": 0})

    def work():
        for _ in range(15):
            s.update("c", "ctr", lambda d: {**d, "n": d["n"] + 1}, retries=200)

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert s.get("c", "ctr")["n"] == 90


def test_encrypting_store_passes_locking_through():
    inner = MemoryStore()
    s = EncryptingStore(inner, Crypter(None, "x" * 40))
    s.update("media", "1", lambda d: {"face": "f"})
    s.update("media", "1", lambda d: {**d, "face": d["face"] + "g"})
    assert s.get("media", "1")["face"] == "fg" and inner.get("media", "1")["face"].startswith("enc:")


def boot():
    mail, store = OutboxMailer(), MemoryStore()
    return TestClient(create_app(Settings(secret="s" * 40, admins=("boss",)), store, mail)), mail, store


def test_otp_cannot_be_replayed_or_brute_forced_in_parallel():
    c, mail, store = boot()
    ctx = c.app.state.ctx
    ctx.send_otp("login", "u1", "a@x.ca")
    good = mail.outbox[-1][2].split("code is ")[1][:6]
    results = []

    def try_(code):
        results.append(ctx.check_otp("login", "u1", code) is not None)

    ts = [threading.Thread(target=try_, args=(good,)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1  # one redemption, however many race
    ctx.send_otp("login", "u2", "a@x.ca")
    wrong = [ctx.check_otp("login", "u2", f"{i:06d}") for i in range(1, 30)]
    assert all(w is None for w in wrong) and store.get("otps", "login:u2")["attempts"] == ctx.settings.otp_attempts


def test_stale_draft_edit_is_rejected_and_revision_advances():
    c, mail, _ = boot()
    ah, uh = setup_admin_and_user(c, mail)
    f = c.get("/api/admin/forms/agm_rsvp", headers=ah).json()
    y = f["draft_yaml"]
    ok = c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y, "base_rev": f["rev"]}, headers=ah)
    assert ok.status_code == 200 and ok.json()["rev"] == f["rev"] + 1
    stale = c.put("/api/admin/forms/agm_rsvp/draft", json={"yaml": y, "base_rev": f["rev"]}, headers=ah)
    assert stale.status_code == 409


def test_verification_decided_once_even_with_two_admins():
    c, mail, store = boot()
    ah, uh = setup_admin_and_user(c, mail)
    from test_portal import scan
    vid = c.post("/api/verification", json=scan(c, uh), headers=uh).json()["id"]
    codes = []

    def decide():
        codes.append(c.post(f"/api/admin/verifications/{vid}/decision", json={"approve": True}, headers=ah).status_code)

    ts = [threading.Thread(target=decide) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 404, 404, 404]
    assert store.get("verifications", vid)["status"] == "approved"
    assert sum(1 for e in store.list("audit") if e["action"] == "verification.decided") == 1


def test_sso_code_redeemable_once():
    from utsu_portal.modules import sso
    c, mail, store = boot()
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    uid = c.app.state.ctx.uid("ada")
    p = c.app.state.ctx.services["sso"]
    p.register_client("app", "sec", ("https://rp/cb",))
    key = sso._sha("code1")
    store.put("sso_codes", key, {"uuid": uid, "client": "app", "redirect_uri": "https://rp/cb", "nonce": "", "challenge": "", "exp": 2**40})
    form = {"grant_type": "authorization_code", "code": "code1", "redirect_uri": "https://rp/cb"}
    assert "id_token" in p.exchange(form, ("app", "sec"))
    with pytest.raises(sso.SSOError) as e:
        p.exchange(form, ("app", "sec"))
    assert e.value.error == "invalid_grant" and store.get("sso_codes", key) is None
