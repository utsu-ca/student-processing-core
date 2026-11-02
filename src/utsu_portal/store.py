"""Database-agnostic document store. Implement `Store` to add a backend."""
from __future__ import annotations

import json
import sqlite3
import random
import secrets
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

Doc = dict[str, Any]
REV = "_rev"  # optimistic-lock revision carried by every document written through create()/replace()/update()


class Conflict(Exception):
    """Another writer changed the document since it was read (optimistic lock failed)."""


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


    @abstractmethod
    def replace(self, col: str, id: str, doc: Doc, expected_rev: int) -> bool:
        """Compare-and-swap: write `doc` (with `_rev` = expected_rev + 1) only if the stored `_rev` is still
        `expected_rev` (a missing `_rev` counts as 0). Must be atomic; returns False on conflict or if the document is gone."""

    def update(self, col: str, id: str, fn: Callable[[Doc | None], Doc | None], retries: int = 10) -> Doc | None:
        """Optimistic read-modify-write. `fn` gets the current document (None if absent) and returns the new one, or None
        to leave it unchanged; it may raise to abort. It is re-run on a fresh read when another writer got in first, so it
        must be free of side effects. Raises `Conflict` if the retries are exhausted."""
        for attempt in range(retries):
            cur = self.get(col, id)
            new = fn(json.loads(json.dumps(cur)) if cur is not None else None)
            if new is None:
                return cur
            if cur is None:
                new = {**new, REV: 1}
                if self.create(col, id, new):
                    return new
            else:
                rev = int(cur.get(REV, 0))
                new = {**new, REV: rev + 1}
                if self.replace(col, id, new, rev):
                    return new
            time.sleep(min(0.002 * 2 ** attempt, 0.05) * random.random())
        raise Conflict(f"{col}/{id}: too much contention")


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

    def replace(self, col, id, doc, expected_rev):
        with self._lock:
            cur = self._d.get(col, {}).get(id)
            if cur is None or int(cur.get(REV, 0)) != expected_rev:
                return False
            self.put(col, id, {**doc, REV: expected_rev + 1})
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

    def replace(self, col, id, doc, expected_rev):
        with self._lock, self._c:  # the lock serialises threads; the body-equality guard protects against other processes
            r = self._c.execute("SELECT body FROM docs WHERE col=? AND id=?", (col, id)).fetchone()
            if not r or int(json.loads(r[0]).get(REV, 0)) != expected_rev:
                return False
            cur = self._c.execute("UPDATE docs SET body=? WHERE col=? AND id=? AND body=?",
                                  (json.dumps({**doc, REV: expected_rev + 1}), col, id, r[0]))
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

    def replace(self, col, id, doc, expected_rev):
        from google.cloud import firestore  # type: ignore

        ref = self._db.collection(col).document(id)

        @firestore.transactional
        def txn(t) -> bool:
            snap = ref.get(transaction=t)
            if not snap.exists or int((snap.to_dict() or {}).get(REV, 0)) != expected_rev:
                return False
            t.set(ref, {**doc, REV: expected_rev + 1})
            return True

        return txn(self._db.transaction())


class ChunkingStore(Store):
    """Wraps any store whose documents have a size cap (Firestore: 1 MiB) by sharding large top-level strings."""

    MARK = "\x00chunked:"

    def __init__(self, inner: Store, max_value: int = 400_000) -> None:
        self.inner, self.max = inner, max_value

    def _pack(self, col: str, id: str, doc: Doc) -> Doc:
        out, nonce = dict(doc), secrets.token_hex(4)  # unique chunk names per write: a losing writer can never clobber the winner's chunks
        for k, v in doc.items():
            if isinstance(v, str) and len(v) > self.max:
                owner = f"{id}~{nonce}"
                parts = [v[i:i + self.max] for i in range(0, len(v), self.max)]
                for i, part in enumerate(parts):
                    self.inner.put(f"{col}__chunks", f"{owner}.{k}.{i}", {"d": part})
                out[k] = f"{self.MARK}{len(parts)}:{owner}"
        return out

    def _unpack(self, col: str, id: str, doc: Doc | None) -> Doc | None:
        if doc is None:
            return None
        for k, v in list(doc.items()):
            if isinstance(v, str) and v.startswith(self.MARK):
                n, _, owner = v[len(self.MARK):].partition(":")
                doc[k] = "".join(self.inner.get(f"{col}__chunks", f"{owner}.{k}.{i}")["d"] for i in range(int(n)))
        return doc

    def _drop_doc(self, col: str, old: Doc | None) -> None:
        for k, v in (old or {}).items():
            if isinstance(v, str) and v.startswith(self.MARK):
                n, _, owner = v[len(self.MARK):].partition(":")
                for i in range(int(n)):
                    self.inner.delete(f"{col}__chunks", f"{owner}.{k}.{i}")

    def get(self, col, id):
        return self._unpack(col, id, self.inner.get(col, id))

    def put(self, col, id, doc):
        old = self.inner.get(col, id)
        self.inner.put(col, id, self._pack(col, id, doc))
        self._drop_doc(col, old)

    def delete(self, col, id):
        old = self.inner.get(col, id)
        self.inner.delete(col, id)
        self._drop_doc(col, old)

    def list(self, col, **equals):
        return [self._unpack(col, "", d) for d in self.inner.list(col, **equals)]

    def create(self, col, id, doc):
        if self.inner.get(col, id) is not None:
            return False
        packed = self._pack(col, id, doc)
        if self.inner.create(col, id, packed):
            return True
        self._drop_doc(col, packed)
        return False

    def replace(self, col, id, doc, expected_rev):
        old, packed = self.inner.get(col, id), self._pack(col, id, doc)
        if old is not None and self.inner.replace(col, id, packed, expected_rev):
            self._drop_doc(col, old)
            return True
        self._drop_doc(col, packed)
        return False


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

    def replace(self, col, id, doc, expected_rev):
        t = self._t
        with self._e.begin() as c:
            raw = c.execute(self._sa.select(t.c.body).where(t.c.col == col, t.c.id == id)).scalar()
            if raw is None or int(json.loads(raw).get(REV, 0)) != expected_rev:
                return False
            # the WHERE on the exact body we read makes this a compare-and-swap on every SQL dialect, with no extra column
            n = c.execute(t.update().where(t.c.col == col, t.c.id == id, t.c.body == raw)
                          .values(body=json.dumps({**doc, REV: expected_rev + 1}))).rowcount
            return n == 1

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
    from .settings import env_secret

    if spec == "memory":
        return MemoryStore()
    if spec.startswith("sqlite:"):
        return SQLiteStore(spec[7:] or ":memory:")
    if spec == "sql" or spec.startswith("sql:"):
        url = spec[4:] or env_secret("DATABASE_URL", "")
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
