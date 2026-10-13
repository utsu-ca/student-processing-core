"""SABP stages: scored -> in_review -> discuss/reconciled -> decided."""
import csv
import sqlite3

import pytest

from test_sap import AP_ROW, LC_ROW, fill_sheet, form_csv, students_db
from utsu_core import sap


def person(first, last, number, ts):
    return {**LC_ROW, "Timestamp": ts, "Student Number": number, "ACORN-registered First Name": first,
            "ACORN-registered Last Name": last, "UofT Email Address": f"{first.lower()}@mail.utoronto.ca"}


def stages(conn):
    return dict(conn.execute("SELECT submission_ts, COALESCE(stage, '-') FROM student_aid"))


def uuid_of(conn, ts):
    return conn.execute("SELECT uuid FROM student_aid WHERE submission_ts = ?", (ts,)).fetchone()[0]


def test_new_applications_are_scored_and_unverified_ones_have_no_stage(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    form = form_csv(tmp_path / "f.csv", [LC_ROW, AP_ROW])  # Bo Chan is not a student
    result = sap.process_sap(form, conn, output_dir=tmp_path / "out")
    assert set(stages(conn).values()) == {"scored", "-"}
    assert result["summary"]["stages"] == {"scored": 1}


def test_issuing_by_hand_freezes_the_batch(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000))
    out = tmp_path / "out"
    sap.process_sap(form_csv(tmp_path / "1.csv", [LC_ROW, AP_ROW]), conn, output_dir=out, batch_size=5)
    sheet = out / "sap_batches" / "batch_001.csv"
    before = sheet.read_bytes()

    assert sap.issue_batches(conn, "all") == [1]
    assert set(stages(conn).values()) == {"in_review"}
    assert sap.issue_batches(conn, "all") == []  # nothing left to issue

    export = [LC_ROW, AP_ROW, person("Cy", "Doe", "1005550000", "2026/10/02 9:00:00 a.m. AST")]
    r = sap.process_sap(form_csv(tmp_path / "2.csv", export), conn, output_dir=out, batch_size=5)
    assert {a["full_name"]: a["batch"] for a in r["verified"]} == {"Ann Lee": 1, "Bo Chan": 1, "Cy Doe": 2}
    assert sheet.read_bytes() == before  # the sheet that was handed out is exactly as issued
    assert r["summary"]["stages"] == {"in_review": 2, "scored": 1}


def test_issue_batches_by_number_and_bad_input(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    sap.process_sap(form_csv(tmp_path / "f.csv", [LC_ROW, AP_ROW]), conn, output_dir=tmp_path / "out", batch_size=1)
    assert sap.issue_batches(conn, "2, 7") == [2]  # 7 does not exist: warned, not an error
    assert sorted(stages(conn).values()) == ["in_review", "scored"]
    with pytest.raises(ValueError):
        sap.issue_batches(conn, "first")


def test_assigning_reviewers_issues_the_batch(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    r = sap.process_sap(form_csv(tmp_path / "f.csv", [LC_ROW, AP_ROW]), conn, output_dir=tmp_path / "out",
                        batch_size=2, reviewers="A,B")
    assert set(stages(conn).values()) == {"in_review"} and r["summary"]["stages"] == {"in_review": 2}


def test_merge_reconciles_and_decisions_are_final(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000))
    out = tmp_path / "out"
    export = [LC_ROW, AP_ROW, person("Cy", "Doe", "1005550000", "2026/10/02 9:00:00 a.m. AST")]
    form = form_csv(tmp_path / "f.csv", export)
    sap.process_sap(form, conn, output_dir=out, batch_size=3, reviewers="A,B")
    sheets = out / "sap_batches" / "by_reviewer"
    fill_sheet(sheets / "A" / "batch_001.csv", [3, 4, 4])
    fill_sheet(sheets / "B" / "batch_001.csv", [3, 0, None])  # Ann agreed, Bo far apart, Cy incomplete

    sap.merge_reviews(conn, out)
    assert stages(conn) == {"2026/09/30 10:39:14 a.m. AST": "reconciled", "2026/10/01 9:00:00 a.m. AST": "discuss",
                            "2026/10/02 9:00:00 a.m. AST": "in_review"}

    decisions = tmp_path / "decisions.csv"
    ann, bo = uuid_of(conn, "2026/09/30 10:39:14 a.m. AST"), uuid_of(conn, "2026/10/01 9:00:00 a.m. AST")
    with open(decisions, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["uuid", "decision", "awarded_amount", "notes"])
        writer.writerows([[ann, "approved", "$650", ""], [bo, "denied", "", "discussed"],
                          ["nope", "approved", "100", ""], [uuid_of(conn, "2026/10/02 9:00:00 a.m. AST"), "", "", ""],
                          [ann, "approved", "lots", ""]])
    result = sap.record_decisions(conn, decisions)
    assert result["recorded"] == 2 and len(result["problems"]) == 3
    assert conn.execute("SELECT decision, awarded_amount, stage FROM student_aid WHERE uuid = ?", (ann,)).fetchone() \
        == ("approved", 650.0, "decided")
    assert conn.execute("SELECT decision, awarded_amount FROM student_aid WHERE uuid = ?", (bo,)).fetchone() \
        == ("denied", None)

    # neither a re-merge nor reprocessing moves a decided application back; the undecided one follows its ratings
    fill_sheet(sheets / "B" / "batch_001.csv", [0, 0, 0])
    sap.merge_reviews(conn, out)
    sap.process_sap(form, conn, output_dir=out, batch_size=3, reviewers="A,B")
    assert sorted(stages(conn).values()) == ["decided", "decided", "discuss"]
    assert conn.execute("SELECT decision FROM student_aid WHERE uuid = ?", (ann,)).fetchone() == ("approved",)


def test_decisions_file_needs_the_right_columns(tmp_path):
    conn = students_db()
    bad = tmp_path / "bad.csv"
    bad.write_text("uuid,verdict\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="decision"):
        sap.record_decisions(conn, bad)


def test_old_databases_get_a_stage_column_filled_in(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE student_aid (uuid TEXT PRIMARY KEY, student_uuid TEXT, student_number TEXT NOT NULL,
            submission_ts TEXT, terms TEXT, status TEXT NOT NULL, aid_type TEXT NOT NULL, requested_amount REAL,
            wfni INTEGER, emergency INTEGER NOT NULL DEFAULT 0, flags TEXT, batch INTEGER, record INTEGER,
            awarded_amount REAL, decision TEXT, import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE sap_batch_reviewer (batch INTEGER NOT NULL, reviewer TEXT NOT NULL, PRIMARY KEY (batch, reviewer));
        INSERT INTO sap_batch_reviewer VALUES (1, 'A');
        INSERT INTO student_aid (uuid, student_number, status, aid_type, batch, decision) VALUES
            ('decided', '1', 'verified', 'LC', 1, 'approved'), ('reviewing', '2', 'verified', 'LC', 1, NULL),
            ('waiting', '3', 'verified', 'LC', 2, NULL), ('unverified', '4', 'unverified', 'LC', NULL, NULL);""")
    sap.ensure_schema(conn)
    sap.ensure_schema(conn)  # a second call changes nothing
    assert dict(conn.execute("SELECT uuid, stage FROM student_aid")) == {
        "decided": "decided", "reviewing": "in_review", "waiting": "scored", "unverified": None}
