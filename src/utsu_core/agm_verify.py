"""
Verify AGM voter RSVPs against uoft_data. Used by agm.py, and runnable on its own:

    python -m utsu_core.agm_verify --agm_file RSVP.csv --db path/to/db.sqlite [--output_dir DIR]
    python -m utsu_core.agm_verify --testing        # fake RSVPs from the last `main --testing` run, against its database

The standalone run only reports (agm_verification.csv); it writes no voter lists and no database rows.

The RSVP form may have no student number, so a person is then matched on their ACORN-registered name alone. A name
shared by more than one student cannot be decided and stays unverified until the form also supplies a "Student
Number" column, which is then used to tell them apart (or on its own, when a row has no name).
"""
import argparse
import csv
import logging
import sqlite3
from pathlib import Path

from utsu_core.verification import METHOD_ID, METHOD_NAME, match_students
from utsu_std.csv_safe import SafeDictWriter
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)

REPORT_FIELDS = ["uuid", "status", "reason", "match_method", "verification_level", "student_uuid", "uoft_uuid",
                 "full_name", "email", "flags"]


def is_yes(value: str) -> bool:
    """
    >>> is_yes(" Yes, I agree")
    True
    >>> is_yes("")
    False
    """
    return (value or "").strip().lower().startswith("yes")


def review_flags(rsvp: dict, match_method: str, reason: str) -> str:
    flags = []
    if match_method == METHOD_NAME:
        flags.append("name_only_match")
    if match_method == METHOD_ID:
        flags.append("student_number_only_match")
    if reason.startswith("name shared"):
        flags.append("ambiguous_name")
    if not is_yes(rsvp.get("data_confirmation", "")):
        flags.append("no_data_confirmation")
    if not rsvp.get("email"):
        flags.append("no_uoft_email")
    return ";".join(flags)


def verify_rsvps(rsvps: list[dict], cursor: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
    """
    Verify RSVPs (one per person) against uoft_data, by name, student number or both. Sets uoft_uuid,
    verification_level, match_method, reason, status and flags on each and returns (verified, unverified).
    """
    verified, unverified = [], []
    for rsvp, match in zip(rsvps, match_students(rsvps, cursor, id_key="student_number")):
        rsvp.update(uoft_uuid=match.uoft_uuid, verification_level=match.level, match_method=match.method,
                    reason=match.reason, status="verified" if match.verified else "unverified",
                    flags=review_flags(rsvp, match.method, match.reason))
        (verified if match.verified else unverified).append(rsvp)
        if not match.verified:
            logger.warning(f"UNVERIFIED AGM RSVP {rsvp['uuid']}: {match.reason}")
    return verified, unverified


def verify_file(csv_file: Path | str, cursor: sqlite3.Cursor, output_dir: Path | str) -> list[dict]:
    """Read an RSVP export, verify it and write agm_verification.csv (private: names and emails) to output_dir."""
    from utsu_core.agm import current_rsvps, read_rsvps  # agm imports this module, so import it late

    cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'uoft_data'")
    if not cursor.fetchone():
        raise ValueError("The database has no uoft_data table; import the UofT student data first")
    rsvps = read_rsvps(Path(csv_file))
    verified, unverified = verify_rsvps(rsvps, cursor)
    current = current_rsvps(rsvps)

    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "agm_verification.csv", "w", newline="", encoding="utf-8") as f:
        writer = SafeDictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(current)
    logger.info(f"AGM verification: {len(verified)} verified, {len(unverified)} unverified of {len(rsvps)} RSVPs "
                f"({len(current)} binding); report in {out / 'agm_verification.csv'}")
    return current


def main():
    parser = argparse.ArgumentParser(description="Verify AGM voter RSVPs against the UofT student data")
    parser.add_argument("--agm_file", default="", help="AGM voter RSVP export (CSV)")
    parser.add_argument("--db", default="", help="Database holding the uoft_data table")
    parser.add_argument("--output_dir", default="secrets/output", help="Where agm_verification.csv is written")
    parser.add_argument("--testing", action="store_true",
                        help="Use testing/output/fake_agm.csv and the database from the last `main --testing` run")
    args = parser.parse_args()
    if args.testing:
        args.agm_file = args.agm_file or "testing/output/fake_agm.csv"
        args.db = args.db or "testing/output/test.sqlite"
        args.output_dir = "testing/output" if args.output_dir == "secrets/output" else args.output_dir
    if not args.agm_file or not args.db:
        parser.error("--agm_file and --db are required (or use --testing)")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    conn = sqlite3.connect(absfile(args.db))
    try:
        rows = verify_file(absfile(args.agm_file), conn.cursor(), args.output_dir)
    finally:
        conn.close()
    verified = sum(r["status"] == "verified" for r in rows)
    print(f"{verified} verified, {len(rows) - verified} unverified of {len(rows)} RSVPs")


if __name__ == "__main__":
    main()
