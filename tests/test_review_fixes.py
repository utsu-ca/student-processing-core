"""Regression tests for the code-review fixes (held originals, sender reconnect, alerts, redaction, roster import)."""
import csv
import smtplib
import sqlite3
from pathlib import Path

import pytest

from test_agm import form_csv as agm_form, rsvp, students_db
from test_mail import SETTINGS
from test_sap import LC_ROW, fill_sheet, form_csv as sap_form, students_db as sap_students
from utsu_core import agm, importing_data, integrity, sap
from utsu_mail.campaign import Prepared, send_all
from utsu_mail.notices import DIVISION_FIELD, NUMBER_FIELD, diagnose
from utsu_mail.templates import RenderedMessage
from utsu_mail.transport import SmtpSender
from utsu_std.utils import absfile

ANN = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann@mail.utoronto.ca")
ANN_FIXED = rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "ann.lee@mail.utoronto.ca")


def agm_state(conn):
    return conn.execute("SELECT status, invalid_reason FROM agm_rsvp ORDER BY uuid").fetchall()


def test_an_application_whose_row_left_the_export_is_held_then_dropped(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    agm.process_agm(agm_form(tmp_path / "1.csv", [ANN]), conn, output_dir=out)
    (original,) = [u for (u,) in conn.execute("SELECT uuid FROM agm_rsvp")]

    # the email is corrected in the form's sheet: a new application, and the original is no longer in the export
    fixed = agm_form(tmp_path / "2.csv", [ANN_FIXED])
    result = agm.process_agm(fixed, conn, output_dir=out)
    assert result["summary"]["missing_from_export"] == 1
    held = dict(conn.execute("SELECT uuid, invalid_reason FROM agm_rsvp"))
    assert held[original] == integrity.INVALID_MISSING and sum(v is None for v in held.values()) == 1
    assert [a["kind"] for a in result["alerts"]] == ["invalid"] and result["alerts"][0]["uuid"] == original

    # it is reported every run until resolved, not only the first time
    assert [a["uuid"] for a in agm.process_agm(fixed, conn, output_dir=out)["alerts"]] == [original]

    # a person drops it: it is superseded and no longer raised
    assert integrity.resolve_invalid(conn, [original]) == [original]
    assert conn.execute("SELECT status, invalid_reason FROM agm_rsvp WHERE uuid = ?", (original,)).fetchone() \
        == ("superseded", None)
    assert agm.process_agm(fixed, conn, output_dir=out)["alerts"] == []


def test_a_held_row_that_comes_back_is_released(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    agm.process_agm(agm_form(tmp_path / "1.csv", [ANN]), conn, output_dir=out)
    agm.process_agm(agm_form(tmp_path / "2.csv", [ANN_FIXED]), conn, output_dir=out)
    assert any(reason == integrity.INVALID_MISSING for _, reason in agm_state(conn))

    agm.process_agm(agm_form(tmp_path / "3.csv", [ANN, ANN_FIXED]), conn, output_dir=out)
    assert all(reason is None for _, reason in agm_state(conn))


def test_an_edited_superseded_row_is_alerted(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    old = {**LC_ROW, "Timestamp": "2026/09/01 10:00:00 a.m. AST"}
    new = {**LC_ROW, "Timestamp": "2026/09/02 10:00:00 a.m. AST"}
    sap.process_sap(sap_form(tmp_path / "1.csv", [old, new]), conn, output_dir=out, batch_size=5)
    result = sap.process_sap(sap_form(tmp_path / "2.csv", [{**old, "LC Requested Amount": "2000"}, new]), conn,
                             output_dir=out, batch_size=5)
    assert [a["kind"] for a in result["alerts"]] == ["invalid"]


class DroppedClient:
    def send_message(self, msg):
        raise smtplib.SMTPServerDisconnected("dropped")

    def quit(self):
        raise smtplib.SMTPServerDisconnected("gone")

    def close(self):
        pass


def test_send_survives_a_failed_reconnect(tmp_path):
    class Sender(SmtpSender):
        def __init__(self, settings):
            self.settings, self._client, self.connects = settings, DroppedClient(), 0

        def __enter__(self):
            return self

        def _connect(self):
            self.connects += 1
            raise OSError("network down")

    import utsu_mail.campaign as campaign
    original, campaign.SmtpSender = campaign.SmtpSender, Sender
    try:
        items = [Prepared(f"x{i}@b.ca", RenderedMessage("s", "t")) for i in range(3)]
        counts = send_all(SETTINGS, items, tmp_path / "log.csv", sleep=lambda _: None)
    finally:
        campaign.SmtpSender = original
    assert counts == {"sent": 0, "failed": 3, "remaining": 0}


def test_redact_finds_short_and_accented_names():
    assert sap.redact("Li said Jos\u00e9 Wu, and JOSE", ["Li", "Jose Wu"]) == "[name] said [name] [name], and [name]"
    assert sap.redact("Anna lives in Liverpool", ["Ann", "Li"]) == "Anna lives in Liverpool"


def test_roster_import_skips_blank_lines_and_leaves_no_copy(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text("First Name,Last Name,Truncated Student Number,Faculty,Division\n"
                      "Ann,Lee,xxxxx3971x,ARTSC,UC\n,,,,\n\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    assert importing_data.import_uoft_data(conn, roster)
    assert conn.execute("SELECT full_name, trunc_id FROM uoft_data").fetchall() == [("Ann Lee", 3971)]
    assert not (tmp_path / "roster_processed.csv").exists()


def test_roster_row_with_too_few_columns_is_an_error(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text("First Name,Last Name,Truncated Student Number,Faculty,Division\nAnn,Lee\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Line 2"):
        importing_data.preprocess_uoft_csv(roster)
    assert not (tmp_path / "roster_processed.csv").exists()


def test_diagnose_asks_for_what_a_shared_name_still_needs():
    conn = sqlite3.connect(":memory:")
    conn.executescript(Path(absfile("src/schema/uoft_data.sql")).read_text(encoding="utf-8"))
    conn.executemany("INSERT INTO uoft_data (uuid, trunc_id, first_name, last_name, full_name, division, faculty) "
                     "VALUES (?, 1111, 'Sam', 'Lee', 'Sam Lee', ?, 'ARTSC')", [("a", "UC"), ("b", "UC")])
    cur = conn.cursor()
    # the number is given and still shared: asking for the number again would be wrong
    assert diagnose({"full_name": "Sam Lee", "id": "1000011119", "email": "s@x.ca"}, cur)[1] == [DIVISION_FIELD]
    assert diagnose({"full_name": "Sam Lee", "id": "1000011119", "division": "UC", "email": "s@x.ca"}, cur)[1] \
        == [NUMBER_FIELD, DIVISION_FIELD]
    assert diagnose({"full_name": "Sam Lee", "email": "s@x.ca"}, cur)[1] == [NUMBER_FIELD]


def test_a_rating_taken_back_is_removed(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    sheets = sorted((out / "sap_batches" / "by_reviewer").glob("*/batch_001.csv"))
    for sheet in sheets:
        fill_sheet(sheet, [3])
    sap.merge_reviews(conn, out)
    assert conn.execute("SELECT COUNT(*) FROM sap_review").fetchone() == (2,)

    fill_sheet(sheets[0], [None])
    result = sap.merge_reviews(conn, out)
    assert conn.execute("SELECT reviewer FROM sap_review").fetchall() == [(sheets[1].parent.name,)]
    assert result["rows"][0]["status"] == "incomplete"


def test_a_rating_of_infinity_is_a_problem_not_a_crash(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    sheet = next((out / "sap_batches" / "by_reviewer").glob("*/batch_001.csv"))
    fill_sheet(sheet, ["inf"])
    assert "invalid need level" in sap.merge_reviews(conn, out)["problems"][0]


def test_program_all_skips_a_missing_senate_export(tmp_path, caplog):
    from utsu_core.main import main

    roster = tmp_path / "roster.csv"
    roster.write_text("First Name,Last Name,Truncated Student Number,Faculty,Division\nAnn,Lee,xxxxx3971x,ARTSC,UC\n",
                      encoding="utf-8")
    cfg = {"db": str(tmp_path / "db.sqlite"), "no_backup": True, "import_uoft_data": str(roster), "program": "all",
           "verify_student": str(tmp_path / "none.csv"), "output_dir": str(tmp_path / "out")}
    main(cfg)  # no Senate export: Senate and seating are skipped, and nothing else is asked for
    assert "skipping Senate verification" in caplog.text
    with pytest.raises(FileNotFoundError):
        main({**cfg, "program": "senate"})


def test_program_is_never_saved_to_the_config(tmp_path):
    import argparse
    import json
    from utsu_std.utils import load_and_merge_config

    path = tmp_path / "config.json"
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(path))
    parser.add_argument("--program", default="all")
    load_and_merge_config(parser.parse_args(["--program", "agm"]), parser, transient=("program",))
    assert "program" not in json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({"program": "sap"}), encoding="utf-8")
    assert load_and_merge_config(parser.parse_args([]), parser, transient=("program",))["program"] == "all"
