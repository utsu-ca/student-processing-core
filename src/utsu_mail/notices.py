"""
Build the recipient file for the "more information" and rejection emails.

    python -m utsu_mail.notices senate more_info
    python -m utsu_mail.notices sabp rejection --reason "Not a UTSU member this term"
    python -m utsu_mail.notices agm more_info --input path/to/agm_unverified.csv

Each row keeps its original columns and gains:
  application_uuid   the application's `uuid` (derived from its submission timestamp and the student's email)
  student_uuid       the internal per-student id: derived from the student's email (else student number, else
                     name), so it is the same before and after the student is verified
  unverified_fields  the details that could not be verified, one per line (more_info only)
  rejection_reason   the text given with --reason (rejection only)

Both ids are internal database keys, shown in the emails only as a reference for replies; a reply is matched back
to an application by the student's email, student number or name.

Then send with:  python -m utsu_mail <program>_<kind> --recipients <the file written here>
"""
import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from utsu_core.verification import match_students
from utsu_mail.recipients import read_csv
from utsu_std.csv_safe import write_rows
from utsu_std.utils import absfile, load_config_file, student_uuid_for

logger = logging.getLogger("utsu_mail.notices")

DEFAULT_INPUT = {"senate": "unverified.csv", "sabp": "sap_unverified.csv", "agm": "agm_unverified.csv"}
KINDS = ("more_info", "rejection")
NAME_FIELD = "- Full name (exactly as it appears on your ACORN registration)"
NUMBER_FIELD = "- Student number"
DIVISION_FIELD = "- Division (as it appears on your ACORN registration)"


def diagnose(app: dict[str, str], cursor: sqlite3.Cursor) -> tuple[str, list[str]]:
    """
    Compare an application with uoft_data. Returns (student_uuid, details that could not be verified).
    Works with a name plus, optionally, a student number and a division. The name is blamed when it is not in
    uoft_data or missing, the student number when it does not belong to the name, the division when it does not
    match the student, and the student number when a shared name cannot be decided without one.
    """
    number = (app.get("id") or app.get("student_number") or "").strip()
    name = app.get("full_name", "")
    division = app.get("division", "")
    student = student_uuid_for(app.get("email", ""), number, name)
    match = match_students([{"full_name": name, "id": number, "division": division}], cursor)[0]
    if match.verified:
        return student, []

    reason = match.reason
    if reason == "division does not match":
        fields = [DIVISION_FIELD]
    elif reason == "name not found" or reason == "no name given" or reason.startswith("name has no plain letters"):
        fields = [NAME_FIELD]
    else:  # a student number that does not match, or a name shared by several students
        fields = [NUMBER_FIELD]
    return student, fields


def build_notices(rows: list[dict[str, str]], cursor: sqlite3.Cursor, kind: str,
                  reason: str = "") -> list[dict[str, str]]:
    notices = []
    for row in rows:
        student_uuid, fields = diagnose(row, cursor)
        notice = dict(row, application_uuid=row.get("uuid", ""), student_uuid=student_uuid)
        if kind == "more_info":
            if not fields:
                logger.warning(f"Application {row.get('uuid')} matches uoft_data; nothing to ask for, skipped")
                continue
            notice["unverified_fields"] = "\n".join(fields)
        elif reason:
            notice["rejection_reason"] = reason
        notices.append(notice)
    return notices


def write_notices(path: Path, notices: list[dict[str, str]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_rows(path, list(dict.fromkeys(k for n in notices for k in n)), notices, extrasaction="raise")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="utsu_mail.notices", description=__doc__.split("\n\n")[0])
    p.add_argument("program", choices=sorted(DEFAULT_INPUT), help="Which application the emails are about")
    p.add_argument("kind", choices=KINDS, help="more_info (which fields could not be verified) or rejection")
    p.add_argument("--input", help="CSV of applications (default: <output_dir>/<the program's unverified file>)")
    p.add_argument("--output", help="Where to write the recipient CSV (default: <output_dir>/notices_<program>_<kind>.csv)")
    p.add_argument("--db", help="SQLite database holding uoft_data (default: 'db' from the config file)")
    p.add_argument("--config", default="config.json")
    p.add_argument("--reason", default="", help="rejection only: the reason given in the email")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    cfg = load_config_file(str(absfile(args.config)))
    output_dir = Path(cfg.get("output_dir", "secrets/output"))
    source = absfile(args.input or output_dir / DEFAULT_INPUT[args.program])
    db = args.db or cfg.get("db")
    if not db:
        logger.error("A database is needed: pass --db or set 'db' in the config file")
        return 2
    if not source.is_file():
        logger.error(f"Applications file not found: {source}")
        return 2

    rows = read_csv(source)
    if args.kind == "rejection" and not args.reason and not any(r.get("rejection_reason") for r in rows):
        logger.error("A rejection needs a reason: pass --reason or include a rejection_reason column")
        return 2

    conn = sqlite3.connect(f"{absfile(db).resolve().as_uri()}?mode=ro", uri=True)
    try:
        notices = build_notices(rows, conn.cursor(), args.kind, args.reason)
    finally:
        conn.close()

    target = absfile(args.output or output_dir / f"notices_{args.program}_{args.kind}.csv")
    write_notices(target, notices)
    logger.info(f"Wrote {len(notices)} recipient(s) to {target}")
    logger.info(f"Next: python -m utsu_mail {args.program}_{args.kind} --recipients \"{target}\"")
    return 0 if notices else 1


if __name__ == "__main__":
    sys.exit(main())
