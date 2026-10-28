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


def open_store(spec: str) -> Store:
    if spec == "memory":
        return MemoryStore()
    if spec.startswith("sqlite:"):
        return SQLiteStore(spec[7:] or ":memory:")
    if spec == "firestore":
        return FirestoreStore()
    raise ValueError(f"unknown store {spec!r}")
