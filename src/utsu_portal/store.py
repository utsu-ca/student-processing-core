"""Database-agnostic document store. Implement `Store` to add a backend."""
from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import Any

Doc = dict[str, Any]


class Store(ABC):
    @abstractmethod
    def get(self, col: str, id: str) -> Doc | None: ...

    @abstractmethod
    def put(self, col: str, id: str, doc: Doc) -> None: ...

    @abstractmethod
    def delete(self, col: str, id: str) -> None: ...

    @abstractmethod
    def list(self, col: str, **equals: Any) -> list[Doc]:
        """All documents in `col` whose top-level fields equal the given values."""

    def create(self, col: str, id: str, doc: Doc) -> bool:
        """Insert only if absent. Backends should override with an atomic version."""
        if self.get(col, id) is not None:
            return False
        self.put(col, id, doc)
        return True


def _match(doc: Doc, equals: dict) -> bool:
    return all(doc.get(k) == v for k, v in equals.items())


class MemoryStore(Store):
    def __init__(self) -> None:
        self._d: dict[str, dict[str, Doc]] = {}
        self._lock = threading.RLock()

    def get(self, col, id):
        with self._lock:
            d = self._d.get(col, {}).get(id)
            return json.loads(json.dumps(d)) if d is not None else None

    def put(self, col, id, doc):
        with self._lock:
            self._d.setdefault(col, {})[id] = json.loads(json.dumps(doc))

    def delete(self, col, id):
        with self._lock:
            self._d.get(col, {}).pop(id, None)

    def list(self, col, **equals):
        with self._lock:
            return [json.loads(json.dumps(d)) for d in self._d.get(col, {}).values() if _match(d, equals)]

    def create(self, col, id, doc):
        with self._lock:
            if id in self._d.get(col, {}):
                return False
            self.put(col, id, doc)
            return True


class SQLiteStore(Store):
    def __init__(self, path: str = ":memory:") -> None:
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.execute("CREATE TABLE IF NOT EXISTS docs (col TEXT, id TEXT, body TEXT, PRIMARY KEY (col, id))")
        self._lock = threading.RLock()

    def get(self, col, id):
        with self._lock:
            r = self._c.execute("SELECT body FROM docs WHERE col=? AND id=?", (col, id)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, col, id, doc):
        with self._lock, self._c:
            self._c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?)", (col, id, json.dumps(doc)))

    def delete(self, col, id):
        with self._lock, self._c:
            self._c.execute("DELETE FROM docs WHERE col=? AND id=?", (col, id))

    def list(self, col, **equals):
        with self._lock:
            rows = self._c.execute("SELECT body FROM docs WHERE col=?", (col,)).fetchall()
        return [d for d in (json.loads(r[0]) for r in rows) if _match(d, equals)]

    def create(self, col, id, doc):
        with self._lock, self._c:
            cur = self._c.execute("INSERT OR IGNORE INTO docs VALUES (?,?,?)", (col, id, json.dumps(doc)))
            return cur.rowcount == 1


class FirestoreStore(Store):
    """Firestore adapter (requires `google-cloud-firestore`, imported lazily)."""

    def __init__(self, client=None) -> None:
        if client is None:
            from google.cloud import firestore  # type: ignore

            client = firestore.Client()
        self._db = client

    def get(self, col, id):
        s = self._db.collection(col).document(id).get()
        return s.to_dict() if s.exists else None

    def put(self, col, id, doc):
        self._db.collection(col).document(id).set(doc)

    def delete(self, col, id):
        self._db.collection(col).document(id).delete()

    def list(self, col, **equals):
        q = self._db.collection(col)
        for k, v in equals.items():
            q = q.where(k, "==", v)
        return [s.to_dict() for s in q.stream()]

    def create(self, col, id, doc):
        from google.api_core.exceptions import AlreadyExists  # type: ignore

        try:
            self._db.collection(col).document(id).create(doc)
            return True
        except AlreadyExists:
            return False


class ChunkingStore(Store):
    """Wraps any store whose documents have a size cap (Firestore: 1 MiB) by sharding large top-level strings."""

    MARK = "\x00chunked:"

    def __init__(self, inner: Store, max_value: int = 400_000) -> None:
        self.inner, self.max = inner, max_value

    def _pack(self, col: str, id: str, doc: Doc) -> Doc:
        out = dict(doc)
        for k, v in doc.items():
            if isinstance(v, str) and len(v) > self.max:
                parts = [v[i:i + self.max] for i in range(0, len(v), self.max)]
                for i, part in enumerate(parts):
                    self.inner.put(f"{col}__chunks", f"{id}.{k}.{i}", {"d": part})
                out[k] = f"{self.MARK}{len(parts)}:{id}"
        return out

    def _unpack(self, col: str, id: str, doc: Doc | None) -> Doc | None:
        if doc is None:
            return None
        for k, v in list(doc.items()):
            if isinstance(v, str) and v.startswith(self.MARK):
                n, _, owner = v[len(self.MARK):].partition(":")
                doc[k] = "".join(self.inner.get(f"{col}__chunks", f"{owner}.{k}.{i}")["d"] for i in range(int(n)))
        return doc

    def _drop(self, col: str, id: str) -> None:
        old = self.inner.get(col, id) or {}
        for k, v in old.items():
            if isinstance(v, str) and v.startswith(self.MARK):
                for i in range(int(v[len(self.MARK):].partition(":")[0])):
                    self.inner.delete(f"{col}__chunks", f"{id}.{k}.{i}")

    def get(self, col, id):
        return self._unpack(col, id, self.inner.get(col, id))

    def put(self, col, id, doc):
        self._drop(col, id)
        self.inner.put(col, id, self._pack(col, id, doc))

    def delete(self, col, id):
        self._drop(col, id)
        self.inner.delete(col, id)

    def list(self, col, **equals):
        return [self._unpack(col, "", d) for d in self.inner.list(col, **equals)]

    def create(self, col, id, doc):
        if self.inner.get(col, id) is not None:
            return False
        return self.inner.create(col, id, self._pack(col, id, doc))


class SQLStore(Store):
    """Portable relational store (SQLAlchemy): PostgreSQL/Supabase, Azure SQL / SQL Server, MySQL, SQLite.

    One table of JSON documents keyed by (col, id), so adding a collection never needs a migration.
    """

    def __init__(self, url: str) -> None:
        import sqlalchemy as sa  # imported lazily: only needed for this backend

        self._sa = sa
        self._e = sa.create_engine(url, pool_pre_ping=True)
        m = sa.MetaData()
        self._t = sa.Table("portal_docs", m, sa.Column("col", sa.String(64), primary_key=True),
                           sa.Column("id", sa.String(255), primary_key=True), sa.Column("body", sa.UnicodeText, nullable=False))
        m.create_all(self._e)

    def get(self, col, id):
        t = self._t
        with self._e.connect() as c:
            r = c.execute(self._sa.select(t.c.body).where(t.c.col == col, t.c.id == id)).first()
        return json.loads(r[0]) if r else None

    def put(self, col, id, doc):
        t, body = self._t, json.dumps(doc)
        with self._e.begin() as c:
            if c.execute(t.update().where(t.c.col == col, t.c.id == id).values(body=body)).rowcount == 0:
                try:
                    with c.begin_nested():
                        c.execute(t.insert().values(col=col, id=id, body=body))
                except self._sa.exc.IntegrityError:  # lost a race with another writer: update instead
                    c.execute(t.update().where(t.c.col == col, t.c.id == id).values(body=body))

    def create(self, col, id, doc):
        try:
            with self._e.begin() as c:
                c.execute(self._t.insert().values(col=col, id=id, body=json.dumps(doc)))
            return True
        except self._sa.exc.IntegrityError:
            return False

    def delete(self, col, id):
        t = self._t
        with self._e.begin() as c:
            c.execute(t.delete().where(t.c.col == col, t.c.id == id))

    def list(self, col, **equals):
        t = self._t
        with self._e.connect() as c:
            rows = c.execute(self._sa.select(t.c.body).where(t.c.col == col)).all()
        return [d for d in (json.loads(r[0]) for r in rows) if _match(d, equals)]


def open_store(spec: str) -> Store:
    """`memory` | `sqlite:<path>` | `sql:<sqlalchemy url>` (or `sql` + DATABASE_URL) | `firestore` | `pkg.module:factory`."""
    import os

    if spec == "memory":
        return MemoryStore()
    if spec.startswith("sqlite:"):
        return SQLiteStore(spec[7:] or ":memory:")
    if spec == "sql" or spec.startswith("sql:"):
        url = spec[4:] or os.environ.get("DATABASE_URL", "")
        if not url:
            raise ValueError("sql store needs a SQLAlchemy URL (sql:<url> or DATABASE_URL)")
        return SQLStore(url)
    if spec == "firestore":
        return ChunkingStore(FirestoreStore())
    if ":" in spec and "/" not in spec:  # custom backend: "my_pkg.module:factory" returning a Store
        import importlib

        mod, _, fn = spec.partition(":")
        store = getattr(importlib.import_module(mod), fn)()
        if not isinstance(store, Store):
            raise TypeError(f"{spec} did not return a Store")
        return store
    raise ValueError(f"unknown store {spec!r}")
