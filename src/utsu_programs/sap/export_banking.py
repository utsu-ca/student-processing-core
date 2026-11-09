"""Join merged reviews to the matching SAP database and original form export.

    python -m utsu_programs.sap.export_banking REVIEWS.csv APPLICATIONS.csv DATABASE.sqlite -o OUTPUT.csv
"""
import argparse
import csv
import re
import sqlite3
import sys
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path

from utsu_core.verification import setup_reader
from utsu_programs.sap.program import SPEC, build_row
from utsu_std.csv_safe import open_csv, read_cell, write_rows
from utsu_std.utils import absfile, normalize_uoft_email

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


def export_banking(reviews: Path, applications: Path, database: Path, output: Path) -> int:
    """Write all review rows with their exact application details, or fail before writing."""
    if output.resolve() in {p.resolve() for p in (reviews, applications, database)}:
        raise ValueError("Output must be different from all input files")
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

    missing_uuids = needed.keys() - found.keys()
    if missing_uuids:
        records = ", ".join(f"B{needed[u][0]:03d}-{needed[u][1]:02d}" for u in sorted(missing_uuids))
        raise ValueError(f"Applications not found in the form export for: {records}")

    rows = [{**row, **found[slots[key][0]]} for key, row in review_rows]
    output.parent.mkdir(parents=True, exist_ok=True)
    write_rows(output, [*fields, *EXPORT_FIELDS], rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reviews", type=Path, help="merged reviews CSV")
    parser.add_argument("applications", type=Path, help="original SABP form export CSV")
    parser.add_argument("database", type=Path, help="SAP database used to assign the review batches")
    parser.add_argument("-o", "--output", type=Path, required=True, help="private output CSV")
    args = parser.parse_args(argv)
    try:
        count = export_banking(args.reviews, args.applications, args.database, args.output)
    except (OSError, sqlite3.Error, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {count} review record(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
