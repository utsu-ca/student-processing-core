"""
Build the recipient file for the "more information" and rejection emails.

    python -m utsu_mail.notices senate more_info
    python -m utsu_mail.notices sabp rejection --reason "Not a UTSU member this term"
    python -m utsu_mail.notices agm more_info --input path/to/agm_unverified.csv

Each row keeps its original columns and gains:
  application_uuid   the application's `uuid` (derived from its submission timestamp)
  student_uuid       uoft_data.uuid when the student can be matched (fully or partly); otherwise a stable
                     uuid derived from the student number (or the email when there is none)
  unverified_fields  the details that could not be verified, one per line (more_info only)
  rejection_reason   the text given with --reason (rejection only)

Then send with:  python -m utsu_mail <program>_<kind> --recipients <the file written here>
"""
import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

from utsu_mail.recipients import read_csv
from utsu_std.parsing_utils import get_trunc_id
from utsu_std.utils import absfile, generate_uuid, load_config_file, normalize_case

logger = logging.getLogger("utsu_mail.notices")

DEFAULT_INPUT = {"senate": "unverified.csv", "sabp": "sap_unverified.csv", "agm": "agm_unverified.csv"}
KINDS = ("more_info", "rejection")
NAME_FIELD = "- Full name (exactly as it appears on your ACORN registration)"
NUMBER_FIELD = "- Student number"


def trunc_of(student_number: str) -> int | None:
    """
    >>> trunc_of("1006639715")
    3971
    >>> trunc_of("")  is None
    True
    >>> trunc_of("12") is None
    True
    """
    try:
        trunc = get_trunc_id(student_number) if student_number else None
    except IndexError:
        return None
    return int(trunc) if trunc is not None and str(trunc).isdigit() else None


def derived_student_uuid(app: dict[str, str]) -> str:
    key = app.get("id", "").strip() or app.get("email", "").strip().lower()
    return generate_uuid("student|" + key)


def diagnose(app: dict[str, str], cursor: sqlite3.Cursor) -> tuple[str, list[str]]:
    """
    Compare an application with uoft_data. Returns (student_uuid, details that could not be verified).
    The name is blamed when it is not in uoft_data; the student number when the name is known but no record
    with that name has that number. A student number the form never asked for (AGM) is only requested when
    a shared name makes it necessary.
    """
    number = app.get("id", "").strip()
    trunc = trunc_of(number)
    by_name = cursor.execute("SELECT uuid, trunc_id FROM uoft_data WHERE full_name = ?",
                             (normalize_case(app.get("full_name", "")),)).fetchall()

    full_match = [u for u, t in by_name if trunc is not None and t == trunc]
    if full_match:
        return full_match[0], []

    if by_name:
        fields = [NUMBER_FIELD]
        candidates = [u for u, _ in by_name]
    else:
        fields = [NAME_FIELD]
        by_id = cursor.execute("SELECT uuid FROM uoft_data WHERE trunc_id = ?", (trunc,)).fetchall() \
            if trunc is not None else []
        candidates = [u for (u,) in by_id]
        if number and not by_id:
            fields.append(NUMBER_FIELD)
    return (candidates[0] if len(candidates) == 1 else derived_student_uuid(app)), fields


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
    fieldnames = list(dict.fromkeys(k for n in notices for k in n))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(notices)


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
