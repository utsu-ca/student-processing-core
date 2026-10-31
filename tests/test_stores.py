import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
from utsu_portal import create_app  # noqa: E402
from utsu_portal.mailer import OutboxMailer, SMTPMailer  # noqa: E402
from utsu_portal.settings import Settings  # noqa: E402
from utsu_portal.store import ChunkingStore, MemoryStore, SQLiteStore, SQLStore, open_store  # noqa: E402


class Capped(MemoryStore):
    """Memory store that rejects documents over a size cap, like Firestore's 1 MiB limit."""

    def put(self, col, id, doc):
        assert len(str(doc)) < 1000, "document too large"
        super().put(col, id, doc)

    def create(self, col, id, doc):
        assert len(str(doc)) < 1000, "document too large"
        return super().create(col, id, doc)


def make(kind, tmp_path):
    return {"memory": MemoryStore, "sqlite": lambda: SQLiteStore(), "sql": lambda: SQLStore(f"sqlite:///{tmp_path}/t.db"),
            "chunk": lambda: ChunkingStore(Capped(), 300)}[kind]()


@pytest.mark.parametrize("kind", ["memory", "sqlite", "sql", "chunk"])
def test_store_contract(kind, tmp_path):
    s = make(kind, tmp_path)
    assert s.get("c", "a") is None
    assert s.create("c", "a", {"id": "a", "n": 1, "t": "x"})
    assert not s.create("c", "a", {"id": "a", "n": 2})
    s.put("c", "a", {"id": "a", "n": 3, "t": "x"})
    s.put("c", "b", {"id": "b", "n": 3, "t": "y"})
    s.put("d", "a", {"id": "a", "n": 9})
    assert s.get("c", "a")["n"] == 3
    assert {d["id"] for d in s.list("c")} == {"a", "b"}
    assert [d["id"] for d in s.list("c", t="y", n=3)] == ["b"]
    s.delete("c", "a")
    s.delete("c", "a")
    assert s.get("c", "a") is None and s.get("d", "a")["n"] == 9


def test_chunking_large_values_roundtrip():
    s = ChunkingStore(Capped(), 300)
    big = "é" * 2000
    assert s.create("m", "k1", {"id": "k1", "img": big, "n": 1})
    assert s.get("m", "k1")["img"] == big
    assert s.list("m", n=1)[0]["img"] == big
    s.put("m", "k1", {"id": "k1", "img": "small"})  # old chunks are cleaned up
    assert s.get("m", "k1")["img"] == "small" and s.inner.list("m__chunks") == []
    s.put("m", "k1", {"id": "k1", "img": big})
    s.delete("m", "k1")
    assert s.inner.list("m__chunks") == [] and s.get("m", "k1") is None


def test_open_store_specs(tmp_path):
    assert isinstance(open_store(f"sql:sqlite:///{tmp_path}/x.db"), SQLStore)
    assert isinstance(open_store("test_stores:Capped"), MemoryStore)
    with pytest.raises(ValueError):
        open_store("nope")
    with pytest.raises(ValueError):
        open_store("sql")


def test_portal_runs_on_sql_store(tmp_path):
    mail = OutboxMailer()
    c = TestClient(create_app(Settings(secret="s" * 32, admins=("boss",), store=f"sql:sqlite:///{tmp_path}/p.db"), mailer=mail))
    assert c.get("/healthz").json() == {"ok": True}
    r = c.post("/api/auth/register", json={"username": "boss", "email": "b@x.ca", "first_name": "B", "last_name": "S", "student_number": "1234567890"})
    assert r.status_code == 201
    assert c.post("/api/auth/verify-email", json={"username": "boss", "code": mail.outbox[-1][2].split("code is ")[1][:6]}).json()["status"] == "verified"
    assert len(c.get("/api/forms").json()) == 3  # seeded forms persisted via SQL


def test_smtp_url_parsing():
    m = SMTPMailer("smtps://" + "us%40er:p%3Ass" + "@smtp.example.com:2465", "a@b.c")
    assert (m.ssl, m.host, m.port, m.user, m.password) == (True, "smtp.example.com", 2465, "us@er", "p:ss")
