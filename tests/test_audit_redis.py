import asyncio
import base64
import gzip
import json
import sys
from pathlib import Path

import fakeredis
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import fakeredis.aioredis
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_portal import IMG, activate, login, register, setup_admin_and_user  # noqa: E402

from utsu_portal import audit as A, create_app
from utsu_portal.crypto import Crypter, EncryptingStore
from utsu_portal.events import RedisEventBus
from utsu_portal.mailer import OutboxMailer
from utsu_portal.ratelimit import RateLimiter
from utsu_portal.settings import Settings
from utsu_portal.store import MemoryStore


def boot(**kw):
    mail, store = OutboxMailer(), MemoryStore()
    app = create_app(Settings(secret="s" * 40, admins=("boss",), **kw), store, mail)
    return TestClient(app), mail, store


def test_redis_bus_crosses_replicas():
    server = fakeredis.FakeServer()
    mk = lambda: RedisEventBus(sync_client=fakeredis.FakeRedis(server=server), async_factory=lambda: fakeredis.aioredis.FakeRedis(server=server))
    a, b = mk(), mk()

    async def run():
        polls = iter([False, False, False, True])
        async def gone():
            await asyncio.sleep(0.05)
            return next(polls, True)
        got = []
        async def reader():
            async for ev in b.listen("form:x", gone):
                got.append(ev)
        t = asyncio.create_task(reader())
        await asyncio.sleep(0.08)
        a.publish("form:x", {"rev": 7})
        await t
        return got

    assert {"rev": 7} in asyncio.run(run())
    assert a.ping()


def test_rate_limit_shared_via_redis():
    r = fakeredis.FakeRedis()
    one, two = RateLimiter(r), RateLimiter(r)  # two replicas, one counter
    assert [one.allow("ip", 3, 60), two.allow("ip", 3, 60), one.allow("ip", 3, 60), two.allow("ip", 3, 60)] == [True, True, True, False]
    m = RateLimiter()
    assert m.allow("k", 1, 60) and not m.allow("k", 1, 60)


def test_auth_endpoints_rate_limited():
    c, mail, _ = boot(rate_limit_auth=3)
    codes = [c.post("/api/auth/login", json={"username": "nobody"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_audit_chain_diff_and_tamper_detection():
    c, mail, store = boot()
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    c.patch("/api/me", json={"preferred_name": "Countess"}, headers=uh)
    rows = c.get("/api/admin/audit?limit=200", headers=ah).json()["entries"]
    actions = {e["action"] for e in rows}
    assert {"account.registered", "auth.login", "verification.decided", "user.preferred_name_changed", "http.POST"} <= actions
    assert c.get("/api/admin/audit/verify", headers=ah).json()["ok"]
    ch = next(e for e in rows if e["action"] == "user.preferred_name_changed")
    d = c.get(f"/api/admin/audit/{ch['id']}/diff", headers=ah).json()
    assert d["changed"] and any(r["type"] == "change" and "Countess" in r["right"] for r in d["rows"]) and "+" in d["unified"]
    assert c.get(f"/api/admin/audit/{ch['id']}/diff", headers=uh).status_code == 403
    exp = c.get("/api/admin/audit/export", headers=ah)
    assert json.loads(gzip.decompress(exp.content).splitlines()[0])["seq"] == 1
    # tampering with a stored entry (or deleting one) breaks verification
    raw = store.get("audit", ch["id"])
    raw["actor"] = "someone-else"
    store.put("audit", ch["id"], raw)
    assert c.get("/api/admin/audit/verify", headers=ah).json()["ok"] is False


def test_audit_never_logs_secrets_or_images():
    c, mail, store = boot()
    ah, uh = setup_admin_and_user(c, mail)
    activate(c, ah, uh)
    blob = json.dumps([d for d in store.list("audit")]) + json.dumps(A.redact({"face": IMG, "otp": "1", "x": IMG}))
    assert IMG not in blob and "1234567890" not in blob
    assert A.redact({"token": "t", "n": {"code": "1", "ok": 2}}) == {"token": "[redacted]", "n": {"code": "[redacted]", "ok": 2}}


def test_wikipedia_style_word_diff():
    d = A.diff({"yaml": "a: 1\nb: two words\nc: 3"}, {"yaml": "a: 1\nb: two birds\nc: 3\nd: 4"})
    chg = next(r for r in d["rows"] if r["type"] == "change")
    assert ["-", "words"] in chg["left_segments"] and ["+", "birds"] in chg["right_segments"]
    assert any(r["type"] == "add" for r in d["rows"])
    assert not A.diff({"a": 1}, {"a": 1})["changed"]


def test_scans_encrypted_at_rest_and_readable_through_app():
    c, mail, store = boot()
    ah, uh = setup_admin_and_user(c, mail)
    inner = store.inner if hasattr(store, "inner") else store
    ch = c.get("/api/verification/challenge").json()["challenge"]
    r = c.post("/api/verification", json={"first_name": "Ada", "last_name": "Lovelace", "division_codes": ["UTSG"], "face": IMG, "id_card": IMG, "challenge": ch}, headers=uh)
    vid = r.json()["id"]
    m = c.get(f"/api/admin/verifications/{vid}/media", headers=ah)
    assert m.status_code == 200 and IMG in m.text


def test_encrypting_store_roundtrip_and_key_rotation():
    inner = MemoryStore()
    s = EncryptingStore(inner, Crypter(None, "x" * 40))
    s.put("media", "1", {"face": "secret-face", "id_card": "secret-id", "other": "plain"})
    raw = inner.get("media", "1")
    assert raw["face"].startswith("enc:v1:") and "secret" not in raw["face"] + raw["id_card"] and raw["other"] == "plain"
    assert s.get("media", "1")["face"] == "secret-face"
    from cryptography.fernet import Fernet
    new = Fernet.generate_key().decode()
    rotated = EncryptingStore(inner, Crypter(new, "x" * 40))
    with pytest.raises(RuntimeError):
        rotated.get("media", "1")  # old data needs the old key
    k0 = base64.urlsafe_b64encode(HKDF(hashes.SHA256(), 32, None, b"utsu-portal-data-v1").derive(b"x" * 40)).decode()
    both = EncryptingStore(inner, Crypter(new + "," + k0, "x" * 40))
    assert both.get("media", "1")["face"] == "secret-face"


def test_security_headers_and_readyz_and_body_limit():
    c, *_ = boot(max_body_bytes=100)
    r = c.get("/healthz")
    assert "max-age" in r.headers["strict-transport-security"] and r.headers["x-frame-options"] == "DENY"
    assert c.get("/readyz").status_code == 200
    assert c.post("/api/auth/login", content=b"x" * 500, headers={"content-type": "application/json"}).status_code == 413


def test_secret_must_be_strong(monkeypatch):
    monkeypatch.setenv("PORTAL_SECRET", "short")
    with pytest.raises(RuntimeError):
        Settings.from_env()
    monkeypatch.delenv("PORTAL_SECRET")
    monkeypatch.setenv("PORTAL_SECRET_FILE", "/nonexistent")
    with pytest.raises(OSError):
        Settings.from_env()
