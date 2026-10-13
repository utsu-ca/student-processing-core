import csv
import sqlite3
from pathlib import Path

from utsu_core import agm, agm_verify
from utsu_core.verification import construct_lookup_tables
from utsu_std.utils import absfile

HEADERS = ["Timestamp", "Username", "Preferred Name", "Preferred Pronouns", "ACORN-registered First Name",
           "ACORN-registered Last Name ", "UofT Email Address", "Meeting Preference",
           "Do you have any dietary restrictions or food allergies?", "Accessibility Requirements",
           "Data Processing Confirmation", "Contact Consent"]


def form_csv(path, rows, extra_header=None):
    header = HEADERS + ([extra_header] if extra_header else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def rsvp(ts, first, last, email="", dietary="", confirm="Yes", extra=None, meeting="Online"):
    row = [ts, "p@gmail.com", "", "she/her", first, last, email, meeting, dietary, "", confirm, "Yes"]
    return row + ([extra] if extra is not None else [])


def students_db(*people):
    conn = sqlite3.connect(":memory:")
    conn.executescript(Path(absfile("src/schema/uoft_data.sql")).read_text(encoding="utf-8"))
    for i, (first, last, trunc) in enumerate(people):
        conn.execute("INSERT INTO uoft_data (uuid, trunc_id, first_name, last_name, full_name, division, faculty) "
                     "VALUES (?, ?, ?, ?, ?, 'UC', 'ARTSC')", (f"u{i}", trunc, first, last, f"{first} {last}"))
    conn.commit()
    return conn


def test_lookup_table_matches_the_real_form_headers():
    names = construct_lookup_tables(Path(absfile("data/conversion_table_agm.csv")))[0]
    assert set(names) == {h.strip() for h in HEADERS}


def test_process_agm_verifies_by_name_and_dedupes(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca", dietary="Vegan"),
        rsvp("2026/10/02 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca", dietary="Halal"),  # newer wins
        rsvp("2026/10/01 10:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca", confirm=""),
        rsvp("2026/10/01 11:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca"),  # not a student
        rsvp("2026/10/01 12:00:00 p.m. AST", "Di", "Park", "di@mail.utoronto.ca"),  # name shared by two students
    ])
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Di", "Park", 1111), ("Di", "Park", 2222))
    out = tmp_path / "out"

    result = agm.process_agm(form, conn, output_dir=out)
    assert [r["full_name"] for r in result["verified"]] == ["Ann Lee", "Bo Chan"]
    reasons = {r["full_name"]: r["reason"] for r in result["unverified"]}
    assert reasons == {"Cy Doe": "name not found", "Di Park": "name shared by 2 students; a student number is needed"}
    ann = next(r for r in result["verified"] if r["full_name"] == "Ann Lee")
    assert ann["dietary"] == "Halal" and ann["match_method"] == "name only" and "name_only_match" in ann["flags"]
    bo = next(r for r in result["verified"] if r["full_name"] == "Bo Chan")
    assert "no_data_confirmation" in bo["flags"]

    accommodations = (out / "agm_accommodations.csv").read_text(encoding="utf-8")
    assert "Halal" in accommodations and "Ann" not in accommodations and "ann@" not in accommodations
    assert conn.execute("SELECT status, COUNT(*) FROM agm_rsvp GROUP BY status ORDER BY status").fetchall() \
        == [("superseded", 1), ("unverified", 2), ("verified", 2)]
    # the stored rows carry no names or emails
    assert "Ann" not in str(conn.execute("SELECT * FROM agm_rsvp").fetchall())


def test_everyone_and_in_person_lists(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca", meeting="In-person"),
        rsvp("2026/10/01 10:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca", meeting="Online"),
        rsvp("2026/10/01 11:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca", meeting="In-person"),  # unverified
        rsvp("2026/10/01 12:00:00 p.m. AST", "Ed", "Abe", "ed@mail.utoronto.ca", meeting="In person"),
    ])
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Ed", "Abe", 1234))
    out = tmp_path / "out"
    result = agm.process_agm(form, conn, output_dir=out)

    def names(file):
        with open(out / file, newline="", encoding="utf-8") as f:
            return [f"{r['first_name']} {r['last_name']}" for r in csv.DictReader(f)]

    assert names("agm_list_all.csv") == ["Ed Abe", "Bo Chan", "Ann Lee"]  # by last name
    assert names("agm_list_in_person.csv") == ["Ed Abe", "Ann Lee"]
    assert result["summary"]["verified_in_person"] == 2 and result["summary"]["unverified_rsvps"] == 1
    assert [r["full_name"] for r in result["in_person"]] == ["Ed Abe", "Ann Lee"]


def test_door_lists_show_a_simple_attendance_and_no_ids(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca", meeting="Yes, I'll be attending in-person."),
        rsvp("2026/10/01 10:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca", meeting="No, I will be attending virtually."),
        rsvp("2026/10/01 11:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca", meeting=""),  # blank: Virtual
        rsvp("2026/10/01 12:00:00 p.m. AST", "Di", "Fox", "di@mail.utoronto.ca", meeting="Undecided", dietary="Vegan"),
    ])
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000), ("Di", "Fox", 6000))
    out = tmp_path / "out"
    result = agm.process_agm(form, conn, output_dir=out)

    with open(out / "agm_list_all.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["last_name", "first_name", "preferred_name", "pronouns", "email", "meeting_preference",
                             "contact_ok", "flags", "verification_level", "checked_in"]
    assert {r["email"].split("@")[0]: r["meeting_preference"] for r in rows} == {
        "ann": "In-person", "bo": "Virtual", "cy": "Virtual", "di": "Virtual"}  # blank or unclear is Virtual
    assert "uuid" not in rows[0] and "application:" not in (out / "agm_list_all.csv").read_text(encoding="utf-8")
    assert [r["first_name"] for r in csv.DictReader(open(out / "agm_list_in_person.csv", encoding="utf-8"))] == ["Ann"]
    # the same simple values everywhere, including the accommodations and the run summary
    assert {r["meeting_preference"] for r in csv.DictReader(open(out / "agm_accommodations.csv", encoding="utf-8"))} == {"Virtual"}
    assert result["summary"]["attendance"] == {"In-person": 1, "Virtual": 3}
    # the stored row keeps the original answer
    assert conn.execute("SELECT meeting_preference FROM agm_rsvp WHERE meeting_preference = ''").fetchall() == [("",)]


def test_the_run_summary_has_descriptive_names(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),
        rsvp("2026/10/02 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),  # a later submission replaces it
        rsvp("2026/10/01 10:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca")])
    conn = students_db(("Ann", "Lee", 3161))
    s = agm.process_agm(form, conn, output_dir=tmp_path / "out")["summary"]
    assert (s["submissions_in_export"], s["new_submissions_this_run"], s["processed_this_run"],
            s["binding_rsvps"], s["verified_rsvps"], s["unverified_rsvps"]) == (3, 3, 3, 2, 1, 1)
    assert not {"new", "rsvps", "in_person", "verified", "unverified"} & set(s)
    again = agm.process_agm(form, conn, output_dir=tmp_path / "out")["summary"]
    assert (again["new_submissions_this_run"], again["processed_this_run"], again["binding_rsvps"]) == (0, 0, 2)


def test_in_person_detection():
    assert [agm.is_in_person(v) for v in ("In-person", "in person", "Online", "Virtual", "Hybrid", "")] \
        == [True, True, False, False, False, False]


def test_standalone_verification_only_reports(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),
        rsvp("2026/10/01 11:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca"),
    ])
    conn = students_db(("Ann", "Lee", 3161))
    rows = agm_verify.verify_file(form, conn.cursor(), tmp_path / "out")
    assert [(r["full_name"], r["status"]) for r in rows] == [("Ann Lee", "verified"), ("Cy Doe", "unverified")]
    with open(tmp_path / "out" / "agm_verification.csv", newline="", encoding="utf-8") as f:
        assert [r["status"] for r in csv.DictReader(f)] == ["verified", "unverified"]
    assert not (tmp_path / "out" / "agm_list_all.csv").exists()
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'agm_rsvp'").fetchone() == (0,)


def test_student_number_column_breaks_name_ties(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Di", "Park", "di@mail.utoronto.ca", extra="1001232222"),
        rsvp("2026/10/01 9:05:00 a.m. AST", "Di", "Park", "other@mail.utoronto.ca", extra="1001239999"),
    ], extra_header="Student Number")
    # "1001232222" -> truncated 3222; "1001239999" -> 3999
    conn = students_db(("Di", "Park", 1111), ("Di", "Park", 3222))
    result = agm.process_agm(form, conn, output_dir=tmp_path / "out")
    assert [(r["email"], r["match_method"]) for r in result["verified"]] == [("di@mail.utoronto.ca",
                                                                              "name + student number")]
    assert result["unverified"][0]["reason"] == "student number does not match"
