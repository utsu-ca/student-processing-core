"""Every run reads the full export: nothing is skipped because an earlier run saw it, and an edit is a newer row."""
import logging
import sqlite3

from test_agm import form_csv as agm_form, rsvp, students_db
from test_sap import AP_ROW, LC_ROW, form_csv as sap_form
from utsu_core import agm, sap
from utsu_core.integrity import duplicate_verified
from utsu_core.selection import select_current, set_statuses


def person(first, last, number, ts):
    return {**LC_ROW, "Timestamp": ts, "Student Number": number, "ACORN-registered First Name": first,
            "ACORN-registered Last Name": last, "UofT Email Address": f"{first.lower()}@mail.utoronto.ca"}


def test_alerts_file_is_written_even_when_there_is_nothing_to_look_at(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=tmp_path / "out")
    text = (tmp_path / "out" / "alerts_sap.csv").read_text(encoding="utf-8-sig").strip()
    assert text == "severity,kind,program,uuid,student_uuid,detail"


def test_integrity_check_finds_two_verified_applications_for_one_student(caplog):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (uuid TEXT, student_uuid TEXT, terms TEXT, status TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", [
        ("1", "s1", "Fall", "verified"), ("2", "s1", "Winter", "verified"),   # two terms: fine
        ("3", "s2", "Fall", "verified"), ("4", "s2", "Fall", "verified"),     # two for one term: wrong
        ("5", "s3", "Fall", "verified"), ("6", "s3", "Fall", "superseded")])
    with caplog.at_level(logging.ERROR):
        assert duplicate_verified(conn, "t", "Test", ("student_uuid", "terms")) == [("s2", "Fall", 2)]
    assert "INTEGRITY" in caplog.text
    assert duplicate_verified(conn, "t", "Test") == [("s1", 2), ("s2", 2)]  # by student alone, two terms count too


def test_select_current_keeps_the_newest_verified_application_per_student(caplog):
    def row(uuid, ts, status, who="a@x.ca"):
        return {"uuid": uuid, "ts": ts, "status": status, "who": who}
    rows = [row("old-ok", 1, "verified"), row("bad-new", 2, "unverified"), row("other", 1, "unverified", "b@x.ca"),
            row("other-newer", 2, "unverified", "b@x.ca"), row("old-bad", 1, "unverified", "c@x.ca"),
            row("new-ok", 2, "verified", "c@x.ca"), row("gone", 0, "superseded", "a@x.ca")]
    with caplog.at_level(logging.WARNING):
        current, changes = select_current(rows, lambda r: r["who"], lambda r: r["ts"])
    assert [r["uuid"] for r in current] == ["old-ok", "other-newer", "new-ok"]
    # an unverified submission never replaces a verified one; with nothing verified the newest wins
    assert changes == {"bad-new": "non_binding", "other": "superseded", "old-bad": "superseded"}
    assert "non-binding" in caplog.text and "bad-new" in caplog.text
    assert next(r for r in rows if r["uuid"] == "gone")["status"] == "superseded"  # never reconsidered


def test_set_statuses_handles_many_ids():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (uuid TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, 'verified')", [(str(i),) for i in range(1200)])
    set_statuses(conn, "t", {str(i): "superseded" for i in range(1100)})
    assert conn.execute("SELECT COUNT(*) FROM t WHERE status = 'superseded'").fetchone() == (1100,)


def test_the_same_export_twice_gives_the_same_result(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    export = sap_form(tmp_path / "1.csv", [LC_ROW, AP_ROW])
    first = sap.process_sap(export, conn, output_dir=out, batch_size=5)
    second = sap.process_sap(export, conn, output_dir=out, batch_size=5)
    assert [(a["uuid"], a["batch"], a["record"]) for a in first["verified"]] == \
        [(a["uuid"], a["batch"], a["record"]) for a in second["verified"]]
    assert conn.execute("SELECT COUNT(*) FROM student_aid").fetchone() == (2,)


def test_sap_a_growing_export_keeps_batches_handed_to_reviewers_stable(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000), ("Di", "Fox", 6000))
    out = tmp_path / "out"
    first = [LC_ROW, AP_ROW]
    r1 = sap.process_sap(sap_form(tmp_path / "1.csv", first), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert [a["batch"] for a in r1["verified"]] == [1, 1]
    sheet = (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes()

    export = first + [person("Cy", "Doe", "1005550000", "2026/10/02 9:00:00 a.m. AST"),
                      person("Di", "Fox", "1006660000", "2026/10/03 9:00:00 a.m. AST")]
    r2 = sap.process_sap(sap_form(tmp_path / "2.csv", export), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    by_name = {a["full_name"]: (a["batch"], a["record"]) for a in r2["verified"]}
    # batch 1 was handed to reviewers, so it is untouched; the new applications start batch 2
    assert by_name["Ann Lee"] == (1, 1) and by_name["Bo Chan"] == (1, 2)
    assert by_name["Cy Doe"] == (2, 1) and by_name["Di Fox"] == (2, 2)
    assert (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes() == sheet
    assert conn.execute("SELECT COUNT(*) FROM student_aid").fetchone() == (4,)


def test_sap_new_applications_top_up_a_batch_not_yet_handed_out(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5)
    r = sap.process_sap(sap_form(tmp_path / "2.csv", [LC_ROW, AP_ROW]), conn, output_dir=out, batch_size=5)
    assert [(a["batch"], a["record"]) for a in r["verified"]] == [(1, 1), (1, 2)]


def test_sap_resubmission_supersedes_the_older_row_and_keeps_its_slot(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW, AP_ROW]), conn, output_dir=out, batch_size=5)
    resubmitted = {**LC_ROW, "Timestamp": "2026/10/05 9:00:00 a.m. AST"}
    r = sap.process_sap(sap_form(tmp_path / "2.csv", [LC_ROW, AP_ROW, resubmitted]), conn, output_dir=out,
                        batch_size=5)
    statuses = dict(conn.execute("SELECT submission_ts, status FROM student_aid"))
    assert statuses["2026/09/30 10:39:14 a.m. AST"] == "superseded"
    assert statuses["2026/10/05 9:00:00 a.m. AST"] == "verified"
    # the old slot (record 1) is not reused: Ann's resubmission gets the next record
    assert {a["full_name"]: a["record"] for a in r["verified"]} == {"Bo Chan": 2, "Ann Lee": 3}


def test_sap_an_edited_row_leaves_the_export_and_is_superseded_by_its_replacement(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    # Google Forms gave the edited response a new timestamp and the export no longer has the old row
    edited = {**LC_ROW, "Timestamp": "2026/10/05 9:00:00 a.m. AST", "LC Requested Amount": "900"}
    r = sap.process_sap(sap_form(tmp_path / "2.csv", [edited]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    assert dict(conn.execute("SELECT submission_ts, status FROM student_aid")) == {
        "2026/09/30 10:39:14 a.m. AST": "superseded", "2026/10/05 9:00:00 a.m. AST": "verified"}
    assert [a["kind"] for a in r["alerts"]] == [] and r["summary"]["missing_from_export"] == 0


def test_sap_a_verified_application_missing_from_the_export_is_kept_and_alerted(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW, AP_ROW]), conn, output_dir=out, batch_size=5,
                    reviewers="A,B")
    r = sap.process_sap(sap_form(tmp_path / "2.csv", [AP_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    assert conn.execute("SELECT status, stage, batch, record FROM student_aid WHERE aid_type = 'LC'").fetchone() \
        == ("verified", "in_review", 1, 1)
    assert [a["kind"] for a in r["alerts"]] == ["missing_from_export"] and r["summary"]["missing_from_export"] == 1


def test_sap_a_completed_application_that_left_the_export_stays_final(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5)
    (uuid,) = conn.execute("SELECT uuid FROM student_aid").fetchone()
    decisions = tmp_path / "d.csv"
    decisions.write_text(f"uuid,decision,awarded_amount\n{uuid},approved,650\n", encoding="utf-8")
    assert sap.record_decisions(conn, decisions)["recorded"] == 1

    later = {**LC_ROW, "Timestamp": "2026/10/05 9:00:00 a.m. AST"}
    sap.process_sap(sap_form(tmp_path / "2.csv", [later]), conn, output_dir=out, batch_size=5)
    assert dict(conn.execute("SELECT submission_ts, status FROM student_aid")) == {
        "2026/09/30 10:39:14 a.m. AST": "verified", "2026/10/05 9:00:00 a.m. AST": "not_considered"}


def test_agm_newer_rsvp_supersedes_the_older_one(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    old = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    new = rsvp("2026/10/02 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    agm.process_agm(agm_form(tmp_path / "1.csv", [old]), conn, output_dir=out)
    agm.process_agm(agm_form(tmp_path / "2.csv", [old, new]), conn, output_dir=out)
    assert sorted(r[0] for r in conn.execute("SELECT status FROM agm_rsvp")) == ["superseded", "verified"]


def test_agm_an_edited_rsvp_replaces_the_old_row_in_the_table(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    old = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    edited = rsvp("2026/10/03 9:00:00 a.m. AST", "Ann", "Lee", "ann.lee@mail.utoronto.ca")  # the email was corrected
    agm.process_agm(agm_form(tmp_path / "1.csv", [old]), conn, output_dir=out)
    r = agm.process_agm(agm_form(tmp_path / "2.csv", [edited]), conn, output_dir=out)
    assert conn.execute("SELECT submission_ts, status FROM agm_rsvp").fetchall() == \
        [("2026/10/03 9:00:00 a.m. AST", "verified")]
    assert len(r["verified"]) == 1 and r["alerts"] == []
