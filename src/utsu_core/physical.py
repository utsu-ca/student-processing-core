"""
Physical verification: record that a student was checked in person (e.g. at the AGM door, or when a bursary is
handed over), which lifts their verified applications from level 2 (name + student number) to level 3.

The CSV needs one way to find each student: `student_uuid`, `email`, `student_number` (or `id`) or `full_name`, the
same key the application was filed under (email, else student number, else name). An optional `checked_in`
(or `physical`) column limits it to the rows that say yes, so a filled-in agm_list_all.csv can be used as it is.
"""
import csv
import logging
import sqlite3
from pathlib import Path

from utsu_core.verification import LEVEL_BOTH, LEVEL_PHYSICAL
from utsu_std.csv_safe import read_cell
from utsu_std.utils import student_uuid_for

logger = logging.getLogger(__name__)

TABLES = ("senate_application", "student_aid", "agm_rsvp")
_NO = {"", "no", "n", "0", "false"}


def _tables(conn: sqlite3.Connection) -> list[str]:
    have = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [t for t in TABLES if t in have]


def record_physical(conn: sqlite3.Connection, path: Path | str) -> dict:
    """
    Raise the verification level of each listed student's verified applications to 3. A student who is not
    verified, or only at level 1 (a name or a student number alone), is reported in `problems` and left as
    they are: level 3 means name + student number + checked in person.
    """
    tables = _tables(conn)
    if not tables:
        raise ValueError("The database has no applications yet; process a form export first")
    recorded, updated, problems = 0, 0, []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for line, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): read_cell((v or "").strip()) for k, v in raw.items()}
            flag = row.get("checked_in", row.get("physical"))
            if flag is not None and flag.lower() in _NO:
                continue
            student = row.get("student_uuid") or student_uuid_for(
                row.get("email", ""), row.get("student_number") or row.get("id", ""),
                row.get("full_name") or row.get("name", ""))
            if not any(row.get(k) for k in ("student_uuid", "email", "student_number", "id", "full_name", "name")):
                continue

            levels = [lvl for t in tables for (lvl,) in conn.execute(
                f"SELECT verification_level FROM {t} WHERE student_uuid = ? AND status = 'verified'", (student,))]
            if not levels:
                problems.append(f"line {line}: no verified application found for this student "
                                f"(they must be found by the same email, student number or name as the form)")
            elif LEVEL_BOTH not in levels and LEVEL_PHYSICAL not in levels:
                problems.append(f"line {line}: verified only by a name or a student number alone; level 3 needs both")
            else:
                for t in tables:
                    updated += conn.execute(f"UPDATE {t} SET verification_level = ? WHERE student_uuid = ? "
                                            f"AND status = 'verified' AND verification_level = ?",
                                            (LEVEL_PHYSICAL, student, LEVEL_BOTH)).rowcount
                recorded += 1
    conn.commit()
    for problem in problems:
        logger.warning(problem)
    logger.info(f"Recorded {recorded} student(s) as checked in person ({updated} application row(s) raised to "
                f"level 3); {len(problems)} row(s) skipped")
    return {"recorded": recorded, "updated": updated, "problems": problems}
