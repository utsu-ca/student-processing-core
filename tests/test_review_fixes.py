"""Regression tests for the code-review fixes."""
import argparse
import csv
import json
import logging
import smtplib
import sqlite3
from pathlib import Path

import pytest

from test_agm import students_db
from test_mail import SETTINGS
from test_sap import LC_ROW, fill_sheet, form_csv as sap_form, students_db as sap_students
from utsu_core import agm, importing_data, sap, wfni
from utsu_mail.campaign import Prepared, send_all
from utsu_mail.notices import DIVISION_FIELD, NUMBER_FIELD, diagnose
from utsu_mail.templates import RenderedMessage
from utsu_mail.transport import SmtpSender
from utsu_std.utils import absfile, explicit_flags, load_and_merge_config, setup_logging
from utsu_core.verification import construct_lookup_tables

ROSTER_HEADER = "First Name,Last Name,Truncated Student Number,Faculty,Division\n"


# ---- the student data import ---------------------------------------------------------------------------------

def test_roster_import_skips_blank_lines_and_leaves_no_copy(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER_HEADER + "Ann,Lee,xxxxx3971x,ARTSC,UC\n,,,,\n\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    assert importing_data.import_uoft_data(conn, roster)
    assert conn.execute("SELECT full_name, trunc_id FROM uoft_data").fetchall() == [("Ann Lee", 3971)]
    assert list(tmp_path.iterdir()) == [roster]


def test_roster_row_with_too_few_columns_is_an_error(tmp_path):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER_HEADER + "Ann,Lee\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Line 2"):
        importing_data.read_uoft_rows(roster)


def test_a_failed_import_leaves_the_previous_student_data(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text(ROSTER_HEADER + "Ann,Lee,xxxxx3971x,ARTSC,UC\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    importing_data.import_uoft_data(conn, good)

    no_columns = tmp_path / "bad.csv"
    no_columns.write_text("First Name,Last Name\nBo,Chan\n", encoding="utf-8")
    empty = tmp_path / "empty.csv"
    empty.write_text(ROSTER_HEADER, encoding="utf-8")
    for bad in (no_columns, empty, tmp_path / "missing.csv"):
        with pytest.raises((ValueError, FileNotFoundError)):
            importing_data.import_uoft_data(conn, bad)
        assert conn.execute("SELECT full_name FROM uoft_data").fetchall() == [("Ann Lee",)]


def test_identical_roster_rows_are_kept_once(tmp_path, caplog):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER_HEADER + "Ann,Lee,xxxxx3971x,ARTSC,UC\nAnn,Lee,xxxxx3971x,ARTSC,UC\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    with caplog.at_level(logging.WARNING):
        importing_data.import_uoft_data(conn, roster)
    assert conn.execute("SELECT COUNT(*) FROM uoft_data").fetchone() == (1,) and "duplicate" in caplog.text


def test_main_imports_again_when_the_student_table_is_empty(tmp_path):
    from utsu_core.main import main

    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER_HEADER + "Ann,Lee,xxxxx3971x,ARTSC,UC\n", encoding="utf-8")
    db = tmp_path / "db.sqlite"
    cfg = {"db": str(db), "no_backup": True, "import_uoft_data": str(roster), "program": "agm",
           "agm_file": "", "output_dir": str(tmp_path / "out")}
    with pytest.raises(ValueError, match="AGM"):  # the roster is imported first, then the missing export is refused
        main(cfg)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM uoft_data").fetchone() == (1,)
    conn.execute("DELETE FROM uoft_data")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="AGM"):  # the table is empty: it is imported again
        main(cfg)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM uoft_data").fetchone() == (1,)
    conn.close()


def test_a_missing_roster_file_is_an_error_exit(tmp_path):
    from utsu_core.main import main

    cfg = {"db": str(tmp_path / "db.sqlite"), "no_backup": True, "import_uoft_data": str(tmp_path / "none.csv"),
           "program": "agm", "output_dir": str(tmp_path / "out")}
    with pytest.raises(FileNotFoundError):
        main(cfg)


# ---- SABP ------------------------------------------------------------------------------------------------------

def test_sabp_form_without_a_student_number_column_is_processed(tmp_path):
    keys = [k for k in construct_lookup_tables(Path(absfile("data/conversion_table_sap.csv")))[0]
            if k != "Student Number"]
    path = tmp_path / "nonum.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(keys)
        writer.writerow([LC_ROW.get(k, "") for k in keys])
    conn = sap_students(("Ann", "Lee", 3161))
    result = sap.process_sap(path, conn, output_dir=tmp_path / "out", batch_size=5)
    assert len(result["verified"]) == 1
    assert conn.execute("SELECT student_number FROM student_aid").fetchone() == ("",)


def test_the_database_keeps_only_the_last_four_digits_of_a_student_number(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=tmp_path / "out", batch_size=5)
    assert conn.execute("SELECT student_number FROM student_aid").fetchone() == ("3161",)
    assert "1001231617" not in str(conn.execute("SELECT * FROM student_aid").fetchall())


def test_overlapping_term_selections_are_raised_for_a_person(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    first = {**LC_ROW, "Timestamp": "2026/09/01 10:00:00 a.m. AST"}
    second = {**LC_ROW, "Timestamp": "2026/09/02 10:00:00 a.m. AST", "Application Semester": "Fall 2026;Winter 2027"}
    result = sap.process_sap(sap_form(tmp_path / "1.csv", [first, second]), conn, output_dir=tmp_path / "out",
                             batch_size=5)
    assert [a["kind"] for a in result["alerts"]] == ["overlapping_terms"]


def test_amounts_must_be_positive_and_finite():
    assert [sap.parse_amount(v) for v in ("nan", "inf", "-50", "0", "", "$1,500")] == [None] * 5 + [1500.0]
    assert sap.parse_amount("0", allow_zero=True) == 0.0
    app = sap.build_application({"bursary_type": "Living-costs", "lc_amount": "nan"})
    assert "no_amount" in app["flags"] and app["requested_amount"] == ""


def test_a_decision_can_award_nothing_and_a_repeat_keeps_the_amount(tmp_path, caplog):
    conn = sap_students(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5)
    (uuid,) = conn.execute("SELECT uuid FROM student_aid").fetchone()
    path = tmp_path / "d.csv"

    path.write_text(f"uuid,decision,awarded_amount\n{uuid},declined,0\n", encoding="utf-8")
    assert sap.record_decisions(conn, path)["recorded"] == 1
    assert conn.execute("SELECT decision, awarded_amount FROM student_aid").fetchone() == ("declined", 0.0)

    path.write_text(f"uuid,decision,awarded_amount\n{uuid},approved,650\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        sap.record_decisions(conn, path)
    assert "replacing it" in caplog.text
    path.write_text(f"uuid,decision,awarded_amount\n{uuid},approved,\n", encoding="utf-8")
    sap.record_decisions(conn, path)  # the same decision again, no amount: the stored amount stays
    assert conn.execute("SELECT decision, awarded_amount FROM student_aid").fetchone() == ("approved", 650.0)


def test_reviewer_names_cannot_climb_out_of_the_folder():
    assert sap.reviewer_id("..") == "" and sap.reviewer_id(".hidden") == "hidden"
    assert sap.parse_reviewers("..,Ann,.") == ["Ann"]


def test_ratings_only_count_from_the_assigned_reviewers(tmp_path):
    conn = sap_students(("Ann", "Lee", 3161))
    out = tmp_path / "out"
    sap.process_sap(sap_form(tmp_path / "1.csv", [LC_ROW]), conn, output_dir=out, batch_size=5, reviewers="A,B")
    by_reviewer = out / "sap_batches" / "by_reviewer"
    stray = by_reviewer / "Stray"
    stray.mkdir()
    (stray / "batch_001.csv").write_bytes((by_reviewer / "A" / "batch_001.csv").read_bytes())
    for sheet in (by_reviewer / "A" / "batch_001.csv", stray / "batch_001.csv"):
        fill_sheet(sheet, [3])
    result = sap.merge_reviews(conn, out)
    assert result["rows"][0]["ratings"] == "A=3" and any("Stray" in p for p in result["problems"])


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


def test_redact_finds_short_and_accented_names():
    assert sap.redact("Li said Jos\u00e9 Wu, and JOSE", ["Li", "Jose Wu"]) == "[name] said [name] [name], and [name]"
    assert sap.redact("Anna lives in Liverpool", ["Ann", "Li"]) == "Anna lives in Liverpool"


# ---- WFNI, AGM --------------------------------------------------------------------------------------------------

def test_only_options_marked_with_a_star_match_by_their_start():
    weights = wfni.current_weights()
    assert wfni.points_for(weights.tables["dependents"], "10") is None
    assert wfni.points_for(weights.tables["external_supports"], "None of the above") is None
    assert wfni.points_for(weights.tables["disposable_income"], "A negative amount (taking on debt)") == 5
    app = sap.build_application({"living_situation": "Stably Housed", "external_supports": "None of the above"})
    assert "wfni_incomplete" in app["flags"]


def test_a_negated_in_person_answer_is_not_in_person():
    assert not agm.is_in_person("No, I will not be attending in person")
    assert agm.is_in_person("Yes, I'll be attending in-person.")


# ---- logging and config ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, level", [("info", logging.INFO), ("WARNING", logging.WARNING), ("warn", logging.WARNING),
                                         ("Debug", logging.DEBUG), ("ERROR", logging.ERROR), ("TRACE", 5)])
def test_verbosity_names_map_to_their_levels(name, level, monkeypatch):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    setup_logging({"verbosity": name, "log_file": ""})
    assert seen["level"] == level


def test_an_unknown_verbosity_is_refused():
    with pytest.raises(ValueError, match="verbosity"):
        setup_logging({"verbosity": "LOUD", "log_file": ""})


def test_a_flag_equal_to_its_default_still_overrides_the_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"seed": 7}), encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(path))
    parser.add_argument("--seed", type=int, default=2026)
    given = explicit_flags(parser, ["--seed", "2026"])
    assert given == {"seed"}
    conf = load_and_merge_config(parser.parse_args(["--seed", "2026"]), parser, explicit=given)
    assert conf["seed"] == 2026 and json.loads(path.read_text(encoding="utf-8"))["seed"] == 2026
    path.write_text(json.dumps({"seed": 7}), encoding="utf-8")
    assert load_and_merge_config(parser.parse_args([]), parser, explicit=set())["seed"] == 7  # not given: the config's


def test_program_all_skips_a_missing_senate_export(tmp_path, caplog):
    from utsu_core.main import main

    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER_HEADER + "Ann,Lee,xxxxx3971x,ARTSC,UC\n", encoding="utf-8")
    cfg = {"db": str(tmp_path / "db.sqlite"), "no_backup": True, "import_uoft_data": str(roster), "program": "all",
           "verify_student": str(tmp_path / "none.csv"), "output_dir": str(tmp_path / "out")}
    with caplog.at_level(logging.WARNING):
        main(cfg)  # no Senate export: Senate and seating are skipped, and nothing else is asked for
    assert "skipping Senate verification" in caplog.text
    with pytest.raises(FileNotFoundError):
        main({**cfg, "program": "senate"})


def test_program_is_never_saved_to_the_config(tmp_path):
    path = tmp_path / "config.json"
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(path))
    parser.add_argument("--program", default="all")
    load_and_merge_config(parser.parse_args(["--program", "agm"]), parser, transient=("program",))
    assert "program" not in json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({"program": "sap"}), encoding="utf-8")
    assert load_and_merge_config(parser.parse_args([]), parser, transient=("program",))["program"] == "all"


# ---- mail ----------------------------------------------------------------------------------------------------

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
            self.settings, self._client = settings, DroppedClient()

        def __enter__(self):
            return self

        def _connect(self):
            raise OSError("network down")

    import utsu_mail.campaign as campaign
    original, campaign.SmtpSender = campaign.SmtpSender, Sender
    try:
        items = [Prepared(f"x{i}@b.ca", RenderedMessage("s", "t")) for i in range(3)]
        counts = send_all(SETTINGS, items, tmp_path / "log.csv", sleep=lambda _: None)
    finally:
        campaign.SmtpSender = original
    assert counts == {"sent": 0, "failed": 3, "remaining": 0}


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
