import csv
import logging
import re
import sqlite3
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from utsu_std.csv_safe import open_csv
from utsu_std.parsing_utils import division_code, get_trunc_id
from utsu_std.utils import clean_text, normalize_case

logger = logging.getLogger(__name__)


METHOD_NAME = "name only"
METHOD_FUZZY_NAME = "fuzzy name"
FUZZY_FLAG = "fuzzy_name_match"
_CHUNK = 300
_SQL_VARIABLES = 500
MAX_CANDIDATES = 3
# how alike two first names have to be (0 to 1, `difflib` ratio) to be taken as the same name
FUZZY_THRESHOLD = 0.85


def _name_parts(first_name: str) -> list[str]:
    return [part for part in re.split(r"[\s-]+", first_name.lower()) if part]


def _part_similarity(a: str, b: str) -> float:
    if len(a) == 1 or len(b) == 1:  # an initial stands for the whole name
        return 1.0 if a[0] == b[0] else 0.0
    return SequenceMatcher(None, a, b).ratio()


def first_name_similarity(given: str, roster: str) -> float:
    """
    How alike a first name written on a form and a roster first name are, from 0 to 1; 0 when they are not the same
    name (below `FUZZY_THRESHOLD`). Students often leave out their middle names, so the first names only have to
    agree on their first part (allowing a small typo); when both have more parts, each part of the shorter name
    must be found, as a whole or as an initial, in the longer one. The score is that of the weakest part compared.

    >>> [first_name_similarity(*n) for n in (("Ann", "Ann Marie"), ("Ann Marie", "Ann"), ("Ann M", "Ann Marie"))]
    [1.0, 1.0, 1.0]
    >>> round(first_name_similarity("Jon", "John"), 2), first_name_similarity("Mary-Jane", "Mary Jane")
    (0.86, 1.0)
    >>> [first_name_similarity(*n) for n in (("Ann Lynn", "Ann Marie"), ("Bo", "Ann"), ("", "Ann"))]
    [0.0, 0.0, 0.0]
    """
    a, b = _name_parts(given), _name_parts(roster)
    if not a or not b:
        return 0.0
    shorter, longer = sorted((a[1:], b[1:]), key=len)
    scores = [_part_similarity(a[0], b[0])]
    for part in shorter:
        scores.append(max(_part_similarity(part, other) for other in longer) if longer else 0.0)
    score = min(scores)
    return round(score, 2) if score >= FUZZY_THRESHOLD else 0.0


@dataclass(frozen=True)
class Candidate:
    """A roster student whose name is close to a name that was not found exactly."""
    uoft_uuid: str
    full_name: str
    student_number: str  # truncated, as on the roster: xxxxx3971x
    division: str
    similarity: float


@dataclass(frozen=True)
class Match:
    """
    The outcome of checking one person against uoft_data. `similarity` is set only when the first name was matched
    by `first_name_similarity` rather than exactly; `candidates` are then the closest roster students (best first),
    which include the one matched, and are also given when the fuzzy match could not be decided.
    """
    uoft_uuid: str | None
    level: int | None
    method: str
    reason: str
    similarity: float | None = None
    candidates: tuple[Candidate, ...] = ()

    @property
    def verified(self) -> bool:
        return self.uoft_uuid is not None

    @property
    def fuzzy_flag(self) -> str:
        """The review flag for a fuzzy verification, `fuzzy_name_match=0.86`; empty for any other outcome."""
        return f"{FUZZY_FLAG}={self.similarity:.2f}" if self.verified and self.similarity is not None else ""


def division_of(text: str) -> str:
    """
    The division code of a form or roster value: `UC - University College` is `UC`.

    >>> division_of(" uc - University College "), division_of(""), division_of(None)
    ('UC', '', '')
    """
    return division_code(clean_text(text))


def decide(name: str, number: str, division: str, by_name: list[tuple[str, int, str]],
           similarity: dict[str, float] | None = None) -> Match:
    """
    Decide one person from the roster rows sharing their name (each a (uoft_data.uuid, trunc_id, division code)
    tuple). A name is always needed: the roster only has the last four digits of a student number, which many
    students share, so a number or a division never identifies anyone alone. They only narrow down the people
    with that name. Every value given has to match the same student, and a wrong value never falls back to the
    others:
      name only                          the name belongs to exactly one student            level 1
      name + student number              both belong to the same student                    level 2
      name + division                    exactly one student of that name is in the division level 2
      name + student number + division   all three belong to the same student              level 3
    A roster student's division is their college or division, or their faculty when they have none (see
    `match_students`); a student with neither never matches a given division.

    `similarity` is given when the rows are not an exact name match but students whose last name agrees and whose
    first name is close (see `first_name_similarity`; it maps each row's uuid to its score). The factors are the
    same, but the name factor is `fuzzy name` and the match carries the similarity of the student it settled on.

    >>> decide("Ann Lee", "1001231617", "", [("u1", 3161, "UC")])
    Match(uoft_uuid='u1', level=2, method='name + student number', reason='', similarity=None, candidates=())
    >>> decide("Ann Lee", "1001231617", "UC", [("u1", 3161, "UC")]).level
    3
    >>> decide("Ann Lee", "", "", [("u1", 3161, "UC")])
    Match(uoft_uuid='u1', level=1, method='name only', reason='', similarity=None, candidates=())
    >>> decide("Sam Lee", "", "TRIN", [("u1", 1, "UC"), ("u2", 2, "TRIN")])
    Match(uoft_uuid='u2', level=2, method='name + division', reason='', similarity=None, candidates=())
    >>> decide("Ann Lee", "1001231617", "", [("u1", 3161, "UC")], {"u1": 0.92})
    Match(uoft_uuid='u1', level=2, method='fuzzy name + student number', reason='', similarity=0.92, candidates=())
    >>> decide("Ann Lee", "", "TRIN", [("u1", 3161, "UC")]).reason
    'division does not match'
    >>> decide("Ann Lee", "1001231617", "TRIN", [("u1", 3161, "UC")]).reason
    'division does not match'
    >>> decide("Ann Lee", "1009999999", "", [("u1", 3161, "UC")]).reason
    'student number does not match'
    >>> decide("", "1001231617", "UC", []).reason
    'no name given'
    """
    if not name:
        return Match(None, None, "", "no name given")
    if not by_name:
        return Match(None, None, "", "name not found")

    # `candidates` starts as the roster students with this name.
    candidates = by_name
    if number:
        trunc = get_trunc_id(number)
        candidates = [student for student in candidates if student[1] == trunc]
        if not candidates:
            return Match(None, None, "", "student number does not match")
    if division:
        candidates = [student for student in candidates if student[2] == division]
        if not candidates:
            return Match(None, None, "", "division does not match")

    # more than one student is still possible: it cannot be decided, so a person has to
    if len(candidates) > 1:
        what = "name and student number" if number else "name"
        within = " in that division" if division else ""
        need = "a student number" if not number else "a division" if not division else "a person to decide"
        return Match(None, None, "", f"{what} shared by {len(candidates)} students{within}; {need} is needed")

    # exactly one student is left; the more things that agreed, the higher the level
    fuzzy = similarity is not None
    factors = ["fuzzy name" if fuzzy else "name"] + ["student number"] * bool(number) + ["division"] * bool(division)
    method = " + ".join(factors) if len(factors) > 1 else METHOD_FUZZY_NAME if fuzzy else METHOD_NAME
    return Match(candidates[0][0], len(factors), method, "", similarity[candidates[0][0]] if fuzzy else None)


def _name_splits(name: str) -> list[tuple[str, str]]:
    """Every way to read a full name as (first names, last name): the last name is one or more trailing words."""
    words = name.split()
    return [(" ".join(words[:k]), " ".join(words[k:])) for k in range(1, len(words))]


def match_students(people: list[dict], cursor: sqlite3.Cursor, name_key: str = "full_name",
                   id_key: str = "id", division_key: str = "division") -> list[Match]:
    """
    Check each person against uoft_data and return one Match per person, in the same order. A person must give a
    name; a student number and a division are optional (see `decide`). Database errors are raised, never reported
    as "not found".

    A name that no roster student has exactly gets a fuzzy second pass, because students often leave out their
    middle names: the last name has to agree exactly (it is taken to be the trailing words of the name, as
    roster last names can have several), and the first names have to be alike (see `first_name_similarity`).
    The students that pass are decided like exact matches (see `decide`); the best `MAX_CANDIDATES` of them are
    kept on the Match, whatever the outcome.
    """
    results: list[Match] = []
    for start in range(0, len(people), _CHUNK):
        chunk = people[start:start + _CHUNK]
        names = [normalize_case(p.get(name_key) or "") for p in chunk]
        numbers = [(p.get(id_key) or "").strip() for p in chunk]
        divisions = [division_of(p.get(division_key) or "") for p in chunk]
        wanted_names = sorted({n for n in names if n})

        def roster_division(division, faculty):
            # a student's division is their college or division; a student with none belongs to their faculty
            # (the Senate form's division codes are colleges and faculties, as in the seats table)
            return division_of(division or "") or division_of(faculty or "")

        by_name: dict[str, list[tuple[str, int, str]]] = {}
        if wanted_names:
            cursor.execute(f"SELECT uuid, trunc_id, full_name, division, faculty FROM uoft_data "
                           f"WHERE full_name IN ({','.join('?' * len(wanted_names))})", wanted_names)
            for uoft_uuid, trunc_id, full_name, division, faculty in cursor.fetchall():
                by_name.setdefault(full_name, []).append((uoft_uuid, trunc_id, roster_division(division, faculty)))

        # students whose last name could be the end of a name that was not found, for the fuzzy pass
        last_names = sorted({last for n in wanted_names if n not in by_name for _, last in _name_splits(n)})
        by_last: dict[str, list[tuple]] = {}
        for i in range(0, len(last_names), _SQL_VARIABLES):
            part = last_names[i:i + _SQL_VARIABLES]
            cursor.execute(f"SELECT uuid, trunc_id, first_name, last_name, division, faculty FROM uoft_data "
                           f"WHERE last_name IN ({','.join('?' * len(part))})", part)
            for uoft_uuid, trunc_id, first_name, last_name, division, faculty in cursor.fetchall():
                by_last.setdefault(last_name, []).append(
                    (uoft_uuid, trunc_id, first_name, last_name, roster_division(division, faculty)))

        for raw, name, number, division in zip(chunk, names, numbers, divisions):
            if not name and str(raw.get(name_key) or "").strip():
                # a name was given, and nothing of it is plain ASCII (e.g. another script)
                results.append(Match(None, None, "", "name has no plain letters (only A-Z letters are supported)"))
                continue
            if name in by_name or not name:
                results.append(decide(name, number, division, by_name.get(name, [])))
                continue
            scored = []
            for given, last in _name_splits(name):
                for uoft_uuid, trunc_id, first_name, last_name, roster_div in by_last.get(last, []):
                    score = first_name_similarity(given, first_name)
                    if score:
                        scored.append((score, uoft_uuid, trunc_id, first_name, last_name, roster_div))
            if not scored:
                results.append(decide(name, number, division, []))
                continue
            scored.sort(key=lambda s: (-s[0], s[3], s[1]))
            match = decide(name, number, division, [(u, t, d) for _, u, t, _, _, d in scored],
                           {u: score for score, u, *_ in scored})
            candidates = tuple(Candidate(u, f"{first} {last}", f"xxxxx{t:04d}x", d, score)
                               for score, u, t, first, last, d in scored[:MAX_CANDIDATES])
            results.append(replace(match, candidates=candidates))
    return results


def construct_lookup_tables(mapping_file: Path) -> list[dict[str, str]]:
    # ensure the file exists
    if not mapping_file.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_file}")

    name_table_mapping = {}
    table_name_mapping = {}
    # the csv module handles quoted commas; skipinitialspace allows `"a", "b"` with a space after the comma
    with open(mapping_file, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f, skipinitialspace=True)
        header = next(reader, [])  # skip the header row
        logger.info(f"Processing lookup table from {mapping_file}; header: {header}")
        for row in reader:
            row = [_.strip() for _ in row]
            if len(row) >= 2 and row[0]:
                name_table_mapping[row[0]] = row[1]
                table_name_mapping[row[1]] = row[0]
    if not name_table_mapping:
        raise ValueError(f"Lookup table {mapping_file} has no 'form column, replacement name' rows")

    return [name_table_mapping, table_name_mapping]


def setup_reader(csv_file: Path, mapping_file: Path, optional=(), identity=()):
    """
    Read a form export with its columns renamed by the lookup table.

    `optional` are replacement names whose form column may be missing from the export (the program then works
    without them). `identity` are the replacement names that identify a person: at least one must be present.
    """
    name_table_mapping, table_name_mapping = construct_lookup_tables(mapping_file)

    # ensure the CSV file exists
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_file}")

    required_col = {form for form, name in name_table_mapping.items() if name not in set(optional)}
    f_in = open_csv(csv_file)
    reader = csv.reader(f_in)
    headers = next(reader, None)
    if not headers or not any(h.strip() for h in headers):
        f_in.close()
        raise ValueError(f"{csv_file} is empty: it has no header row. Export the full form responses again.")

    # check if required columns are present in the headers; padding such as the form's "Faculty " is ignored
    headers = [clean_text(h) for h in headers]
    if not required_col.issubset(set(headers)):
        missing_cols = required_col - set(headers)
        f_in.close()
        raise ValueError(f"Missing required names in columns: {missing_cols}")

    # replace current headers with new names; columns not in the mapping keep their original header so
    # the remaining columns stay aligned
    new_headers = []
    for key in headers:
        if name_table_mapping.get(key):
            new_headers.append(name_table_mapping[key])
        else:
            new_headers.append(key)
            logger.debug(f" {key} not in mapping; keeping its original name")

    if identity and not set(identity) & set(new_headers):
        f_in.close()
        raise ValueError(f"The export needs at least one of these columns to identify people: "
                         f"{', '.join(name_table_mapping_forms(name_table_mapping, identity))}")

    # replace reader
    return csv.DictReader(f_in, fieldnames=new_headers)


def mapped_names(mapping_file: Path) -> list[str]:
    """The replacement names a lookup table gives the form's columns: the fields the program uses from a form."""
    return sorted(construct_lookup_tables(mapping_file)[1])


def name_table_mapping_forms(name_table_mapping: dict[str, str], names) -> list[str]:
    """The form column headers behind some replacement names (a name with no lookup row is its own header)."""
    by_name = {name: form for form, name in name_table_mapping.items()}
    return [by_name.get(n, n) for n in names]