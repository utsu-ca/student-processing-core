"""Declarative form definitions (YAML) : parsing, hashing, validation and sanitisation.

The same rules are implemented client-side in static/forms.js; the server re-validates every submission.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml

from .security import clean_text

FIELD_TYPES = {"text", "textarea", "email", "number", "date", "select", "radio", "multiselect", "checkbox", "info"}
MAX_FORM_BYTES = 200_000
_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELD_KEYS = {"id", "type", "label", "help", "required", "options", "pattern", "min", "max", "show_if", "default"}
_TOP_KEYS = {"id", "version", "title", "description", "fields"}


class FormError(ValueError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else errors
        super().__init__("; ".join(self.errors))


def schema_hash(schema: dict) -> str:
    """Hash of the canonical definition, independent of YAML formatting/key order."""
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def parse_yaml(text: str) -> dict:
    if len(text.encode()) > MAX_FORM_BYTES:
        raise FormError("form too large")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise FormError(f"invalid YAML: {e}") from e
    return validate_schema(raw)


def to_yaml(schema: dict) -> str:
    return yaml.safe_dump(schema, sort_keys=False, allow_unicode=True)


def validate_schema(raw: Any) -> dict:
    errs: list[str] = []
    if not isinstance(raw, dict):
        raise FormError("form must be a mapping")
    for k in raw:
        if k not in _TOP_KEYS:
            errs.append(f"unknown top-level key {k!r}")
    fid, ver = raw.get("id"), raw.get("version")
    if not isinstance(fid, str) or not _ID.match(fid):
        errs.append("id must match [a-z][a-z0-9_]{0,63}")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        errs.append("version must be a positive integer")
    if not isinstance(raw.get("title"), str) or not raw["title"].strip():
        errs.append("title is required")
    fields = raw.get("fields")
    if not isinstance(fields, list) or not fields:
        errs.append("fields must be a non-empty list")
        raise FormError(errs)
    seen: set[str] = set()
    out_fields = []
    for i, f in enumerate(fields):
        where = f"fields[{i}]"
        if not isinstance(f, dict):
            errs.append(f"{where} must be a mapping")
            continue
        for k in f:
            if k not in _FIELD_KEYS:
                errs.append(f"{where}: unknown key {k!r}")
        t, id_ = f.get("type"), f.get("id")
        if t not in FIELD_TYPES:
            errs.append(f"{where}: type must be one of {sorted(FIELD_TYPES)}")
        if not isinstance(id_, str) or not _ID.match(id_):
            errs.append(f"{where}: bad id")
        elif id_ in seen:
            errs.append(f"{where}: duplicate id {id_!r}")
        else:
            seen.add(id_)
        if not isinstance(f.get("label"), str):
            errs.append(f"{where}: label is required")
        if t in {"select", "radio", "multiselect"}:
            opts = f.get("options")
            if not (isinstance(opts, list) and opts and all(isinstance(o, str) and o for o in opts) and len(set(opts)) == len(opts)):
                errs.append(f"{where}: options must be a list of unique non-empty strings")
        if "pattern" in f:
            try:
                if len(f["pattern"]) > 200:
                    raise ValueError
                re.compile(f["pattern"])
            except (re.error, TypeError, ValueError):
                errs.append(f"{where}: invalid pattern")
        sif = f.get("show_if")
        if sif is not None:
            if not (isinstance(sif, dict) and isinstance(sif.get("field"), str) and "equals" in sif):
                errs.append(f"{where}: show_if needs field and equals")
            elif sif["field"] not in seen or sif["field"] == id_:
                errs.append(f"{where}: show_if must reference an earlier field")
        out_fields.append(f)
    if errs:
        raise FormError(errs)
    return json.loads(json.dumps({k: raw[k] for k in raw}))  # plain JSON-safe copy


def visible(field: dict, data: dict) -> bool:
    c = field.get("show_if")
    if not c:
        return True
    v = data.get(c["field"])
    return (c["equals"] in v) if isinstance(v, list) else v == c["equals"]


def validate_submission(schema: dict, data: Any) -> dict:
    """Return sanitised data for visible fields or raise FormError. Unknown fields are rejected."""
    if not isinstance(data, dict):
        raise FormError("data must be an object")
    known = {f["id"] for f in schema["fields"]}
    errs = [f"unknown field {k!r}" for k in data if k not in known]
    out: dict[str, Any] = {}
    for f in schema["fields"]:
        fid, t = f["id"], f["type"]
        if t == "info":
            continue
        if not visible(f, out):
            continue
        v = data.get(fid)
        empty = v is None or v == "" or v == [] or (t == "checkbox" and v is False)
        if empty:
            if f.get("required"):
                errs.append(f"{fid}: required")
            elif t == "checkbox":
                out[fid] = False
            continue
        try:
            out[fid] = _coerce(f, v)
        except FormError as e:
            errs.append(f"{fid}: {e}")
    if errs:
        raise FormError(errs)
    return out


def _coerce(f: dict, v: Any) -> Any:
    t = f["type"]
    if t in {"text", "textarea", "email", "date", "select", "radio"}:
        if not isinstance(v, str):
            raise FormError("must be text")
        s = clean_text(v, 5000 if t == "textarea" else 500, multiline=t == "textarea")
        if t in {"select", "radio"} and s not in f["options"]:
            raise FormError("not an allowed option")
        if t == "email" and not re.fullmatch(r"[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[^@\s<>]{2,}", s):
            raise FormError("invalid email")
        if t == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            raise FormError("date must be YYYY-MM-DD")
        if "min" in f and len(s) < f["min"] and t in {"text", "textarea"}:
            raise FormError(f"at least {f['min']} characters")
        if "max" in f and len(s) > f["max"] and t in {"text", "textarea"}:
            raise FormError(f"at most {f['max']} characters")
        if "pattern" in f and not re.fullmatch(f["pattern"], s):
            raise FormError("invalid format")
        return s
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FormError("must be a number")
        if "min" in f and v < f["min"] or "max" in f and v > f["max"]:
            raise FormError("out of range")
        return v
    if t == "multiselect":
        if not isinstance(v, list) or any(x not in f["options"] for x in v) or len(set(v)) != len(v):
            raise FormError("invalid selection")
        return v
    if t == "checkbox":
        if not isinstance(v, bool):
            raise FormError("must be true/false")
        return v
    raise FormError("unsupported type")
