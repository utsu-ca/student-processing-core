"""
Annual General Meeting (AGM) voter RSVP processor.

Reads the RSVP form export, keeps each person's newest RSVP, verifies them against uoft_data, records every RSVP in
the agm_rsvp table, and writes two attendee lists (everyone, and in-person only) plus a separate name-free
accommodations list for the organizers.

The verification can also be run on its own, which only reports (agm_verification.csv); it writes no voter lists
and no database rows:

    python -m utsu_core.agm --agm_file RSVP.csv --db path/to/db.sqlite [--output_dir DIR]
    python -m utsu_core.agm --testing        # fake RSVPs from the last `main --testing` run, against its database

The RSVP form may have no student number, so a person is then matched on their ACORN-registered name alone. A name
shared by more than one student cannot be decided and stays unverified until the form also supplies a "Student
Number" or "Division" column, which is then used to tell them apart. A name is always needed.
"""
import argparse
import json
import logging
import re
import sqlite3
from pathlib import Path

from utsu_core.selection import dedupe_by_uuid, select_current
from utsu_core.pipeline import Run, ensure_table
from utsu_core.verification import METHOD_NAME, match_students, setup_reader
from utsu_std.csv_safe import write_rows
from utsu_std.utils import absfile, application_uuid_for, normalize_case, student_uuid_for

logger = logging.getLogger(__name__)

LIST_FIELDS = ["last_name", "first_name", "preferred_name", "pronouns", "email", "meeting_preference", "contact_ok",
               "flags", "verification_level", "checked_in"]
UNVERIFIED_FIELDS = ["uuid", "student_uuid", "reason", "submission_ts", "full_name", "student_number", "email",
                     "personal_email", "meeting_preference", "flags"]
ACCOMMODATION_FIELDS = ["uuid", "meeting_preference", "dietary", "accessibility"]
REPORT_FIELDS = ["uuid", "status", "reason", "match_method", "verification_level", "student_uuid", "uoft_uuid",
                 "full_name", "email", "flags"]
# the form needs a name column; "Student Number" and "Division" are optional and only narrow a match
IDENTITY_COLUMNS = ("first_name", "last_name")


def agm_uuid(rsvp: dict[str, str]) -> str:
    """Internal id for an RSVP, so reprocessing the same form yields the same uuid."""
    return application_uuid_for("agm", rsvp.get("submission_ts", ""), rsvp.get("email", ""),
                                rsvp.get("student_number", ""), rsvp.get("full_name", ""))


def is_yes(value: str) -> bool:
    """
    >>> is_yes(" Yes, I agree")
    True
    >>> is_yes("")
    False
    """
    return (value or "").strip().lower().startswith("yes")


def is_ambiguous(reason: str) -> bool:
    """
    Whether an unverified RSVP failed because more than one student matches it (a shared name).

    >>> is_ambiguous("name shared by 2 students; a student number is needed"), is_ambiguous("name not found")
    (True, False)
    """
    return "shared by" in (reason or "")


def review_flags(rsvp: dict, match_method: str, reason: str) -> str:
    flags = []
    if match_method == METHOD_NAME:
        flags.append("name_only_match")
    if is_ambiguous(reason):
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
            if is_ambiguous(match.reason):
                logger.warning(f"AMBIGUOUS AGM RSVP {rsvp['uuid']}: more than one student matches ({match.reason}). "
                               f"A person has to decide which student this is.")
    return verified, unverified


def is_in_person(meeting_preference: str) -> bool:
    """
    Whether a meeting preference means attending in person.

    >>> [is_in_person(v) for v in ("In-person", "in person", "In Person (Hart House)", "Online", "Virtual", "")]
    [True, True, True, False, False, False]
    """
    return bool(re.search(r"\bin[\s-]*person\b", meeting_preference or "", re.I))


def attendance(meeting_preference: str) -> str:
    """
    How an RSVP's meeting preference is shown in the output files: In-person when the answer says so, otherwise
    Virtual (a blank or unclear answer defaults to Virtual).

    >>> attendance("Yes, I'll be attending in-person."), attendance("No, I will be attending virtually.")
    ('In-person', 'Virtual')
    >>> attendance("Online (Zoom)"), attendance(""), attendance("Undecided"), attendance(None)
    ('Virtual', 'Virtual', 'Virtual', 'Virtual')
    """
    return "In-person" if is_in_person(meeting_preference) else "Virtual"


def build_rsvp(row: dict[str, str]) -> dict:
    rsvp = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
    rsvp["full_name"] = normalize_case(f"{rsvp.get('first_name', '')} {rsvp.get('last_name', '')}")
    rsvp["student_number"] = rsvp.get("Student Number", "")  # optional column, kept under its original header
    rsvp["division"] = rsvp.get("Division", "")  # optional column too; it takes part in the match when given
    rsvp["contact_ok"] = "yes" if is_yes(rsvp.get("contact_consent", "")) else "no"
    rsvp["uuid"] = agm_uuid(rsvp)
    rsvp["student_uuid"] = student_uuid_for(rsvp.get("email", ""), rsvp["student_number"], rsvp["full_name"])
    return rsvp


def read_rsvps(csv_file: Path) -> list[dict]:
    mapping = absfile("data/conversion_table_agm.csv")
    reader = setup_reader(csv_file, mapping, optional=IDENTITY_COLUMNS, identity=IDENTITY_COLUMNS)
    rsvps = [build_rsvp(row) for row in reader]
    return dedupe_by_uuid([r for r in rsvps if r["full_name"] or r["student_number"]])


def verify_file(csv_file: Path | str, cursor: sqlite3.Cursor, output_dir: Path | str) -> list[dict]:
    """
    Read an RSVP export, verify it and write agm_verification.csv (private: names and emails) to output_dir, with
    the binding RSVP per person (see selection.select_current). Returns those RSVPs. Nothing is saved.
    """
    cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'uoft_data'")
    if not cursor.fetchone():
        raise ValueError("The database has no uoft_data table; import the UofT student data first")
    rsvps = read_rsvps(Path(csv_file))
    verified, unverified = verify_rsvps(rsvps, cursor)
    current = select_current(rsvps)[0]

    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    write_rows(out / "agm_verification.csv", REPORT_FIELDS, current)
    logger.info(f"AGM verification: {len(verified)} verified, {len(unverified)} unverified of {len(rsvps)} RSVPs "
                f"({len(current)} binding); report in {out / 'agm_verification.csv'}")
    return current


def attendee_list(verified: list[dict], in_person_only: bool = False) -> list[dict]:
    """Verified attendees sorted by last then first name, for check-in; optionally only those attending in person."""
    rows = [r for r in verified if not in_person_only or is_in_person(r.get("meeting_preference", ""))]
    return sorted(rows, key=lambda r: (r.get("last_name", "").lower(), r.get("first_name", "").lower(), r["uuid"]))


_INSERT = """
INSERT INTO agm_rsvp (uuid, student_uuid, uoft_uuid, verification_level, status, reason, match_method,
                      submission_ts, meeting_preference, contact_ok, flags)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :status, :reason, :match_method, :submission_ts,
        :meeting_preference, :contact_ok, :flags)
"""


def ensure_schema(conn: sqlite3.Connection):
    ensure_table(conn, "src/schema/agm.sql")


def write_csv(rows: list[dict], fields: list[str], path: Path):
    """Write rows to a CSV, with the meeting preference shown simply as In-person / Virtual."""
    write_rows(path, fields, [{**r, "meeting_preference": attendance(r.get("meeting_preference", ""))} for r in rows])


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value or "(blank)"] = counts.get(value or "(blank)", 0) + 1
    return dict(sorted(counts.items()))


def process_agm(csv_file: Path | str, conn: sqlite3.Connection, output_dir: Path | str = "secrets/output") -> dict:
    """
    Process an AGM voter RSVP export and write, under output_dir:
      agm_list_all.csv        every verified attendee (door list; no ids; meeting preference as In-person / Virtual)
      agm_list_in_person.csv  verified attendees whose meeting preference is in person
      agm_unverified.csv      RSVPs that could not be verified, with the reason
      agm_accommodations.csv  dietary and accessibility needs of verified attendees, by uuid only (no names)
      alerts_agm.csv          what a person has to look at (duplicate, ambiguous, non-binding RSVPs)
      agm_run.json            counts
    The lists carry names and emails and are private. The agm_rsvp table is rebuilt from the export.

    Give the full export each time. Every RSVP in it is verified against the current student data (the status
    follows the student data, so it can change when the data is re-imported). An edit or a re-submission is a newer
    RSVP: per person, the newest verified RSVP is the binding one; a newer unverified one never replaces it (it
    becomes non-binding). The lists always cover the whole export.
    """
    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)

    rsvps = read_rsvps(Path(csv_file))
    ensure_schema(conn)
    run = Run(conn, "agm_rsvp", "AGM", "agm", rsvps, sticky=False)
    verify_rsvps(rsvps, conn.cursor())
    run.select()
    unverified = [r for r in run.current if r["status"] == "unverified"]

    run.replace_all(_INSERT)
    run.settle()
    verified = [r for r in run.current if r["status"] == "verified"]
    everyone, in_person = attendee_list(verified), attendee_list(verified, in_person_only=True)
    write_csv(everyone, LIST_FIELDS, out / "agm_list_all.csv")
    write_csv(in_person, LIST_FIELDS, out / "agm_list_in_person.csv")
    write_csv(unverified, UNVERIFIED_FIELDS, out / "agm_unverified.csv")
    write_csv([r for r in everyone if r.get("dietary") or r.get("accessibility")], ACCOMMODATION_FIELDS,
              out / "agm_accommodations.csv")
    # an RSVP that more than one student fits cannot be verified: raise it for a person, every run until resolved
    ambiguous = [r for r in unverified if is_ambiguous(r.get("reason", ""))]
    alerts = run.alert(out, ambiguous)

    summary = {**run.summary(alerts),
               "binding_rsvps": len(run.current),
               "verified_rsvps": len(verified), "verified_in_person": len(in_person),
               "unverified_rsvps": len(unverified), "ambiguous_matches": len(ambiguous),
               "attendance": _tally(attendance(r.get("meeting_preference", "")) for r in verified),
               "match_method": _tally(r["match_method"] for r in verified),
               "unverified_reasons": _tally(r["reason"] for r in unverified),
               "dietary_requests": sum(bool(r.get("dietary")) for r in verified),
               "accessibility_requests": sum(bool(r.get("accessibility")) for r in verified),
               "flags": _tally(f for r in run.current for f in (r.get("flags") or "").split(";") if f)}
    (out / "agm_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"AGM: {len(verified)} verified ({len(in_person)} in person), {len(unverified)} unverified")
    return {"verified": verified, "in_person": in_person, "unverified": unverified, "alerts": alerts,
            "ambiguous": ambiguous, "summary": summary}


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
