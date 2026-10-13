"""
Weighted Financial Need Index (WFNI) for the SABP living-costs questionnaire.

The answer weights live in data/wfni_weights.csv (see the comments there) and are read again whenever that file
changes, so they can be adjusted between runs without touching the code.
"""
import csv
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from utsu_std.parsing_utils import split_multi
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)

WEIGHTS_FILE = "data/wfni_weights.csv"
QUESTIONS = ("disability_mobility", "disability_invisible", "living_situation", "disposable_income", "liquidity",
             "dependents", "external_supports")
CIRCUMSTANCES = "circumstances"
CIRCUMSTANCE_SETTINGS = ("per_answer", "cap")


class WeightsError(ValueError):
    pass


def _norm(value: str) -> str:
    """
    Option text as compared: no spaces or case, and "to" written as "-".

    >>> _norm("$0 to $500")
    '$0-$500'
    >>> _norm("  Stably  Housed ")
    'stablyhoused'
    """
    return re.sub(r"\s+", "", (value or "").lower().replace(" to ", "-"))


@dataclass(frozen=True)
class Weights:
    tables: dict[str, dict[str, int]]
    circumstance_points: int
    circumstance_cap: int
    sha256: str

    @property
    def max_score(self) -> int:
        return sum(max(table.values()) for table in self.tables.values()) + self.circumstance_cap

    @property
    def high_threshold(self) -> int:
        """Policy: "trust but verify when point totals represent a majority of the point scale"."""
        return self.max_score // 2 + 1


def parse_weights(text: str, source: str = WEIGHTS_FILE) -> Weights:
    """
    >>> w = parse_weights("field,answer,points\\n" + "\\n".join(f"{q},Yes,2" for q in QUESTIONS)
    ...                   + "\\ncircumstances,per_answer,3\\ncircumstances,cap,10")
    >>> (w.max_score, w.high_threshold)
    (24, 13)
    """
    lines = [line for line in text.lstrip("\ufeff").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    rows = [r for r in csv.reader(lines) if any(c.strip() for c in r)]
    if not rows or [c.strip().lower() for c in rows[0][:3]] != ["field", "answer", "points"]:
        raise WeightsError(f"{source}: the header must be 'field,answer,points'")

    tables: dict[str, dict[str, int]] = {q: {} for q in QUESTIONS}
    settings: dict[str, int] = {}
    for row in rows[1:]:
        if len(row) < 3:
            raise WeightsError(f"{source}: expected field,answer,points but got {row}")
        field, answer, raw = row[0].strip(), row[1].strip(), row[2].strip()
        try:
            points = int(raw)
        except ValueError:
            raise WeightsError(f"{source}: points for '{field}' / '{answer}' must be a whole number, got {raw!r}")
        if field == CIRCUMSTANCES:
            if answer not in CIRCUMSTANCE_SETTINGS or answer in settings:
                raise WeightsError(f"{source}: circumstances needs one 'per_answer' and one 'cap' row (got '{answer}')")
            settings[answer] = points
        elif field in tables:
            if not answer or _norm(answer) in tables[field]:
                raise WeightsError(f"{source}: blank or duplicate answer for '{field}': {answer!r}")
            tables[field][_norm(answer)] = points
        else:
            raise WeightsError(f"{source}: unknown field '{field}' (expected one of {', '.join(QUESTIONS)}, "
                               f"{CIRCUMSTANCES})")

    missing = [q for q, table in tables.items() if not table]
    if set(settings) != set(CIRCUMSTANCE_SETTINGS):
        missing.append(f"{CIRCUMSTANCES} (needs {' and '.join(CIRCUMSTANCE_SETTINGS)})")
    if missing:
        raise WeightsError(f"{source}: missing rows for {', '.join(missing)}")
    if settings["cap"] < 0 or settings["per_answer"] < 0:
        raise WeightsError(f"{source}: circumstance points and cap cannot be negative")
    return Weights(tables, settings["per_answer"], settings["cap"], hashlib.sha256(text.encode("utf-8")).hexdigest())


_cache: dict[Path, tuple[tuple[int, int], Weights]] = {}


def current_weights(path: Path | str | None = None) -> Weights:
    """The weights in the file, re-read whenever the file has changed since the last call."""
    path = Path(absfile(path or WEIGHTS_FILE))
    if not path.is_file():
        raise FileNotFoundError(f"WFNI weights file not found: {path}")
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    if path not in _cache or _cache[path][0] != stamp:
        weights = parse_weights(path.read_text(encoding="utf-8-sig"), str(path))
        if path in _cache:
            logger.info(f"WFNI weights changed; reloaded {path} ({weights.sha256[:12]})")
        _cache[path] = (stamp, weights)
    return _cache[path][1]


def points_for(table: dict[str, int], value: str) -> int | None:
    """Points for an answer, or None when it is not a known option (the long 'negative amount' option matches by prefix)."""
    key = _norm(value)
    if key in table:
        return table[key]
    return next((p for k, p in table.items() if key.startswith(k)), None)


def compute_wfni(app: dict[str, str], weights: Weights | None = None) -> tuple[int | None, list[str]]:
    """
    Weighted Financial Need Index from the questionnaire. Only the living-costs stream answers it, so other
    applications get None. Returns (score, names of answers that were blank or not a known option).

    >>> compute_wfni({"disability_mobility": "Yes", "disability_invisible": "No", "living_situation": "Stably Housed",
    ...               "disposable_income": "Less than $100", "liquidity": "$0 to $500", "dependents": "1",
    ...               "external_supports": "No", "circumstances": "Racial Violence;Recent estrangement"})
    (17, [])
    """
    if not any(app.get(k) for k in ("living_situation", "disposable_income", "liquidity")):
        return None, []
    weights = weights or current_weights()
    total, unknown = 0, []
    for field in QUESTIONS:
        points = points_for(weights.tables[field], app.get(field, ""))
        if points is None:
            unknown.append(field)
        else:
            total += points
    circumstances = [c for c in split_multi(app.get(CIRCUMSTANCES, "")) if c.lower() not in ("n/a", "na", "none")]
    total += min(weights.circumstance_cap, weights.circumstance_points * len(circumstances))
    return total, unknown
