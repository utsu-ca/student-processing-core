"""Re-running a program on an updated (cumulative) export only processes what is new."""
import csv
import sqlite3
from pathlib import Path

import pytest

from test_agm import form_csv as agm_form, rsvp, students_db
from test_sap import AP_ROW, LC_ROW, form_csv as sap_form
from utsu_core import agm, sap, senate
from utsu_core import verification
from utsu_core.incremental import apply_known, count_new, mark_superseded
from utsu_std.utils import absfile


def spy_on_verification(monkeypatch, module, name):
    """Record the full names each verification call is asked about."""
    seen = []
    original = getattr(module, name)

    def wrapper(apps, *args, **kwargs):
        seen.append([a["full_name"] for a in apps])
        return original(apps, *args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return seen


def test_apply_known_rechecks_new_unverified_and_stale_rows_and_copies_stored_outcome():
    current = [{"uuid": "a"}, {"uuid": "b"}, {"uuid": "c"}, {"uuid": "d"}]
    known = {"a": {"status": "verified", "uoft_uuid": "s1", "verification_level": 2},
             "c": {"status": "superseded", "uoft_uuid": None, "verification_level": None},
             "d": {"status": "unverified", "uoft_uuid": None, "verification_level": None}}
    assert [x["uuid"] for x in apply_known(current, known)] == ["b", "c", "d"]
    assert current[0] == {"uuid": "a", "status": "verified", "uoft_uuid": "s1", "verification_level": 2}
    assert len(apply_known(current, known, reverify=True)) == 4
    assert count_new(current, known) == 1


def person(first, last, number, ts):
    return {**LC_ROW, "Timestamp": ts, "Student Number": number, "ACORN-registered First Name": first,
            "ACORN-registered Last Name": last, "UofT Email Address": f"{first.lower()}@mail.utoronto.ca"}


def test_sap_update_verifies_only_new_and_keeps_batches_stable(tmp_path, monkeypatch):
    seen = spy_on_verification(monkeypatch, sap, "verify_applications")
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000), ("Di", "Fox", 6000))
    out = tmp_path / "out"
    first = [LC_ROW, AP_ROW]
    r1 = sap.process_sap(sap_form(tmp_path / "1.csv", first), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert r1["summary"]["new"] == 2 and [a["batch"] for a in r1["verified"]] == [1, 1]
    sheet = (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes()

    export = first + [person("Cy", "Doe", "1005550000", "2026/10/02 9:00:00 a.m. AST"),
                      person("Di", "Fox", "1006660000", "2026/10/03 9:00:00 a.m. AST")]
    r2 = sap.process_sap(sap_form(tmp_path / "2.csv", export), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert len(seen[-1]) == 2 and r2["summary"]["new"] == 2  # only the two new applications were checked
    by_name = {a["full_name"]: (a["batch"], a["record"]) for a in r2["verified"]}
    # batch 1 was handed to reviewers, so it is untouched; the new applications start batch 2
    assert by_name["Ann Lee"] == (1, 1) and by_name["Bo Chan"] == (1, 2)
    assert by_name["Cy Doe"] == (2, 1) and by_name["Di Fox"] == (2, 2)
    assert (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes() == sheet
    assert conn.execute("SELECT COUNT(*) FROM student_aid").fetchone() == (4,)

    # an unchanged export has nothing new
    r3 = sap.process_sap(sap_form(tmp_path / "3.csv", export), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert seen[-1] == [] and r3["summary"]["new"] == 0
    assert {a["full_name"]: (a["batch"], a["record"]) for a in r3["verified"]} == by_name


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


def test_sap_reverify_checks_everything_again(tmp_path, monkeypatch):
    seen = spy_on_verification(monkeypatch, sap, "verify_applications")
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    export = sap_form(tmp_path / "1.csv", [LC_ROW, AP_ROW])
    sap.process_sap(export, conn, output_dir=tmp_path / "out")
    sap.process_sap(export, conn, output_dir=tmp_path / "out", reverify=True)
    assert len(seen[-1]) == 2


def test_agm_update_verifies_new_rsvps_and_retries_unverified_ones(tmp_path, monkeypatch):
    seen = spy_on_verification(monkeypatch, agm, "verify_rsvps")
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    first = [rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),
             rsvp("2026/10/01 10:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca")]
    agm.process_agm(agm_form(tmp_path / "1.csv", first), conn, output_dir=out)

    second = first + [rsvp("2026/10/02 9:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca")]
    r = agm.process_agm(agm_form(tmp_path / "2.csv", second), conn, output_dir=out)
    # Ann was verified before, so she is not checked again; Cy (unverified) is retried; Bo is new
    assert seen[-1] == ["Cy Doe", "Bo Chan"] and r["summary"]["new"] == 1
    # the lists and the unverified reasons still cover the whole export
    assert sorted(x["full_name"] for x in r["verified"]) == ["Ann Lee", "Bo Chan"]
    assert [(x["full_name"], x["reason"]) for x in r["unverified"]] == [("Cy Doe", "name not found")]
    assert conn.execute("SELECT COUNT(*) FROM agm_rsvp").fetchone() == (3,)


def test_agm_corrected_row_is_verified_on_the_next_run(tmp_path):
    conn = students_db(("Cy", "Doe", 5000))
    out = tmp_path / "out"
    typo = rsvp("2026/10/01 10:00:00 a.m. AST", "Cy", "Dow", "cy@mail.utoronto.ca")
    r1 = agm.process_agm(agm_form(tmp_path / "1.csv", [typo]), conn, output_dir=out)
    assert [x["reason"] for x in r1["unverified"]] == ["name not found"]

    # the row is fixed in place (same timestamp and email, so the same uuid)
    fixed = rsvp("2026/10/01 10:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca")
    r2 = agm.process_agm(agm_form(tmp_path / "2.csv", [fixed]), conn, output_dir=out)
    assert [x["full_name"] for x in r2["verified"]] == ["Cy Doe"] and r2["summary"]["new"] == 0
    assert conn.execute("SELECT status, uoft_uuid, verification_level FROM agm_rsvp").fetchall() == [
        ("verified", "u0", 1)]


def test_agm_newer_rsvp_supersedes_the_older_one(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    old = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    new = rsvp("2026/10/02 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    agm.process_agm(agm_form(tmp_path / "1.csv", [old]), conn, output_dir=out)
    agm.process_agm(agm_form(tmp_path / "2.csv", [old, new]), conn, output_dir=out)
    assert sorted(r[0] for r in conn.execute("SELECT status FROM agm_rsvp")) == ["superseded", "verified"]


def senate_form(path, rows):
    keys = list(verification.construct_lookup_tables(Path(absfile("data/conversion_table_senate.csv")))[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(keys)
        for row in rows:
            writer.writerow(row)
    return path


def senate_row(ts, name, number):
    # Timestamp, Username, name, Student Number, UofT Email, Divisional Code, delegated
    return [ts, "p@gmail.com", name, number, f"{name.split()[0].lower()}@mail.utoronto.ca", "UC", "No"]


def test_senate_update_verifies_new_and_unverified_applications_only(tmp_path, monkeypatch):
    seen = []
    original = senate.match_students
    monkeypatch.setattr(senate, "match_students",
                        lambda people, *a, **k: seen.append([p["full_name"] for p in people]) or original(people, *a, **k))
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    first = [senate_row("2026/10/01 9:00:00 a.m. AST", "Ann Lee", "1001231617"),
             senate_row("2026/10/01 10:00:00 a.m. AST", "Cy Doe", "1005550000")]
    r1 = senate.verify_senators(senate_form(tmp_path / "1.csv", first), conn.cursor(), output_dir=out)
    assert r1["new"] == 2 and [a["full_name"] for a in r1["verified"]] == ["Ann Lee"]

    second = first + [senate_row("2026/10/02 9:00:00 a.m. AST", "Bo Chan", "1009991111")]
    r2 = senate.verify_senators(senate_form(tmp_path / "2.csv", second), conn.cursor(), output_dir=out)
    assert seen[-1] == ["Cy Doe", "Bo Chan"] and r2["new"] == 1  # Ann was verified before: not checked again
    assert sorted(a["full_name"] for a in r2["verified"]) == ["Ann Lee", "Bo Chan"]
    assert [a["full_name"] for a in r2["unverified"]] == ["Cy Doe"]
    assert len(list(csv.DictReader(open(out / "verified.csv", encoding="utf-8")))) == 2
    assert sorted(conn.execute("SELECT status, uoft_uuid, verification_level FROM senate_application").fetchall(),
                  key=str) == [("unverified", None, None), ("verified", "u0", 2), ("verified", "u1", 2)]

    r3 = senate.verify_senators(senate_form(tmp_path / "3.csv", second), conn.cursor(), output_dir=out)
    assert r3["new"] == 0 and seen[-1] == ["Cy Doe"]


def test_mark_superseded_handles_many_ids():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (uuid TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, 'verified')", [(str(i),) for i in range(1200)])
    mark_superseded(conn, "t", [str(i) for i in range(1100)])
    assert conn.execute("SELECT COUNT(*) FROM t WHERE status = 'superseded'").fetchone() == (1100,)
