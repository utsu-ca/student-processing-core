import csv
import logging
import re
import sqlite3
from pathlib import Path

from utsu_std.database import import_csv_to_db
from utsu_std.csv_safe import open_csv
from utsu_std.utils import absfile, clean_text, normalize_case, typed_uuid

logger = logging.getLogger(__name__)


def ascii_digits(text: str) -> str:
    """
    The digits of a text and nothing else. The text is made ASCII first, so fullwidth and superscript digits count
    (other scripts' digits are dropped with the rest of the non-ASCII text).

    >>> ascii_digits("xxxxx3971x"), ascii_digits("\\uff13\\uff19\\u00b3"), ascii_digits("\\u0667\\u0661")
    ('3971', '393', '')
    """
    return "".join(c for c in clean_text(text) if c.isdigit())


def import_uoft_data(conn: sqlite3.Connection, csv_file: Path):
    """
    Function to import UofT data from a CSV file into the database.
    Returns True if the import is successful.
    """
    logger.info(f"Creating uoft_data table")
    try:
        script = open( absfile("src/schema/uoft_data.sql") ).read()
        conn.executescript(script)
    except Exception as e:
        logger.error(f"An error occurred while creating uoft_data table: {e}")
        conn.rollback()
        return False

    logger.info(f"Importing UofT data from {csv_file}")
    preprocessed_csv_file = preprocess_uoft_csv(csv_file)
    logger.info(f"Preprocessed UofT CSV file: {csv_file} -> {preprocessed_csv_file}")
    import_csv_to_db(preprocessed_csv_file, "uoft_data", conn)

    logger.info(f"Indexing uoft_data table, please wait...")
    # apply index on Last Name column, followed by Truncated Student Number
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_full_name ON uoft_data(`full_name`)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trunc_id ON uoft_data(`trunc_id`)")
    conn.commit()
    logger.info(f"Indexes applied successfully on uoft_data table")

    return True


def preprocess_uoft_csv(csv_file: Path):
    # Preprocess the UofT CSV file to ensure it meets the required format before importing into the database.

    # step one: ensure required columns are present in the CSV file

    # ensure the CSV file exists
    if not csv_file.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_file}")

    required_col = ("First Name", "Last Name", "Truncated Student Number", "Faculty", "Division")
    processed_csv_file = csv_file.with_name(csv_file.stem + "_processed.csv")
    with open_csv(csv_file) as f_in, \
            open(processed_csv_file, 'w', newline='', encoding='utf-8') as f_out:
        reader = csv.reader(f_in)
        headers = [clean_text(h) for h in next(reader)]

        # check if required columns are present in the headers
        if not set(required_col).issubset(set(headers)):
            missing_cols = set(required_col) - set(headers)
            # clean up after self
            f_out.close()
            processed_csv_file.unlink()

            raise ValueError(f"Missing required columns: {missing_cols}")

        # prepare the CSV writer with the expected headers
        expected_headers: tuple[str, ...] = ("uuid", "last_name", "first_name", "full_name", "trunc_id",
                                             "faculty", "division")
        writer = csv.DictWriter(f_out, fieldnames=expected_headers)
        writer.writeheader()

        # step two: process rows
        for row in reader:
            # using dictionary so order doesn't matter
            row_dict = {}

            trunc_id_indx = headers.index("Truncated Student Number")
            last_name_indx = headers.index("Last Name")
            first_name_indx = headers.index("First Name")
            faculty_indx = headers.index("Faculty")
            division_indx = headers.index("Division")

            row_dict["trunc_id"] = ascii_digits(row[trunc_id_indx])
            row_dict["last_name"] = normalize_case(row[last_name_indx])
            row_dict["first_name"] = normalize_case(row[first_name_indx])
            row_dict["full_name"] = f"{row_dict["first_name"]} {row_dict["last_name"]}"
            row_dict["faculty"] = clean_text(row[faculty_indx]).upper()
            row_dict["division"] = clean_text(row[division_indx]).upper()
            # generate UUID; should be safe for UUID as .values() returns in insertion order.
            row_dict["uuid"] = typed_uuid("".join(row_dict.values()), "roster")

            writer.writerow(row_dict)
    return processed_csv_file