import csv
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from utsu_std.csv_safe import open_csv
from utsu_std.parsing_utils import division_code, get_trunc_id
from utsu_std.utils import clean_text, normalize_case

logger = logging.getLogger(__name__)

# How sure a match is: the number of factors (name, student number, division) that matched the same student.
# One factor alone is level 1 (a name or a student number: a division alone identifies nobody), any two are level 2
# (name + student number, name + division, student number + division), all three are level 3.
LEVEL_ONE, LEVEL_TWO, LEVEL_THREE = 1, 2, 3
LEVEL_NAMES = {LEVEL_ONE: "name or student number", LEVEL_TWO: "two of name, student number, division",
               LEVEL_THREE: "name + student number + division"}
METHOD_NAME, METHOD_ID, METHOD_BOTH = "name only", "student number only", "name + student number"
METHOD_NAME_DIVISION, METHOD_ID_DIVISION = "name + division", "student number + division"
METHOD_ALL = "name + student number + division"
_CHUNK = 300


@dataclass(frozen=True)
class Match:
    """The outcome of checking one person against uoft_data."""
    uoft_uuid: str | None
    level: int | None
    method: str
    reason: str

    @property
    def verified(self) -> bool:
        return self.uoft_uuid is not None


def trunc_of(student_number: str) -> int | None:
    """
    The roster's 4-digit form of a student number, or None when it is blank or unreadable.

    >>> trunc_of("1006639715"), trunc_of(" "), trunc_of("12")
    (3971, None, None)
    """
    trunc = get_trunc_id(student_number or "")
    return int(trunc) if trunc is not None and str(trunc).isdecimal() else None


def division_of(text: str) -> str:
    """
    The division code of a form or roster value: `UC - University College` is `UC`.

    >>> division_of(" uc - University College "), division_of(""), division_of(None)
    ('UC', '', '')
    """
    return division_code(clean_text(text))


def decide(name: str, number: str, division: str, by_name: list[tuple[str, int, str]],
           by_trunc: list[tuple[str, int, str]]) -> Match:
    """
    Decide one person from the roster rows sharing their name and the rows sharing their truncated number
    (each a (uoft_data.uuid, trunc_id, division code) tuple). Uses whatever was given: a name, a student number
    or both, and a division as well when the application has one. Every value given has to match the same
    student, and a wrong value never falls back to the others:
      name only                          the name belongs to exactly one student            level 1
      student number only                the number belongs to exactly one student          level 1
      name + student number              both belong to the same student                    level 2
      name + division                    exactly one student of that name is in the division level 2
      student number + division          likewise                                           level 2
      name + student number + division   all three belong to the same student              level 3
    A division alone identifies nobody. A roster student's division is their college or division, or their faculty
    when they have none (see `match_students`); a student with neither never matches a given division.

    >>> decide("Ann Lee", "1001231617", "", [("u1", 3161, "UC")], [("u1", 3161, "UC")])
    Match(uoft_uuid='u1', level=2, method='name + student number', reason='')
    >>> decide("Ann Lee", "1001231617", "UC", [("u1", 3161, "UC")], [("u1", 3161, "UC")]).level
    3
    >>> decide("Ann Lee", "", "", [("u1", 3161, "UC")], [])
    Match(uoft_uuid='u1', level=1, method='name only', reason='')
    >>> decide("Sam Lee", "", "TRIN", [("u1", 1, "UC"), ("u2", 2, "TRIN")], [])
    Match(uoft_uuid='u2', level=2, method='name + division', reason='')
    >>> decide("Ann Lee", "", "TRIN", [("u1", 3161, "UC")], []).reason
    'division does not match'
    >>> decide("Ann Lee", "1001231617", "TRIN", [("u1", 3161, "UC")], [("u1", 3161, "UC")]).reason
    'division does not match'
    >>> decide("", "1001231617", "", [], [("u1", 3161, "UC"), ("u2", 3161, "UC")]).reason
    'student number shared by 2 students; a name is needed'
    >>> decide("Ann Lee", "1009999999", "", [("u1", 3161, "UC")], []).reason
    'student number does not match'
    >>> decide("", "", "UC", [], []).reason
    'no name or student number given'
    """
    trunc = trunc_of(number)
    if name and number:
        if not by_name:
            return Match(None, None, "", "name not found")
        hits = [row for row in by_name if trunc is not None and row[1] == trunc]
        if not hits:
            return Match(None, None, "", "student number does not match")
        if division:
            hits = [row for row in hits if row[2] == division]
            if not hits:
                return Match(None, None, "", "division does not match")
            return Match(min(hits)[0], LEVEL_THREE, METHOD_ALL, "")
        return Match(min(hits)[0], LEVEL_TWO, METHOD_BOTH, "")
    if name or number:
        rows, what, other = (by_name, "name", "student number") if name else (by_trunc, "student number", "name")
        if not rows:
            return Match(None, None, "", f"{what} not found")
        if division:
            rows = [row for row in rows if row[2] == division]
            if not rows:
                return Match(None, None, "", "division does not match")
        if len(rows) > 1:
            within = " in that division" if division else ""
            return Match(None, None, "", f"{what} shared by {len(rows)} students{within}; a {other} is needed")
        level = LEVEL_TWO if division else LEVEL_ONE
        method = ((METHOD_NAME_DIVISION if name else METHOD_ID_DIVISION) if division
                  else (METHOD_NAME if name else METHOD_ID))
        return Match(rows[0][0], level, method, "")
    return Match(None, None, "", "no name or student number given")


def match_students(people: list[dict], cursor: sqlite3.Cursor, name_key: str = "full_name",
                   id_key: str = "id", division_key: str = "division") -> list[Match]:
    """
    Check each person against uoft_data and return one Match per person, in the same order. A person may give a
    name, a student number or both, and a division (see `decide`). Database errors are raised, never reported as
    "not found".
    """
    results: list[Match] = []
    for start in range(0, len(people), _CHUNK):
        chunk = people[start:start + _CHUNK]
        names = [normalize_case(p.get(name_key) or "") for p in chunk]
        numbers = [(p.get(id_key) or "").strip() for p in chunk]
        divisions = [division_of(p.get(division_key) or "") for p in chunk]
        wanted_names = sorted({n for n in names if n})
        wanted_truncs = sorted({t for t in map(trunc_of, numbers) if t is not None})

        by_name: dict[str, list[tuple[str, int, str]]] = {}
        by_trunc: dict[int, list[tuple[str, int, str]]] = {}
        clauses, params = [], []
        if wanted_names:
            clauses.append(f"full_name IN ({','.join('?' * len(wanted_names))})")
            params += wanted_names
        if wanted_truncs:
            clauses.append(f"trunc_id IN ({','.join('?' * len(wanted_truncs))})")
            params += wanted_truncs
        if clauses:
            cursor.execute(f"SELECT uuid, trunc_id, full_name, division, faculty FROM uoft_data "
                           f"WHERE {' OR '.join(clauses)}", params)
            for uoft_uuid, trunc_id, full_name, roster_division, roster_faculty in cursor.fetchall():
                # a student's division is their college or division; a student with none belongs to their faculty
                # (the Senate form's division codes are colleges and faculties, as in the seats table)
                row = (uoft_uuid, trunc_id, division_of(roster_division or "") or division_of(roster_faculty or ""))
                by_name.setdefault(full_name, []).append(row)
                by_trunc.setdefault(trunc_id, []).append(row)

        for raw, name, number, division in zip(chunk, names, numbers, divisions):
            if not name and not number and str(raw.get(name_key) or "").strip():
                # only a name was given, and nothing of it is plain ASCII (e.g. another script)
                results.append(Match(None, None, "", "name has no plain letters (only A-Z letters are supported)"))
                continue
            results.append(decide(name, number, division, by_name.get(name, []), by_trunc.get(trunc_of(number), [])))
    return results


def bulk_check_for_students(full_names: list[str], trunc_ids: list[str], cursor: sqlite3.Cursor):
    """
    Check students against uoft_data by (trunc_id, full_name).

    Returns {"result": bool, "matches": [...uoft_data rows], "non_matches": [(full_name, trunc_id), ...]}.
    Database errors are raised.
    """
    cursor.execute("DROP TABLE IF EXISTS temp_chk_stds")
    cursor.execute("CREATE TEMP TABLE temp_chk_stds (trunc_id INTEGER, full_name TEXT)")
    try:
        cursor.executemany(
            "INSERT INTO temp_chk_stds (full_name, trunc_id) VALUES (?, ?)",
            list(zip(full_names, trunc_ids))
        )
        cursor.execute("""
                SELECT s.*
                FROM uoft_data s
                INNER JOIN temp_chk_stds t
                ON s.trunc_id = t.trunc_id AND s.full_name = t.full_name
            """)
        matches = cursor.fetchall()

        cursor.execute("""
                SELECT t.full_name, t.trunc_id
                FROM temp_chk_stds t
                LEFT JOIN uoft_data s
                ON s.trunc_id = t.trunc_id AND s.full_name = t.full_name
                WHERE s.uuid IS NULL
            """)
        non_matches = cursor.fetchall()
    finally:
        cursor.execute("DROP TABLE IF EXISTS temp_chk_stds")

    return {"result": len(matches) == len(full_names), "matches": matches, "non_matches": non_matches}



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
    headers = next(reader)

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