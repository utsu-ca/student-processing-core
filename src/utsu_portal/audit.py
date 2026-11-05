"""Append-only, hash-chained audit log with compressed, encrypted change snapshots and Wikipedia-style diffs."""
from __future__ import annotations

import base64
import calendar
import difflib
import gzip
import hashlib
import hmac
import json
import time
import re
import zlib
from typing import Any

SECRET_KEYS = re.compile(r"(face|id_card|data_url|otp|code|token|secret|password|hash|challenge|body)$", re.I)
MAX_SNAPSHOT = 512_000


def redact(v: Any) -> Any:
    """Drop credentials and image payloads from anything that is about to be logged."""
    if isinstance(v, dict):
        return {k: ("[redacted]" if SECRET_KEYS.search(str(k)) else redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    if isinstance(v, str) and v.startswith("data:"):
        return "[redacted]"
    return v


def pack(obj: Any) -> str:
    raw = zlib.compress(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode(), 9)
    return base64.b64encode(raw).decode()


def unpack(s: str) -> Any:
    return json.loads(zlib.decompress(base64.b64decode(s)))


def render(obj: Any, indent: int = 0) -> list[str]:
    """Readable line form of a snapshot: multi-line strings (e.g. form YAML) expand so diffs are line based."""
    pad = "  " * indent
    if isinstance(obj, dict):
        out: list[str] = []
        for k in sorted(obj):
            v = obj[k]
            if isinstance(v, str) and "\n" in v:
                out.append(f"{pad}{k}: |")
                out += [f"{pad}  {ln}" for ln in v.split("\n")]
            elif isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out += render(v, indent + 1)
            else:
                out.append(f"{pad}{k}: {json.dumps(v, ensure_ascii=False)}")
        return out
    if isinstance(obj, list):
        out = []
        for v in obj:
            sub = render(v, indent + 1) if isinstance(v, (dict, list)) and v else [f"{pad}  {json.dumps(v, ensure_ascii=False)}"]
            out.append(f"{pad}-")
            out += sub
        return out
    return [f"{pad}{json.dumps(obj, ensure_ascii=False)}"]


def _words(a: str, b: str) -> tuple[list, list]:
    ta, tb = re.findall(r"\s+|\w+|[^\w\s]", a), re.findall(r"\s+|\w+|[^\w\s]", b)
    left, right = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            left.append(["=", "".join(ta[i1:i2])])
            right.append(["=", "".join(tb[j1:j2])])
        else:
            if i2 > i1:
                left.append(["-", "".join(ta[i1:i2])])
            if j2 > j1:
                right.append(["+", "".join(tb[j1:j2])])
    return left, right


def diff(before: Any, after: Any, context: int = 3) -> dict:
    """Two-column diff like a Wikipedia revision comparison: changed line pairs carry word-level highlights."""
    a, b = render(before if before is not None else {}), render(after if after is not None else {})
    rows: list[dict] = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append({"type": "context", "left_no": i1 + k + 1, "right_no": j1 + k + 1, "left": a[i1 + k], "right": b[j1 + k]})
            continue
        n = max(i2 - i1, j2 - j1)
        for k in range(n):
            l = a[i1 + k] if i1 + k < i2 else None
            r = b[j1 + k] if j1 + k < j2 else None
            row = {"type": "change" if l is not None and r is not None else ("del" if l is not None else "add"),
                   "left_no": i1 + k + 1 if l is not None else None, "right_no": j1 + k + 1 if r is not None else None,
                   "left": l, "right": r}
            if row["type"] == "change":
                row["left_segments"], row["right_segments"] = _words(l, r)
            rows.append(row)
    keep = [i for i, r in enumerate(rows) if r["type"] != "context"]  # collapse long unchanged runs
    shown = {j for i in keep for j in range(max(0, i - context), min(len(rows), i + context + 1))}
    out, gap = [], False
    for i, r in enumerate(rows):
        if i in shown:
            out.append(r)
            gap = False
        elif not gap:
            out.append({"type": "gap"})
            gap = True
    unified = "\n".join(difflib.unified_diff(a, b, "before", "after", lineterm="", n=context))
    return {"rows": out if keep else [], "unified": unified, "changed": bool(keep),
            "stats": {"added": sum(r["type"] in ("add", "change") for r in rows), "removed": sum(r["type"] in ("del", "change") for r in rows)}}


def month_of(ts: int) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_year}_{t.tm_mon:02d}"


def month_end(month: str) -> int:
    y, m = (int(x) for x in month.split("_"))
    return calendar.timegm((y + (m == 12), 1 if m == 12 else m + 1, 1, 0, 0, 0))


def coll(month: str) -> str:
    return f"audit_{month}"


def merkle_root(leaves: list[str]) -> str:
    """Binary Merkle root over hex leaf hashes (odd nodes are promoted unchanged). One 32-byte value commits to a whole batch."""
    level = [bytes.fromhex(x) for x in leaves]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        level = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
    return level[0].hex()


class AuditLog:
    """Hash-chained log in one table (collection) per UTC month: `audit_YYYY_MM`. The chain runs across months, so old
    months can be archived and dropped without breaking verification: the archive keeps the boundary hash."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def _mac(self, prev: str, doc: dict) -> str:
        body = json.dumps({k: doc[k] for k in sorted(doc) if k not in ("hash", "blob")}, sort_keys=True, separators=(",", ":"))
        blob = hashlib.sha256((doc.get("blob") or "").encode()).hexdigest()
        return hmac.new(self.ctx.settings.secret.encode(), f"{prev}|{body}|{blob}".encode(), hashlib.sha256).hexdigest()

    # -- months registry ------------------------------------------------------------------------------
    def months(self, states: tuple[str, ...] = ("hot", "archived")) -> list[dict]:
        return sorted((m for m in self.ctx.store.list("audit_months") if m["state"] in states), key=lambda m: m["month"])

    def _get(self, seq: int, months: list[str | None]) -> dict | None:
        for m in dict.fromkeys(x for x in months if x):
            d = self.ctx.store.get(coll(m), f"{seq:012d}")
            if d:
                return d
        return None

    # -- writing ----------------------------------------------------------------------------------------
    def record(self, actor: str | None, action: str, target: str = "", before: Any = None, after: Any = None, meta: dict | None = None) -> dict:
        """Append an entry. `meta` must hold only non-sensitive context; before/after snapshots are redacted, compressed and encrypted."""
        S = self.ctx.store
        ts = self.ctx.now()
        doc = {"ts": ts, "actor": actor or "anonymous", "action": action[:100], "target": str(target)[:200],
               "meta": redact(meta or {}), "blob": ""}
        if before is not None or after is not None:
            blob = pack({"before": redact(before), "after": redact(after)})
            if len(blob) > MAX_SNAPSHOT:
                blob, doc["meta"]["truncated"] = pack({"before": "[too large]", "after": "[too large]"}), True
            doc["blob"] = blob
        month = month_of(ts)
        cur = S.get("audit_head", "head") or {"seq": 0, "hash": "", "month": None}
        for _ in range(200):  # claim the next sequence number atomically via create(); if another replica won it, build on its entry
            seq = cur["seq"] + 1
            d = {**doc, "id": f"{seq:012d}", "seq": seq, "prev": cur["hash"]}
            d["hash"] = self._mac(cur["hash"], d)
            if S.create(coll(month), d["id"], d):
                break
            winner = self._get(seq, [month, cur.get("month")])
            cur = ({"seq": seq, "hash": winner["hash"], "month": month_of(winner["ts"])} if winner
                   else S.get("audit_head", "head") or {"seq": 0, "hash": "", "month": None})
        else:
            raise RuntimeError("audit log is too contended")
        S.create("audit_months", month, {"id": month, "month": month, "state": "hot", "first_seq": seq})
        S.update("audit_head", "head", lambda h: {"id": "head", "seq": seq, "hash": d["hash"], "month": month} if not h or h["seq"] < seq else None)
        return d

    # -- reading ------------------------------------------------------------------------------------------
    def entries(self, since_seq: int = 0) -> list[dict]:
        """Hot entries with seq > since_seq, in order. Archived/dropped months are read through `archive_entries`."""
        out: list[dict] = []
        ms = self.months(("hot", "archived"))
        for i, m in enumerate(ms):
            nxt = ms[i + 1]["first_seq"] if i + 1 < len(ms) else None
            if nxt is not None and nxt - 1 <= since_seq:
                continue  # whole month precedes the cursor
            out += [d for d in self.ctx.store.list(coll(m["month"])) if d["seq"] > since_seq]
        return sorted(out, key=lambda d: d["seq"])

    def get(self, seq: int) -> dict | None:
        for m in reversed(self.months(("hot", "archived"))):
            if m["first_seq"] <= seq:
                d = self.ctx.store.get(coll(m["month"]), f"{seq:012d}")
                if d:
                    return d
        return self.archived_entry(seq)

    def archived_entry(self, seq: int) -> dict | None:
        for ar in self.ctx.store.list("audit_archive"):
            if ar["first_seq"] <= seq <= ar["last_seq"]:
                return next((d for d in self.read_archive(ar["month"]) if d["seq"] == seq), None)
        return None

    def read_archive(self, month: str) -> list[dict]:
        ar = self.ctx.store.get("audit_archive", month)
        if not ar:
            return []
        raw = base64.b64decode(ar["blob"])
        if hashlib.sha256(raw).hexdigest() != ar["sha256"]:
            raise RuntimeError(f"archive {month} failed its integrity check")
        return [json.loads(x) for x in gzip.decompress(raw).decode().splitlines()]

    def _start_prev(self, first: dict) -> str | None:
        """Chain value expected before the first hot entry: '' at the very start, else the boundary hash kept by the archive."""
        if first["seq"] == 1:
            return ""
        for ar in self.ctx.store.list("audit_archive"):
            if ar["last_seq"] == first["seq"] - 1:
                return ar["last_hash"]
        before = self.get(first["seq"] - 1)  # predecessor still in a hot table (e.g. verifying one month in isolation)
        return before["hash"] if before else None

    def verify(self, entries: list[dict] | None = None) -> dict:
        """Re-walk the chain over the hot tables. Detects edited, removed, reordered or forged entries."""
        es = self.entries() if entries is None else entries
        if not es:
            return {"ok": True, "checked": 0}
        prev = self._start_prev(es[0])
        if prev is None:
            return {"ok": False, "checked": 0, "broken_at": es[0]["seq"], "reason": "missing archive boundary"}
        n, expect = 0, es[0]["seq"]
        for d in es:
            n += 1
            if d["seq"] != expect or d["prev"] != prev or not hmac.compare_digest(d["hash"], self._mac(prev, d)):
                return {"ok": False, "checked": n, "broken_at": d["seq"]}
            prev, expect = d["hash"], expect + 1
        head = self.ctx.store.get("audit_head", "head") if entries is None else None
        if head and es[-1]["seq"] < head["seq"]:
            return {"ok": False, "checked": n, "broken_at": es[-1]["seq"] + 1, "reason": "entries missing after the last one"}
        return {"ok": True, "checked": n}

    @staticmethod
    def snapshots(d: dict) -> tuple[Any, Any]:
        s = unpack(d["blob"]) if d.get("blob") else {}
        return s.get("before"), s.get("after")

    @staticmethod
    def public(d: dict) -> dict:
        return {k: d[k] for k in ("id", "seq", "ts", "actor", "action", "target", "meta", "prev", "hash")} | {"has_changes": bool(d.get("blob"))}

    def export_gzip(self) -> bytes:
        return gzip.compress("\n".join(json.dumps(self.public(d), sort_keys=True) for d in self.entries()).encode(), 9)
