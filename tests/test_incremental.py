"""Re-running a program on an updated (cumulative) export only processes what is new."""
import csv
import logging
import sqlite3
from pathlib import Path

import pytest

from test_agm import form_csv as agm_form, rsvp, students_db
from test_sap import AP_ROW, LC_ROW, form_csv as sap_form
from utsu_core import agm, integrity, sap, senate
from utsu_core import verification
from utsu_core.incremental import apply_known, count_new, mark_superseded, select_current
from utsu_core.integrity import changed_after_processing, duplicate_verified
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


def test_sap_row_edited_after_processing_is_invalid_and_held(tmp_path, caplog):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5)
    before = conn.execute("SELECT requested_amount, wfni, status, stage, batch, record, source_hash "
                          "FROM student_aid").fetchall()

    edited = {**LC_ROW, "LC Requested Amount": "2500", "Liquidity": "$10,000+"}  # same timestamp and email
    with caplog.at_level(logging.ERROR):
        r = sap.process_sap(sap_form(tmp_path / "2.csv", [edited]), conn, output_dir=out, batch_size=5)
    assert any(x.levelname == "ERROR" and "INTEGRITY" in x.getMessage() for x in caplog.records)
    assert r["summary"]["changed_after_processing"] == 1 and r["summary"]["held_invalid"] == 1
    # it stays in progress with its stored result, but is invalid and held: out of the batches and the verified list
    assert conn.execute("SELECT requested_amount, wfni, status, stage, batch, record, source_hash FROM student_aid"
                        ).fetchall() == before
    assert conn.execute("SELECT invalid_reason FROM student_aid").fetchone() == ("row changed after processing",)
    assert r["verified"] == [] and len(r["held"]) == 1 and r["batches"] == []
    assert [a["kind"] for a in r["alerts"]] == ["invalid"] and "held" in r["alerts"][0]["detail"]
    alerts = list(csv.DictReader(open(out / "alerts_sap.csv", encoding="utf-8")))
    assert [a["kind"] for a in alerts] == ["invalid"] and alerts[0]["severity"] == "ERROR"
    assert "Ann" not in (out / "sap_verified.csv").read_text(encoding="utf-8")


def test_a_held_application_goes_no_further_until_a_person_resolves_it(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    (uuid,) = conn.execute("SELECT uuid FROM student_aid").fetchone()
    edited = sap_form(tmp_path / "2.csv", [{**LC_ROW, "LC Requested Amount": "900"}])
    sap.process_sap(edited, conn, output_dir=out, batch_size=5, reviewers="A,B")

    # reviewers' ratings, decisions and the merge all leave it alone while it is held
    sheets = out / "sap_batches" / "by_reviewer"
    sap_stage = lambda: conn.execute("SELECT stage FROM student_aid").fetchone()[0]
    assert sap_stage() == "in_review"
    path = tmp_path / "d.csv"
    path.write_text(f"uuid,decision,awarded_amount\r\n{uuid},approved,650\r\n", encoding="utf-8")
    result = sap.record_decisions(conn, path)
    assert result["recorded"] == 0 and "held for a person" in result["problems"][0]
    assert conn.execute("SELECT decision FROM student_aid").fetchone() == (None,) and sap_stage() == "in_review"
    assert sap.merge_reviews(conn, out)["rows"] == [] and sap_stage() == "in_review"
    assert sap.issue_batches(conn, "all") == [] and sap.mark_in_review(conn, [1]) == 0

    # a person looks at it and accepts the edit: the hold is lifted and the edited row becomes the baseline
    assert integrity.resolve_invalid(conn, [uuid, "not-held"]) == [uuid]
    r = sap.process_sap(edited, conn, output_dir=out, batch_size=5, reviewers="A,B")
    assert r["summary"]["held_invalid"] == 0 and [a["uuid"] for a in r["verified"]] == [uuid]
    assert conn.execute("SELECT requested_amount, invalid_reason FROM student_aid").fetchone() == (900.0, None)
    assert sap.record_decisions(conn, path)["recorded"] == 1 and sap_stage() == "decided"


def test_putting_the_row_back_releases_the_hold(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out)
    sap.process_sap(sap_form(tmp_path / "2.csv", [{**LC_ROW, "LC Requested Amount": "900"}]), conn, output_dir=out)
    assert conn.execute("SELECT invalid_reason FROM student_aid").fetchone()[0]
    r = sap.process_sap(sap_form(tmp_path / "3.csv", [LC_ROW]), conn, output_dir=out)
    assert conn.execute("SELECT invalid_reason FROM student_aid").fetchone() == (None,)
    assert len(r["verified"]) == 1 and r["alerts"] == []


def test_senate_and_agm_hold_invalid_applications_out_of_their_lists(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    ts = "2026/10/01 9:00:00 a.m. AST"
    good = senate_row(ts, "Ann Lee", "1001231617")
    senate.verify_senators(senate_form(tmp_path / "1.csv", [good]), conn.cursor(), output_dir=tmp_path / "out")
    edited = senate_row(ts, "Ann Lee", "1001231617")
    edited[1] = "someone-else@gmail.com"  # personal email edited after processing
    r = senate.verify_senators(senate_form(tmp_path / "2.csv", [edited]), conn.cursor(), output_dir=tmp_path / "out")
    assert r["verified"] == [] and len(r["held"]) == 1 and r["held"][0]["flags"] == "changed_after_processing;invalid"
    assert [a["kind"] for a in r["alerts"]] == ["invalid"]
    assert len(list(csv.DictReader(open(tmp_path / "out" / "verified.csv", encoding="utf-8")))) == 0

    base = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
    agm.process_agm(agm_form(tmp_path / "a1.csv", [base]), conn, output_dir=tmp_path / "out")
    changed = list(base)
    changed[7] = "In person"  # meeting preference edited after processing
    a = agm.process_agm(agm_form(tmp_path / "a2.csv", [changed]), conn, output_dir=tmp_path / "out")
    assert a["verified"] == [] and len(a["held"]) == 1 and a["summary"]["held_invalid"] == 1
    assert len(list(csv.DictReader(open(tmp_path / "out" / "agm_list_all.csv", encoding="utf-8")))) == 0


def test_alerts_file_is_written_even_when_there_is_nothing_to_look_at(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=tmp_path / "out")
    text = (tmp_path / "out" / "alerts_sap.csv").read_text(encoding="utf-8").strip()
    assert text == "severity,kind,program,uuid,student_uuid,detail"


def test_apply_known_reprocesses_edited_unverified_rows_but_never_locked_ones(caplog):
    def row(uuid, digest):
        return {"uuid": uuid, "source_hash": digest}
    stored = lambda status, digest: {"status": status, "source_hash": digest, "uoft_uuid": None,
                                     "verification_level": None}
    known = {"ok": stored("verified", "h"), "ok-edited": stored("verified", "old"),
             "bad": stored("unverified", "h"), "bad-edited": stored("unverified", "old"),
             "nb-edited": stored("non_binding", "old"), "old": stored("superseded", "old"),
             "ignored": stored("not_considered", "old")}
    current = [row("ok", "h"), row("ok-edited", "new"), row("bad", "h"), row("bad-edited", "new"),
               row("nb-edited", "new"), row("old", "new"), row("ignored", "new"), row("brand-new", "h")]
    with caplog.at_level(logging.INFO):
        # a new row, and an unverified or non-binding row that was edited, are processed; nothing else is
        assert [x["uuid"] for x in apply_known(current, known)] == ["bad-edited", "nb-edited", "brand-new"]
    assert current[1]["status"] == "verified" and current[1]["source_hash"] == "new"  # stored outcome, own hash
    assert count_new(current, known) == 1 and "edited since it was processed" in caplog.text
    # after new student data everything is processed again, except a locked row that was edited
    assert [x["uuid"] for x in apply_known(current, known, reverify=True)] == [
        "ok", "bad", "bad-edited", "nb-edited", "ignored", "brand-new"]


def test_integrity_check_reports_edits_to_locked_rows_only(caplog):
    rows = [{"uuid": "a", "source_hash": "new", "flags": "x"}, {"uuid": "b", "source_hash": "same"},
            {"uuid": "c", "source_hash": "anything"}, {"uuid": "d", "source_hash": "new"},
            {"uuid": "e", "source_hash": "new"}]
    known = {"a": {"status": "verified", "source_hash": "old"}, "b": {"status": "unverified", "source_hash": "same"},
             "c": {"status": "verified", "source_hash": None},  # no stored hash (an older database): not judged
             "d": {"status": "unverified", "source_hash": "old"},  # not locked: it is simply processed again
             "e": {"status": "superseded", "source_hash": "old"}}
    with caplog.at_level(logging.ERROR):
        assert changed_after_processing(rows, known, "Test") == ["a", "e"]
    assert rows[0]["flags"] == "x;changed_after_processing" and "flags" not in rows[1] and "flags" not in rows[3]
    assert [r.get("invalid_reason") for r in rows] == ["row changed after processing", None, None, None,
                                                       "row changed after processing"]
    assert [r.levelname for r in caplog.records] == ["ERROR", "ERROR"]
    assert "INTEGRITY" in caplog.text and "NOT processed again" in caplog.text and "invalid" in caplog.text


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
    assert r1["summary"]["new_submissions_this_run"] == 2 and [a["batch"] for a in r1["verified"]] == [1, 1]
    sheet = (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes()

    export = first + [person("Cy", "Doe", "1005550000", "2026/10/02 9:00:00 a.m. AST"),
                      person("Di", "Fox", "1006660000", "2026/10/03 9:00:00 a.m. AST")]
    r2 = sap.process_sap(sap_form(tmp_path / "2.csv", export), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert len(seen[-1]) == 2 and r2["summary"]["new_submissions_this_run"] == 2  # only the two new applications were checked
    by_name = {a["full_name"]: (a["batch"], a["record"]) for a in r2["verified"]}
    # batch 1 was handed to reviewers, so it is untouched; the new applications start batch 2
    assert by_name["Ann Lee"] == (1, 1) and by_name["Bo Chan"] == (1, 2)
    assert by_name["Cy Doe"] == (2, 1) and by_name["Di Fox"] == (2, 2)
    assert (out / "sap_batches" / "by_reviewer" / r1["assignments"][1][0] / "batch_001.csv").read_bytes() == sheet
    assert conn.execute("SELECT COUNT(*) FROM student_aid").fetchone() == (4,)

    # an unchanged export has nothing new
    r3 = sap.process_sap(sap_form(tmp_path / "3.csv", export), conn, output_dir=out, batch_size=2,
                         reviewers="Ann,Bo,Cy")
    assert seen[-1] == [] and r3["summary"]["new_submissions_this_run"] == 0
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


def test_agm_update_verifies_only_new_rsvps(tmp_path, monkeypatch):
    seen = spy_on_verification(monkeypatch, agm, "verify_rsvps")
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    first = [rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca"),
             rsvp("2026/10/01 10:00:00 a.m. AST", "Cy", "Doe", "cy@mail.utoronto.ca")]
    agm.process_agm(agm_form(tmp_path / "1.csv", first), conn, output_dir=out)

    second = first + [rsvp("2026/10/02 9:00:00 a.m. AST", "Bo", "Chan", "bo@mail.utoronto.ca")]
    r = agm.process_agm(agm_form(tmp_path / "2.csv", second), conn, output_dir=out)
    # Ann and Cy were processed before (Cy unverified), so neither is processed again; only Bo is new
    assert seen[-1] == ["Bo Chan"] and r["summary"]["new_submissions_this_run"] == 1
    # the lists and the unverified reasons still cover the whole export
    assert sorted(x["full_name"] for x in r["verified"]) == ["Ann Lee", "Bo Chan"]
    assert [(x["full_name"], x["reason"]) for x in r["unverified"]] == [("Cy Doe", "name not found")]
    assert conn.execute("SELECT COUNT(*) FROM agm_rsvp").fetchone() == (3,)


def test_an_unverified_row_corrected_in_place_is_processed_again(tmp_path, caplog):
    conn = students_db(("Cy", "Doe", 5000))
    out = tmp_path / "out"
    ts = "2026/10/01 10:00:00 a.m. AST"
    r1 = agm.process_agm(agm_form(tmp_path / "1.csv", [rsvp(ts, "Cy", "Dow", "cy@mail.utoronto.ca")]), conn,
                         output_dir=out)
    assert [x["reason"] for x in r1["unverified"]] == ["name not found"]
    uuid = r1["unverified"][0]["uuid"]

    # an unchanged export does not retry it
    r2 = agm.process_agm(agm_form(tmp_path / "2.csv", [rsvp(ts, "Cy", "Dow", "cy@mail.utoronto.ca")]), conn,
                         output_dir=out)
    assert [x["reason"] for x in r2["unverified"]] == ["name not found"]

    # the row is corrected in place (same timestamp and email, so the same uuid): it is processed again
    with caplog.at_level(logging.INFO):
        r3 = agm.process_agm(agm_form(tmp_path / "3.csv", [rsvp(ts, "Cy", "Doe", "cy@mail.utoronto.ca")]), conn,
                             output_dir=out)
    assert [(x["uuid"], x["full_name"]) for x in r3["verified"]] == [(uuid, "Cy Doe")] and r3["unverified"] == []
    assert "edited since it was processed" in caplog.text and r3["summary"]["changed_after_processing"] == 0
    assert conn.execute("SELECT status, uoft_uuid, invalid_reason FROM agm_rsvp").fetchall() == [("verified", "u0", None)]

    # now it is verified, so it is immutable: a further edit makes it invalid and held (see the SABP tests)
    again = agm.process_agm(agm_form(tmp_path / "4.csv", [rsvp(ts, "Cy", "Doe", "cy@mail.utoronto.ca", dietary="x")]),
                            conn, output_dir=out)
    assert again["verified"] == [] and len(again["held"]) == 1


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


def test_senate_update_verifies_only_new_applications(tmp_path, monkeypatch):
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
    assert seen[-1] == ["Bo Chan"] and r2["new"] == 1  # Ann and the unverified Cy were processed before
    assert sorted(a["full_name"] for a in r2["verified"]) == ["Ann Lee", "Bo Chan"]
    assert [a["full_name"] for a in r2["unverified"]] == ["Cy Doe"]
    assert len(list(csv.DictReader(open(out / "verified.csv", encoding="utf-8")))) == 2
    assert sorted(conn.execute("SELECT status, uoft_uuid, verification_level FROM senate_application").fetchall(),
                  key=str) == [("unverified", None, None), ("verified", "u0", 2), ("verified", "u1", 2)]

    r3 = senate.verify_senators(senate_form(tmp_path / "3.csv", second), conn.cursor(), output_dir=out)
    assert r3["new"] == 0 and seen[-1] == [] and r3["changed"] == []


def test_mark_superseded_handles_many_ids():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (uuid TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, 'verified')", [(str(i),) for i in range(1200)])
    mark_superseded(conn, "t", [str(i) for i in range(1100)])
    assert conn.execute("SELECT COUNT(*) FROM t WHERE status = 'superseded'").fetchone() == (1100,)
