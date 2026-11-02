"""Append-only, hash-chained audit log with compressed, encrypted change snapshots and Wikipedia-style diffs."""
from __future__ import annotations

import base64
import difflib
import gzip
import hashlib
import hmac
import json
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


class AuditLog:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def _mac(self, prev: str, doc: dict) -> str:
        body = json.dumps({k: doc[k] for k in sorted(doc) if k not in ("hash", "blob")}, sort_keys=True, separators=(",", ":"))
        blob = hashlib.sha256((doc.get("blob") or "").encode()).hexdigest()
        return hmac.new(self.ctx.settings.secret.encode(), f"{prev}|{body}|{blob}".encode(), hashlib.sha256).hexdigest()

    def record(self, actor: str | None, action: str, target: str = "", before: Any = None, after: Any = None, meta: dict | None = None) -> dict:
        """Append an entry. `meta` must hold only non-sensitive context; before/after snapshots are redacted, compressed and encrypted."""
        S = self.ctx.store
        doc = {"ts": self.ctx.now(), "actor": actor or "anonymous", "action": action[:100], "target": str(target)[:200],
               "meta": redact(meta or {}), "blob": ""}
        if before is not None or after is not None:
            blob = pack({"before": redact(before), "after": redact(after)})
            if len(blob) > MAX_SNAPSHOT:
                blob, doc["meta"]["truncated"] = pack({"before": "[too large]", "after": "[too large]"}), True
            doc["blob"] = blob
        head = S.get("audit_head", "head") or {"seq": 0}
        seq = head["seq"]
        while True:  # claim the next sequence number atomically via create(); retry if another replica won it
            seq += 1
            prev = (S.get("audit", f"{seq - 1:012d}") or {}).get("hash", "") if seq > 1 else ""
            d = {**doc, "id": f"{seq:012d}", "seq": seq, "prev": prev}
            d["hash"] = self._mac(prev, d)
            if S.create("audit", d["id"], d):
                break
        S.put("audit_head", "head", {"id": "head", "seq": seq})
        return d

    def entries(self) -> list[dict]:
        return sorted(self.ctx.store.list("audit"), key=lambda d: d["seq"])

    def verify(self) -> dict:
        prev, n = "", 0
        for d in self.entries():
            n += 1
            if d["seq"] != n or d["prev"] != prev or not hmac.compare_digest(d["hash"], self._mac(prev, d)):
                return {"ok": False, "checked": n, "broken_at": d["seq"]}
            prev = d["hash"]
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
