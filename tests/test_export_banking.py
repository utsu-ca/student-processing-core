import csv
import sqlite3
import subprocess
import sys

import pytest

from utsu_core.verification import construct_lookup_tables, setup_reader
from utsu_programs.sap.export_banking import BANK_FIELDS, export_banking, export_email
from utsu_programs.sap.program import SPEC, build_row
from utsu_std.utils import absfile


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def inputs(tmp_path):
    mapping = construct_lookup_tables(absfile(SPEC.mapping))[0]
    fields = [*mapping, *BANK_FIELDS.values()]
    source = {
        "Timestamp": "2026-01-02 12:00:00",
        "ACORN-registered First Name": "Ann",
        "ACORN-registered Last Name": "Lee",
        "Student Number": "1001234567",
        "UofT Email Address": "Ann@gmail.com",
        "Name of Institution": "Example Bank",
        "Institution Number (3 digits)": "001",
        "Transit Number (5 digits)": "00023",
        "Account Number": "0000456",
    }
    applications = tmp_path / "applications.csv"
    write_csv(applications, fields, [source])
    app = build_row(next(setup_reader(applications, absfile(SPEC.mapping), optional=SPEC.optional,
                                      identity=SPEC.identity)))
    database = tmp_path / "database.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE student_aid (uuid TEXT, batch INTEGER, record INTEGER)")
        conn.execute("INSERT INTO student_aid VALUES (?, 1, 2)", (app["uuid"],))
    reviews = tmp_path / "reviews.csv"
    write_csv(reviews, ["record_id", "mean_score", "notes"],
              [{"record_id": "B001-02", "mean_score": "3", "notes": "=untrusted"}])
    return reviews, applications, database, tmp_path / "private" / "output.csv", source, fields


@pytest.mark.parametrize("value, expected, flags", [
    ("ann@mail.utoronto.ca", "ann@mail.utoronto.ca", []),
    (" Ann@MAIL.utoronto.ca ", "ann@mail.utoronto.ca", ["email_changed"]),
    ("ann@mal.utoront.c", "ann@mail.utoronto.ca", ["email_changed"]),
    ("ann@toronto.ca", "ann@mail.utoronto.ca", ["email_changed"]),
    ("ann@mai.utroronto.ca", "ann@mail.utoronto.ca", ["email_changed"]),
    ("Ann@gmail.com", "ann@mail.utoronto.ca", ["email_changed", "email_low_domain_similarity"]),
    ("not an address", "", ["invalid_email"]),
    ("", "", ["invalid_email"]),
])
def test_email(value, expected, flags):
    address, score, actual_flags = export_email(value)
    assert (address, actual_flags) == (expected, flags)
    if score:
        assert 0 <= float(score) <= 1
        if value == "ann@toronto.ca":
            assert float(score) >= 0.8


def test_export_cli_joins_and_preserves_bank_numbers(inputs):
    reviews, applications, database, output, _, _ = inputs
    result = subprocess.run(
        [sys.executable, "-m", "utsu_programs.sap.export_banking", str(reviews), str(applications),
         str(database), "-o", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with output.open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["record_id"] == "B001-02"
    assert row["mean_score"] == "3"
    assert row["notes"] == "'=untrusted"
    assert (row["first_name"], row["last_name"]) == ("Ann", "Lee")
    assert row["email"] == "ann@mail.utoronto.ca"
    assert row["original_email"] == "Ann@gmail.com"
    assert row["export_flags"] == "email_changed;email_low_domain_similarity"
    assert float(row["email_domain_similarity"]) < 0.8
    assert (row["institution_number"], row["transit_number"], row["account_number"]) == ("001", "00023", "0000456")


def test_missing_bank_fields_are_flagged(inputs):
    reviews, applications, database, output, source, fields = inputs
    source["Account Number"] = ""
    source["UofT Email Address"] = "invalid"
    # Changing the email changes the application UUID, so update the database as a real processing run would.
    write_csv(applications, fields, [source])
    app = build_row(next(setup_reader(applications, absfile(SPEC.mapping), optional=SPEC.optional,
                                      identity=SPEC.identity)))
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE student_aid SET uuid = ?", (app["uuid"],))
    export_banking(reviews, applications, database, output)
    with output.open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["email"] == ""
    assert row["export_flags"] == "invalid_email;missing_bank_information"


@pytest.mark.parametrize("failure", ["ambiguous_record", "duplicate_application",
                                     "duplicate_review", "missing_bank_column"])
def test_errors_never_replace_output(inputs, failure):
    reviews, applications, database, output, source, fields = inputs
    output.parent.mkdir()
    output.write_text("unchanged", encoding="utf-8")
    if failure == "ambiguous_record":
        with sqlite3.connect(database) as conn:
            conn.execute("INSERT INTO student_aid VALUES ('another', 1, 2)")
    elif failure == "duplicate_application":
        write_csv(applications, fields, [source, source])
    elif failure == "duplicate_review":
        write_csv(reviews, ["record_id"], [{"record_id": "B001-02"}, {"record_id": "b1-2"}])
    else:
        fields.remove("Account Number")
        del source["Account Number"]
        write_csv(applications, fields, [source])
    with pytest.raises(ValueError):
        export_banking(reviews, applications, database, output)
    assert output.read_text(encoding="utf-8") == "unchanged"


def test_missing_records_are_retained_and_flagged(inputs, caplog):
    reviews, applications, database, output, _, _ = inputs
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO student_aid VALUES ('missing-application-uuid', 1, 3)")
    write_csv(reviews, ["record_id", "mean_score", "notes"], [
        {"record_id": "B001-02", "mean_score": "3", "notes": "matched"},
        {"record_id": "B001-03", "mean_score": "2", "notes": "missing application"},
        {"record_id": "B001-04", "mean_score": "1", "notes": "missing database record"},
    ])
    assert export_banking(reviews, applications, database, output) == 3
    with output.open(encoding="utf-8-sig", newline="") as f:
        matched, missing_app, missing_db = list(csv.DictReader(f))
    assert matched["first_name"] == "Ann"
    assert missing_app["application_uuid"] == "missing-application-uuid"
    assert missing_app["export_flags"] == "manual_reconciliation;missing_application"
    assert missing_db["application_uuid"] == ""
    assert missing_db["export_flags"] == "manual_reconciliation;missing_database_record"
    for row in (missing_app, missing_db):
        assert all(not row[field] for field in ("first_name", "email", *BANK_FIELDS))
    assert "B001-03" in caplog.text and "B001-04" in caplog.text


def test_refuses_to_overwrite_an_input(inputs):
    reviews, applications, database, _, _, _ = inputs
    with pytest.raises(ValueError, match="different"):
        export_banking(reviews, applications, database, reviews)
