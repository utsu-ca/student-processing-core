"""Fuzzy first-name matching (undisclosed middle names), its flag and the AGM reconciliation file."""
import csv

from test_agm import form_csv, rsvp, students_db
from utsu_core import verification
from utsu_programs.agm import program as agm


def test_first_name_similarity_allows_missing_middle_names_and_typos():
    sim = verification.first_name_similarity
    assert sim("Ann", "Ann Marie") == sim("Ann Marie", "Ann") == sim("Ann M", "Ann Marie") == 1.0
    assert sim("Jon", "John") == 0.86
    assert sim("Ann Lynn", "Ann Marie") == 0.0 and sim("Bo", "Ann") == 0.0 and sim("", "Ann") == 0.0


def test_a_name_that_is_not_exact_is_matched_on_last_name_and_a_close_first_name():
    conn = students_db(("Ann Marie", "Lee", 3161), ("Bo", "De La Cruz", 9111), ("John", "Doe", 1111))
    people = [{"full_name": "Ann Lee"}, {"full_name": "Bo De La Cruz"}, {"full_name": "Jon Doe"},
              {"full_name": "Zed Lee"}, {"full_name": "Ann Smith"}, {"full_name": "Ann Lee", "id": "1009999999"}]
    got = verification.match_students(people, conn.cursor())
    assert (got[0].uoft_uuid, got[0].method, got[0].similarity) == ("u0", "fuzzy name", 1.0)
    assert got[0].fuzzy_flag == "fuzzy_name_match=1.00"
    assert got[1].method == "name only" and got[1].fuzzy_flag == ""  # exact names are not fuzzy
    assert (got[2].uoft_uuid, got[2].similarity) == ("u2", 0.86)
    assert got[3].reason == "name not found" and got[4].reason == "name not found"  # the last name must be exact
    assert got[5].reason == "student number does not match" and got[5].candidates[0].uoft_uuid == "u0"


def test_the_other_factors_still_have_to_agree_with_a_fuzzy_name():
    conn = students_db(("Ann Marie", "Lee", 3161))
    got = verification.match_students([{"full_name": "Ann Lee", "id": "1001231617"},
                                       {"full_name": "Ann Lee", "id": "1001231617", "division": "UC"}], conn.cursor())
    assert [(m.level, m.method) for m in got] == [(2, "fuzzy name + student number"),
                                                  (3, "fuzzy name + student number + division")]


def test_two_close_students_are_ambiguous_and_the_best_three_are_kept():
    conn = students_db(("Ann Marie", "Lee", 1), ("Ann Rose", "Lee", 2), ("Anne", "Lee", 3), ("Anna", "Lee", 4))
    got, = verification.match_students([{"full_name": "Ann Lee"}], conn.cursor())
    assert not got.verified and got.reason.startswith("name shared by 4")
    assert len(got.candidates) == 3 and [c.student_number for c in got.candidates][0] == "xxxxx0001x"


def test_agm_writes_a_fuzzy_review_file_with_flags(tmp_path):
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),
        rsvp("2026/10/01 10:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca"),
        rsvp("2026/10/01 11:00:00 a.m. AST", "Di", "Park", "di@mail.utoronto.ca"),
    ])
    conn = students_db(("Ann Marie", "Lee", 3161), ("Bo", "Chan", 9111), ("Di Anne", "Park", 1111),
                       ("Di Rose", "Park", 2222))
    out = tmp_path / "out"
    result = agm.process_agm(form, conn, output_dir=out)

    ann = next(r for r in result["verified"] if r["full_name"] == "Ann Lee")
    assert ann["match_method"] == "fuzzy name" and "fuzzy_name_match=1.00" in ann["flags"]
    assert "fuzzy" not in next(r for r in result["verified"] if r["full_name"] == "Bo Chan")["flags"]
    assert result["summary"]["fuzzy_name_reviews"] == 2 and result["summary"]["flags"]["fuzzy_name_match"] == 1

    with open(out / "agm_fuzzy_review.csv", newline="", encoding="utf-8-sig") as f:
        rows = {r["uuid"]: r for r in csv.DictReader(f)}
    assert len(rows) == 2
    by_status = {r["status"]: r for r in rows.values()}
    verified, undecided = by_status["verified"], by_status["unverified"]
    assert verified["match1_name"] == "Ann Marie Lee" and verified["match1_selected"] == "SELECTED"
    assert verified["match1_student_number"] == "xxxxx3161x" and verified["match2_name"] == ""
    assert undecided["uoft_uuid"] == "" and undecided["reason"].startswith("name shared by 2")
    assert {undecided["match1_selected"], undecided["match2_selected"]} == {""}
    assert verified["reviewer_decision"] == "" and "reviewer_notes" in verified
