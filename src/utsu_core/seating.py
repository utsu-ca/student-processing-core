import csv
import json
import logging
import random
from collections import Counter
from pathlib import Path

from utsu_core.senate import PREF_PREFIX
from utsu_std.csv_safe import SafeDictWriter, open_csv
from utsu_std.parsing_utils import division_code
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)

FACULTY = "Faculty/College"
# Best first. Anything else (None, blank) is never seated in that constituency; the only exception is the
# General Member catch-all seats.
TIERS = ["First-choice", "Second-choice", "Third-choice", "Interested"]
NOT_RANKED = "Not ranked"
GENERAL = "General Member"
_TIE_BITS = 32
_BIG = 1 << 200
_INF = 1 << 400


def tier_index(value: str) -> int | None:
    """
    >>> tier_index(" first-choice ")
    0
    >>> tier_index("Interested")
    3
    >>> tier_index("None") is None
    True
    """
    value = (value or "").strip().lower()
    for i, tier in enumerate(TIERS):
        if value == tier.lower():
            return i
    return None


def load_seats(seats_file: Path | str) -> dict[tuple[str, str], int]:
    """
    Load seats per constituency from a CSV with columns constituency,division,seats.
    Lines starting with '#' are ignored. Faculty/College rows need a division code; the others must not have one.
    """
    path = Path(absfile(seats_file))
    if not path.exists():
        raise FileNotFoundError(f"Seats file not found: {path}")

    with open_csv(path) as f:
        lines = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
    seats: dict[tuple[str, str], int] = {}
    for row in csv.DictReader(lines):
        constituency = (row.get("constituency") or "").strip()
        division = (row.get("division") or "").strip().upper()
        try:
            count = int((row.get("seats") or "").strip())
        except ValueError:
            raise ValueError(f"Invalid seat count for {constituency!r} {division!r}: {row.get('seats')!r}")
        if count < 0:
            raise ValueError(f"Negative seat count for {constituency!r} {division!r}")
        if constituency == FACULTY and not division:
            raise ValueError(f"{FACULTY} seats need a division code")
        if constituency != FACULTY and division:
            raise ValueError(f"Only {FACULTY} seats are per division; got division {division!r} for {constituency!r}")
        if (constituency, division) in seats:
            raise ValueError(f"Duplicate seats row for {constituency!r} {division!r}")
        seats[(constituency, division)] = count
    return seats


def load_applicants(verified_csv: Path | str) -> list[dict]:
    """Read verified.csv into applicant dicts with parsed preference tiers, sorted by uuid."""
    with open_csv(verified_csv) as f:
        applicants = []
        for row in csv.DictReader(f):
            prefs = {k[len(PREF_PREFIX):]: tier_index(v) for k, v in row.items() if k.startswith(PREF_PREFIX)}
            applicants.append({
                "uuid": row["uuid"],
                "full_name": row.get("full_name", ""),
                "division_code": division_code(row.get("division") or ""),
                "delegated": (row.get("delegated") or "").strip().lower() == "yes",
                "prefs": {c: t for c, t in prefs.items() if t is not None},
            })
    return sorted(applicants, key=lambda a: a["uuid"])


def _min_cost_assignment(cost: list[list[int]]) -> list[int]:
    """
    Hungarian algorithm on an n x m integer matrix (n <= m). Returns the column chosen by each row.
    Integers are exact, so strict-priority weights never lose precision.
    """
    n, m = len(cost), len(cost[0])
    u, v = [0] * (n + 1), [0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [_INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], _INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    chosen = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            chosen[p[j] - 1] = j - 1
    return chosen


def _fill(pool: list[dict], slots: list[tuple[str, str]], tier_of, rng: random.Random):
    """
    Match applicants to seats, maximizing First-choices, then Second, then Third, then Interested, then
    anything else (tier == len(TIERS)). tier_of(applicant, constituency, division) is None when the applicant
    is not eligible for that seat. Returns (applicant, slot, tier) triples.
    """
    if not slots or not pool:
        return []
    base = len(slots) + 1
    scale = len(slots) << _TIE_BITS
    cost = []
    for constituency, division in slots:
        row = []
        for a in pool:
            tier = tier_of(a, constituency, division)
            if tier is None:
                row.append(_BIG)
            else:
                row.append(-(base ** (len(TIERS) - tier) * scale + rng.getrandbits(_TIE_BITS)))
        row.extend([0] * len(slots))
        cost.append(row)

    placed = []
    for slot_index, col in enumerate(_min_cost_assignment(cost)):
        if col < len(pool) and cost[slot_index][col] != _BIG:
            a, slot = pool[col], slots[slot_index]
            placed.append((a, slot, tier_of(a, *slot)))
    return placed


def assign_seats(applicants: list[dict], seats: dict[tuple[str, str], int], seed: int) -> dict:
    """
    Seat applicants, everyone at most once, in this order of priority:
      1. Delegates take the first seats of their division's Faculty/College constituency; when a division has
         more delegates than seats, the seeded RNG draws who gets them and the rest join the general pool.
      2. Ranked: the remaining seats (except General Member) are filled to maximize First-choices, then
         Second, then Third, then Interested; nobody is placed in a constituency they marked None/blank.
      3. Faculty/College seats still empty go to unseated applicants of that division who ranked General
         Member, best General Member tier first.
      4. General Member seats are the catch-all for everyone still unseated, those who ranked General
         Member first (seats labeled NOT_RANKED when they did not).
    Ties are broken by a seeded RNG, so the same inputs and seed always give the same result.
    """
    rng = random.Random(seed)
    applicants = sorted(applicants, key=lambda a: a["uuid"])
    seated: list[dict] = []
    unseated: dict[str, str] = {}
    remaining = dict(seats)

    delegates_by_division: dict[str, list[dict]] = {}
    for a in applicants:
        if a["delegated"]:
            delegates_by_division.setdefault(a["division_code"], []).append(a)

    overflow: list[dict] = []
    for division in sorted(delegates_by_division):
        group = list(delegates_by_division[division])
        capacity = remaining.get((FACULTY, division), 0)
        rng.shuffle(group)
        for a in group[:capacity]:
            seated.append({"uuid": a["uuid"], "full_name": a["full_name"], "constituency": FACULTY,
                           "division": division, "seat_type": "delegate", "preference": ""})
        for a in group[capacity:]:
            logger.warning(f"Delegate {a['uuid']} not drawn for the {capacity} {FACULTY} seat(s) of division "
                           f"{division!r}; moved to the general pool")
        overflow.extend(group[capacity:])
        remaining[(FACULTY, division)] = max(0, capacity - len(group))

    pool = [a for a in applicants if not a["delegated"]] + overflow

    def place(slot_keys: list[tuple[str, str]], tier_of, seat_type: str, label):
        """Fill the given seats from the pool, then drop the seated applicants from the pool."""
        nonlocal pool
        slots = [key for key in sorted(slot_keys) for _ in range(remaining[key])]
        for a, (constituency, division), tier in _fill(pool, slots, tier_of, rng):
            seated.append({"uuid": a["uuid"], "full_name": a["full_name"], "constituency": constituency,
                           "division": division, "seat_type": seat_type, "preference": label(a, tier)})
            remaining[(constituency, division)] -= 1
            pool = [p for p in pool if p["uuid"] != a["uuid"]]

    # 2. Ranked: everyone gets the best seat they ranked (General Member is held back for the last two steps).
    ranked_slots = [key for key in remaining if key[0] != GENERAL]
    place(ranked_slots,
          lambda a, c, d: a["prefs"].get(c) if c != FACULTY or a["division_code"] == d else None,
          "ranked", lambda a, tier: TIERS[tier])

    # 3. Leftover Faculty/College seats go to applicants of that division who applied as General Members.
    place([key for key in remaining if key[0] == FACULTY],
          lambda a, c, d: a["prefs"].get(GENERAL) if a["division_code"] == d else None,
          "general_member_division", lambda a, tier: TIERS[tier])

    # 4. General Member seats are the catch-all: anyone still unseated, ranked General Members first.
    place([key for key in remaining if key[0] == GENERAL],
          lambda a, c, d: a["prefs"].get(GENERAL, len(TIERS)),
          "general_member", lambda a, tier: TIERS[tier] if tier < len(TIERS) else NOT_RANKED)

    seated_ids = {s["uuid"] for s in seated}
    for a in applicants:
        if a["uuid"] not in seated_ids and a["uuid"] not in unseated:
            unseated[a["uuid"]] = "no seat left in any constituency they were eligible for"
    names = {a["uuid"]: a["full_name"] for a in applicants}

    filled = Counter((s["constituency"], s["division"]) for s in seated)
    unfilled = [{"constituency": c, "division": d, "unfilled": n - filled[(c, d)]}
                for (c, d), n in sorted(seats.items()) if n - filled[(c, d)] > 0]
    seated.sort(key=lambda s: (s["constituency"], s["division"], s["seat_type"], s["uuid"]))
    return {
        "seated": seated,
        "unseated": [{"uuid": u, "full_name": names[u], "reason": r} for u, r in sorted(unseated.items())],
        "unfilled_seats": unfilled,
        "seed": seed,
    }


def run_seating(verified_csv: Path | str, seats_file: Path | str, output_dir: Path | str, seed: int) -> dict:
    """Seat the verified applicants and write seats.csv, unseated.csv and seating_run.json to output_dir."""
    seats = load_seats(seats_file)
    applicants = load_applicants(verified_csv)

    known = {c for a in applicants for c in a["prefs"]} | {FACULTY}
    for constituency, _ in seats if applicants else ():
        if constituency not in known:
            logger.warning(f"Seats file lists {constituency!r}, which no applicant ranked or the form doesn't have")

    result = assign_seats(applicants, seats, seed)

    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in (
        ("seats.csv", ["uuid", "full_name", "constituency", "division", "seat_type", "preference"], result["seated"]),
        ("unseated.csv", ["uuid", "full_name", "reason"], result["unseated"]),
    ):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            writer = SafeDictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    summary = {
        "seed": seed,
        "seats_file": str(seats_file),
        "applicants": len(applicants),
        "seated": len(result["seated"]),
        "unseated": len(result["unseated"]),
        "by_preference": dict(Counter(s["preference"] or "delegate" for s in result["seated"])),
        "unfilled_seats": result["unfilled_seats"],
    }
    (out / "seating_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"Seating (seed={seed}): {summary['seated']} seated, {summary['unseated']} unseated, "
                f"by preference {summary['by_preference']}")
    for gap in result["unfilled_seats"]:
        logger.warning(f"Unfilled seat(s): {gap}")
    return result
