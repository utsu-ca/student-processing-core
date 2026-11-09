"""Join merged reviews to the matching SAP database and original form export.

    python -m utsu_programs.sap.export_banking REVIEWS.csv APPLICATIONS.csv DATABASE.sqlite -o OUTPUT.csv
"""
import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path

from utsu_core.verification import setup_reader
from utsu_programs.sap.eft import create_eft
from utsu_programs.sap.program import SPEC, build_row
from utsu_std.csv_safe import open_csv, read_cell, write_rows
from utsu_std.utils import absfile, normalize_uoft_email

logger = logging.getLogger(__name__)

BANK_FIELDS = {
    "institution_name": "Name of Institution",
    "institution_number": "Institution Number (3 digits)",
    "transit_number": "Transit Number (5 digits)",
    "account_number": "Account Number",
}
EXPORT_FIELDS = ["application_uuid", "first_name", "last_name", "email", "original_email", "email_domain_similarity",
                 *BANK_FIELDS, "export_flags"]
ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
RECORD_ID = re.compile(r"B(\d+)-(\d+)", re.I)


def export_email(value: str) -> tuple[str, str, list[str]]:
    """Normalize usable addresses; leave invalid addresses blank and flag every change."""
    normalized, status = normalize_uoft_email(value)
    original = value.strip()
    if status == "invalid" or not ADDRESS.fullmatch(original):
        return "", "", ["invalid_email"]
    domain = original.rsplit("@", 1)[1].lower().rstrip(".")
    score = max(SequenceMatcher(None, domain, target).ratio()
                for target in ("utoronto.ca", "mail.utoronto.ca"))
    flags = ["email_changed"] if normalized != value or status == "other" else []
    if score < 0.8:
        flags.append("email_low_domain_similarity")
    if status == "other":
        normalized = normalized.rsplit("@", 1)[0].lower() + "@mail.utoronto.ca"
    return normalized, f"{score:.3f}", flags


def join_banking(reviews: Path, applications: Path, database: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Join review rows; flag missing matches for reconciliation and reject ambiguous matches."""
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")

    with open_csv(reviews) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if not fields or any(not h or h != h.strip() for h in fields) or len(set(fields)) != len(fields):
            raise ValueError("Reviews CSV has empty, padded, or duplicate column names")
        if "record_id" not in fields:
            raise ValueError("Reviews CSV needs a record_id column")
        conflicts = set(fields) & set(EXPORT_FIELDS)
        if conflicts:
            raise ValueError(f"Reviews CSV already has output columns: {', '.join(sorted(conflicts))}")
        review_rows = []
        seen = set()
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"Reviews CSV line {line} has the wrong number of columns")
            row = {k: read_cell(v) for k, v in row.items()}
            match = RECORD_ID.fullmatch(row["record_id"].strip())
            if not match:
                raise ValueError(f"Reviews CSV line {line} has an invalid record_id")
            key = (int(match.group(1)), int(match.group(2)))
            if key in seen:
                raise ValueError(f"Reviews CSV line {line} has a duplicate record_id")
            seen.add(key)
            review_rows.append((key, row))
    if not review_rows:
        raise ValueError("Reviews CSV has no records")

    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        slots: dict[tuple[int, int], list[str]] = {}
        for uuid, batch, record in conn.execute(
                "SELECT uuid, batch, record FROM student_aid WHERE batch IS NOT NULL AND record IS NOT NULL"):
            slots.setdefault((batch, record), []).append(uuid)

    needed: dict[str, tuple[int, int]] = {}
    for key, _ in review_rows:
        matches = slots.get(key, [])
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(f"Record B{key[0]:03d}-{key[1]:02d} has {len(matches)} database matches; expected one")
        if matches[0] in needed:
            raise ValueError("Multiple record IDs refer to the same database application")
        needed[matches[0]] = key

    reader = setup_reader(applications, absfile(SPEC.mapping), optional=SPEC.optional, identity=SPEC.identity)
    headers = reader.fieldnames or []
    missing = set(BANK_FIELDS.values()) - set(headers)
    if missing:
        raise ValueError(f"Application export is missing columns: {', '.join(sorted(missing))}")
    if len(headers) != len(set(headers)):
        raise ValueError("Application export has duplicate column names after mapping")
    found = {}
    for line, raw in enumerate(reader, 2):
        if None in raw or any(v is None for v in raw.values()):
            raise ValueError(f"Application export line {line} has the wrong number of columns")
        app = build_row(raw)
        if not app or app["uuid"] not in needed:
            continue
        uuid = app["uuid"]
        if uuid in found:
            raise ValueError(f"Application export has duplicate rows for record "
                             f"B{needed[uuid][0]:03d}-{needed[uuid][1]:02d}")
        original_email = raw.get("email", "")
        email, similarity, flags = export_email(original_email)
        bank = {key: raw[column].strip() for key, column in BANK_FIELDS.items()}
        if any(not value for value in bank.values()):
            flags.append("missing_bank_information")
        found[uuid] = {
            "application_uuid": uuid,
            "first_name": raw.get("first_name", "").strip(),
            "last_name": raw.get("last_name", "").strip(),
            "email": email,
            "original_email": original_email,
            "email_domain_similarity": similarity,
            **bank,
            "export_flags": ";".join(flags),
        }

    rows = []
    for key, row in review_rows:
        matches = slots.get(key, [])
        uuid = matches[0] if matches else ""
        if not matches or uuid not in found:
            reason = "missing_database_record" if not matches else "missing_application"
            details = {field: "" for field in EXPORT_FIELDS}
            details.update(application_uuid=uuid, export_flags=f"manual_reconciliation;{reason}")
            logger.warning("Record %s requires manual reconciliation: %s", row["record_id"], reason)
        else:
            details = found[uuid]
        rows.append({**row, **details})
    return [*fields, *EXPORT_FIELDS], rows


def export_banking(reviews: Path, applications: Path, database: Path, output: Path) -> int:
    """Write all review rows with their exact application details, or fail before writing."""
    if output.resolve() in {p.resolve() for p in (reviews, applications, database)}:
        raise ValueError("Output must be different from all input files")
    fields, rows = join_banking(reviews, applications, database)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_rows(output, fields, rows)
    return len(rows)


def make_eft(rows: list[dict[str, str]], config_path: Path) -> bytes:
    """Generate an EFT in memory; no output is written unless every payment validates."""
    if any("manual_reconciliation" in row["export_flags"].split(";") for row in rows):
        raise ValueError("EFT blocked: applications require manual reconciliation")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return create_eft(rows, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reviews", type=Path, help="merged reviews CSV")
    parser.add_argument("applications", type=Path, help="original SABP form export CSV")
    parser.add_argument("database", type=Path, help="SAP database used to assign the review batches")
    parser.add_argument("-o", "--output", type=Path, required=True, help="private output CSV")
    parser.add_argument("--eft-output", type=Path, help="new private Standard 005 EFT file (never overwrites)")
    parser.add_argument("--eft-config", type=Path, help="private JSON file with bank-specific EFT settings")
    args = parser.parse_args(argv)
    try:
        options = (args.eft_output, args.eft_config)
        if any(options) and not all(options):
            raise ValueError("--eft-output and --eft-config must be supplied together")
        if not args.eft_output:
            count = export_banking(args.reviews, args.applications, args.database, args.output)
        else:
            paths = [args.reviews, args.applications, args.database, args.eft_config, args.output, args.eft_output]
            if len({p.resolve() for p in paths}) != len(paths):
                raise ValueError("All inputs and outputs must be different files")
            if args.eft_output.exists():
                raise FileExistsError(f"EFT output already exists; choose a new path: {args.eft_output}")
            fields, rows = join_banking(args.reviews, args.applications, args.database)
            unresolved = [row for row in rows if "manual_reconciliation" in row["export_flags"].split(";")]
            if unresolved:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                write_rows(args.output, fields, rows)
                raise ValueError(f"EFT blocked: {len(unresolved)} application(s) require manual reconciliation; "
                                 f"review {args.output}")
            eft = make_eft(rows, args.eft_config)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.eft_output.parent.mkdir(parents=True, exist_ok=True)
            with args.eft_output.open("xb") as f:
                try:
                    f.write(eft)
                    write_rows(args.output, fields, rows)
                except (OSError, ValueError, csv.Error):
                    f.close()
                    args.eft_output.unlink()
                    raise
            count = len(rows)
    except (OSError, sqlite3.Error, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} review record(s) to {args.output}")
    if args.eft_output:
        print(f"Created Standard 005 EFT file: {args.eft_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
