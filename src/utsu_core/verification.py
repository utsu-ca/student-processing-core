import csv
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from utsu_std.csv_safe import open_csv
from utsu_std.parsing_utils import division_code, get_trunc_id
from utsu_std.utils import clean_text, normalize_case

logger = logging.getLogger(__name__)


METHOD_NAME = "name only"
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


def division_of(text: str) -> str:
    """
    The division code of a form or roster value: `UC - University College` is `UC`.

    >>> division_of(" uc - University College "), division_of(""), division_of(None)
    ('UC', '', '')
    """
    return division_code(clean_text(text))


def decide(name: str, number: str, division: str, by_name: list[tuple[str, int, str]]) -> Match:
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

    >>> decide("Ann Lee", "1001231617", "", [("u1", 3161, "UC")])
    Match(uoft_uuid='u1', level=2, method='name + student number', reason='')
    >>> decide("Ann Lee", "1001231617", "UC", [("u1", 3161, "UC")]).level
    3
    >>> decide("Ann Lee", "", "", [("u1", 3161, "UC")])
    Match(uoft_uuid='u1', level=1, method='name only', reason='')
    >>> decide("Sam Lee", "", "TRIN", [("u1", 1, "UC"), ("u2", 2, "TRIN")])
    Match(uoft_uuid='u2', level=2, method='name + division', reason='')
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
    factors = ["name"] + ["student number"] * bool(number) + ["division"] * bool(division)
    return Match(candidates[0][0], len(factors), " + ".join(factors) if len(factors) > 1 else METHOD_NAME, "")


def match_students(people: list[dict], cursor: sqlite3.Cursor, name_key: str = "full_name",
                   id_key: str = "id", division_key: str = "division") -> list[Match]:
    """
    Check each person against uoft_data and return one Match per person, in the same order. A person must give a
    name; a student number and a division are optional (see `decide`). Database errors are raised, never reported
    as "not found".
    """
    results: list[Match] = []
    for start in range(0, len(people), _CHUNK):
        chunk = people[start:start + _CHUNK]
        names = [normalize_case(p.get(name_key) or "") for p in chunk]
        numbers = [(p.get(id_key) or "").strip() for p in chunk]
        divisions = [division_of(p.get(division_key) or "") for p in chunk]
        wanted_names = sorted({n for n in names if n})

        by_name: dict[str, list[tuple[str, int, str]]] = {}
        if wanted_names:
            cursor.execute(f"SELECT uuid, trunc_id, full_name, division, faculty FROM uoft_data "
                           f"WHERE full_name IN ({','.join('?' * len(wanted_names))})", wanted_names)
            for uoft_uuid, trunc_id, full_name, roster_division, roster_faculty in cursor.fetchall():
                # a student's division is their college or division; a student with none belongs to their faculty
                # (the Senate form's division codes are colleges and faculties, as in the seats table)
                row = (uoft_uuid, trunc_id, division_of(roster_division or "") or division_of(roster_faculty or ""))
                by_name.setdefault(full_name, []).append(row)

        for raw, name, number, division in zip(chunk, names, numbers, divisions):
            if not name and str(raw.get(name_key) or "").strip():
                # a name was given, and nothing of it is plain ASCII (e.g. another script)
                results.append(Match(None, None, "", "name has no plain letters (only A-Z letters are supported)"))
                continue
            results.append(decide(name, number, division, by_name.get(name, [])))
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