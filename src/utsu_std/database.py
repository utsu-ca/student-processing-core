import logging
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import csv

from utsu_std.utils import absfile, generate_uuid, normalize_case
from utsu_std.parsing_utils import get_uoft_trunc_format, get_trunc_id

logger = logging.getLogger(__name__)

def get_connection(filepath : Path):
    path = absfile(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def backup_database(db_path: Path | str, backup_dir: Path | str | None = None, keep: int = 0) -> Path | None:
    """
    Copy the database to `<backup_dir>/<name>_<YYYYmmdd-HHMMSS>.sqlite` (default folder: `backups/` next to it),
    using SQLite's backup, so the copy is consistent. Returns the copy, or None when there is no database yet.
    With `keep` > 0, only that many of the newest backups of this database are kept.
    """
    source = absfile(db_path)
    if not source.is_file() or source.stat().st_size == 0:
        return None
    folder = absfile(backup_dir) if backup_dir else source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = folder / f"{source.stem}_{stamp}.sqlite", 1
    while target.exists():  # two runs in the same second
        target, n = folder / f"{source.stem}_{stamp}_{n}.sqlite", n + 1
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    logger.info(f"Backed up the database to {target}")

    if keep > 0:
        pattern = re.compile(rf"^{re.escape(source.stem)}_\d{{8}}-\d{{6}}(_\d+)?\.sqlite$")
        old = sorted((p for p in folder.iterdir() if pattern.match(p.name)), key=lambda p: p.name)
        for stale in old[:-keep]:
            stale.unlink()
            logger.info(f"Removed old backup {stale}")
    return target

def import_csv_to_db(csv_file: Path, table_name: str, conn: sqlite3.Connection):
    """
    Function to import CSV data into the specified table in the database.
    Relies on the table's import_date column default (CURRENT_TIMESTAMP) to record when the data was imported.
    """
    try: 
        cursor = conn.cursor()
        with open(csv_file, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
            placeholders = ', '.join(['?'] * len(headers))
            query = f"INSERT INTO {table_name} ({', '.join(headers)}) VALUES ({placeholders})"
            for row in reader:
                cursor.execute(query, row)
        conn.commit()
        logger.info(f"Data imported successfully into {table_name}")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        conn.rollback()
        raise e

def export_db_to_csv(table_name: str, csv_file: Path, conn: sqlite3.Connection):
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        headers = [description[0] for description in cursor.description]

        with open(csv_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)

        logger.info(f"Data exported successfully from {table_name} to {csv_file}")
    except Exception as e:
        logger.error(f"An error occurred: {e}")

def return_unique_values(table_name: str, column_name: str, conn: sqlite3.Connection):
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT DISTINCT {column_name} FROM {table_name}")
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return []

def return_all_values(table_name: str, column_name: str, conn: sqlite3.Connection):
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT {column_name} FROM {table_name}")
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return []

def check_for_student(student: dict[str, str], cursor: sqlite3.Cursor):
    """
    Verify the provided student information against the stored database records.

    Args:
        cursor (Cursor): The database cursor to execute queries against.
        student (dict[str, str]): The student object containing the student's information to be verified.
        The student dictionary should contain the following keys:
            - "id" (optional): The full student ID.
            - "trun_id" (optional): The truncated student ID derived from the full ID.
            - "first_name": The student's first name.
            - "last_name": The student's last name.

    Returns:
        bool: True if the student information matches any stored records, False otherwise.
    """

    keys = student.keys()

    # check if id is supplied and derive trunc_id
    if "id" in keys and "trun_id" not in keys:
        student["trun_id"] = get_uoft_trunc_format(student["id"])

    # Ensure that the required student information fields are present
    required_fields = {"trun_id", "first_name", "last_name"}
    # Use subset math to determine if any required fields are missing
    if not required_fields.issubset(keys):
        missing_fields = required_fields - keys
        for field in missing_fields:
            logger.error(f"{field.replace('_', ' ').title()} is missing from the provided student information.")
        return False

    # query the database for the student information based on the required fields
    query = "SELECT * FROM students WHERE trun_id = ? AND first_name = ? AND last_name = ?"
    cursor.execute(query, (student["trun_id"], student["first_name"], student["last_name"]))
    matches = cursor.fetchall()

    return {"result": matches.__len__() == 1, "matches": matches}
