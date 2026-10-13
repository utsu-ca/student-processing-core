"""A complete application (the committee decision is recorded) is final: the student's other applications for that
term are not considered. Other terms are unaffected."""
import csv
import logging

from test_sap import LC_ROW, form_csv, students_db
from utsu_core import sap

FALL_1 = LC_ROW  # Ann Lee, Fall 2026
FALL_2 = {**LC_ROW, "Timestamp": "2026/10/20 9:00:00 a.m. AST"}  # a later submission, same student and term
WINTER = {**LC_ROW, "Timestamp": "2026/10/21 9:00:00 a.m. AST", "Application Semester": "Winter 2027"}


def decide(conn, tmp_path, uuid, name="d.csv"):
    path = tmp_path / name
    path.write_text(f"uuid,decision,awarded_amount\r\n{uuid},approved,650\r\n", encoding="utf-8")
    assert sap.record_decisions(conn, path)["recorded"] == 1


def table(conn):
    return conn.execute("SELECT terms, submission_ts, status, stage, batch FROM student_aid "
                        "ORDER BY submission_ts").fetchall()


def test_a_new_application_for_a_completed_term_is_not_considered(tmp_path, caplog):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    r1 = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1]), conn, output_dir=out, batch_size=5)
    done = r1["verified"][0]["uuid"]
    decide(conn, tmp_path, done)

    with caplog.at_level(logging.WARNING):
        r2 = sap.process_sap(form_csv(tmp_path / "2.csv", [FALL_1, FALL_2]), conn, output_dir=out, batch_size=5)
    assert [a["uuid"] for a in r2["verified"]] == [done] and r2["unverified"] == []
    assert r2["summary"]["not_considered"] == 1 and "not considered" in caplog.text
    assert table(conn) == [("Fall 2026", FALL_1["Timestamp"], "verified", "decided", 1),
                           ("Fall 2026", FALL_2["Timestamp"], "not_considered", None, None)]
    assert [a["kind"] for a in r2["alerts"]] == ["not_considered"]
    # nothing was verified, scored or batched for the new one
    assert r2["batches"][0][0]["uuid"] == done and len(r2["batches"][0]) == 1


def test_the_completed_application_is_never_replaced_even_by_a_newer_verified_one(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    done = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1]), conn, output_dir=out)["verified"][0]["uuid"]
    decide(conn, tmp_path, done)
    for n in range(3):  # re-running, with more and more submissions, changes nothing
        rows = [FALL_1] + [{**LC_ROW, "Timestamp": f"2026/10/{20 + i} 9:00:00 a.m. AST"} for i in range(n + 1)]
        r = sap.process_sap(form_csv(tmp_path / f"r{n}.csv", rows), conn, output_dir=out)
        assert [a["uuid"] for a in r["verified"]] == [done]
    assert sorted(s for (s,) in conn.execute("SELECT status FROM student_aid")) == [
        "not_considered", "not_considered", "not_considered", "verified"]
    assert conn.execute("SELECT stage, decision FROM student_aid WHERE status = 'verified'").fetchall() == [
        ("decided", "approved")]


def test_a_different_term_is_still_considered(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    done = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1]), conn, output_dir=out)["verified"][0]["uuid"]
    decide(conn, tmp_path, done)
    r = sap.process_sap(form_csv(tmp_path / "2.csv", [FALL_1, WINTER]), conn, output_dir=out)
    assert sorted(a["terms"] for a in r["verified"]) == ["Fall 2026", "Winter 2027"]
    assert r["summary"]["not_considered"] == 0 and r["alerts"] == []
    # the new term is an ordinary application; the Fall one keeps its stage
    assert dict(conn.execute("SELECT terms, stage FROM student_aid")) == {"Fall 2026": "decided", "Winter 2027": "scored"}


def test_an_application_that_was_already_in_progress_is_not_considered_once_the_other_is_complete(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    unverified = {**FALL_2, "Student Number": "1000000000"}  # fails verification, so it is not binding
    r1 = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1, unverified]), conn, output_dir=out)
    done = r1["verified"][0]["uuid"]
    assert dict(conn.execute("SELECT submission_ts, status FROM student_aid")) == {
        FALL_1["Timestamp"]: "verified", FALL_2["Timestamp"]: "non_binding"}
    decide(conn, tmp_path, done)
    sap.process_sap(form_csv(tmp_path / "2.csv", [FALL_1, unverified]), conn, output_dir=out)
    # a non-binding submission becomes "not considered" once the student's application is complete
    assert dict(conn.execute("SELECT submission_ts, status FROM student_aid")) == {
        FALL_1["Timestamp"]: "verified", FALL_2["Timestamp"]: "not_considered"}


def test_completion_blocks_new_applications_even_if_the_completed_row_left_the_export(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    done = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1]), conn, output_dir=out)["verified"][0]["uuid"]
    decide(conn, tmp_path, done)
    r = sap.process_sap(form_csv(tmp_path / "2.csv", [FALL_2]), conn, output_dir=out)  # the old row is gone
    assert r["verified"] == [] and r["summary"]["not_considered"] == 1
    assert conn.execute("SELECT stage FROM student_aid WHERE uuid = ?", (done,)).fetchone() == ("decided",)


def test_before_completion_the_newest_verified_application_still_wins(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    r = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1, FALL_2]), conn, output_dir=out)
    assert [a["submission_ts"] for a in r["verified"]] == [FALL_2["Timestamp"]] and r["summary"]["not_considered"] == 0
    assert dict(conn.execute("SELECT submission_ts, status FROM student_aid")) == {
        FALL_1["Timestamp"]: "superseded", FALL_2["Timestamp"]: "verified"}


def test_the_alerts_file_lists_what_a_person_has_to_look_at(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    done = sap.process_sap(form_csv(tmp_path / "1.csv", [FALL_1]), conn, output_dir=out)["verified"][0]["uuid"]
    decide(conn, tmp_path, done)
    sap.process_sap(form_csv(tmp_path / "2.csv", [FALL_1, FALL_2]), conn, output_dir=out)
    alerts = list(csv.DictReader(open(out / "alerts_sap.csv", encoding="utf-8-sig")))
    assert [(a["severity"], a["kind"], a["program"]) for a in alerts] == [("WARNING", "not_considered", "SABP")]
    assert alerts[0]["uuid"].startswith("application:sap:") and alerts[0]["student_uuid"].startswith("student:email:")
