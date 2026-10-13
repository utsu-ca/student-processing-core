import csv
import logging
import sqlite3
from pathlib import Path

from utsu_std.csv_safe import open_csv
from utsu_std.utils import absfile, clean_text, normalize_case, typed_uuid

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("First Name", "Last Name", "Truncated Student Number", "Faculty", "Division")
_INSERT = ("INSERT INTO uoft_data (uuid, last_name, first_name, full_name, trunc_id, faculty, division) "
           "VALUES (:uuid, :last_name, :first_name, :full_name, :trunc_id, :faculty, :division)")


def ascii_digits(text: str) -> str:
    """
    The digits of a text and nothing else. The text is made ASCII first, so fullwidth and superscript digits count
    (other scripts' digits are dropped with the rest of the non-ASCII text).

    >>> ascii_digits("xxxxx3971x"), ascii_digits("\\uff13\\uff19\\u00b3"), ascii_digits("\\u0667\\u0661")
    ('3971', '393', '')
    """
    return "".join(c for c in clean_text(text) if c.isdigit())


def _statements(script: str) -> list[str]:
    """The SQL statements of a script, so they can run inside one transaction (PRAGMAs are left out)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if not buffer.strip().upper().startswith("PRAGMA"):
                statements.append(buffer.strip())
            buffer = ""
    return statements


def import_uoft_data(conn: sqlite3.Connection, csv_file: Path):
    """
    Replace the UofT student data in the database with the data in a CSV file. Returns True if the import is successful.

    The file is read and checked first; the table is only touched once it is known to be good, and the replacement
    (drop, create, insert, index) is one transaction, so a failure leaves the previous student data as it was.
    """
    logger.info(f"Importing UofT data from {csv_file}")
    rows = read_uoft_rows(Path(csv_file))
    if not rows:
        raise ValueError(f"{csv_file} has no students")
    script = Path(absfile("src/schema/uoft_data.sql")).read_text(encoding="utf-8-sig")

    conn.commit()
    try:
        conn.execute("BEGIN")
        for statement in _statements(script):
            conn.execute(statement)
        conn.executemany(_INSERT, rows)
        logger.info("Indexing uoft_data table, please wait...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_full_name ON uoft_data(`full_name`)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trunc_id ON uoft_data(`trunc_id`)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(f"Imported {len(rows)} students into uoft_data")
    return True


def read_uoft_rows(csv_file: Path) -> list[dict]:
    """
    The students in a roster CSV, cleaned (plain ASCII, names in Title Case, digits only). Blank lines are skipped.
    A missing column or a short row is an error, and rows that are exact duplicates are kept once, with a warning.
    """
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_file}")

    with open_csv(csv_file) as f:
        reader = csv.reader(f)
        headers = [clean_text(h) for h in next(reader, [])]
        missing_cols = set(REQUIRED_COLUMNS) - set(headers)
        if missing_cols:
            raise ValueError(f"Missing required columns: {missing_cols}")

        trunc_id_indx, last_name_indx, first_name_indx, faculty_indx, division_indx = (
            headers.index(col) for col in ("Truncated Student Number", "Last Name", "First Name", "Faculty", "Division"))
        needed = max(trunc_id_indx, last_name_indx, first_name_indx, faculty_indx, division_indx) + 1

        rows: dict[str, dict] = {}
        duplicates = 0
        for row in reader:
            if not any(cell.strip() for cell in row):  # blank lines, such as one at the end of the file
                continue
            if len(row) < needed:
                raise ValueError(f"Line {reader.line_num} of {csv_file} has {len(row)} column(s); expected at "
                                 f"least {needed}")
            student = {"trunc_id": ascii_digits(row[trunc_id_indx]),
                       "last_name": normalize_case(row[last_name_indx]),
                       "first_name": normalize_case(row[first_name_indx])}
            student["full_name"] = f"{student['first_name']} {student['last_name']}"
            student["faculty"] = clean_text(row[faculty_indx]).upper()
            student["division"] = clean_text(row[division_indx]).upper()
            student["uuid"] = typed_uuid("".join(student.values()), "roster")
            duplicates += student["uuid"] in rows
            rows[student["uuid"]] = student
    if duplicates:
        logger.warning(f"{duplicates} duplicate row(s) in {csv_file} (same name, student number, faculty and "
                       f"division) were kept once")
    return list(rows.values())
