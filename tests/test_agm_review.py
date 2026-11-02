"""AGM review fixes: UofT email rule, safe writes, "no need" answers, unreadable timestamps, rebuilt tables."""
import logging
import os
import sqlite3

import pytest

from test_agm import form_csv, rsvp, students_db
from utsu_core import selection
from utsu_programs.agm import program as agm
from utsu_mail.notices import EMAIL_FIELD, diagnose
from utsu_std import csv_safe
from utsu_std.database import replace_table

TS = "2026/10/01 9:00:00 a.m. AST"


# ---- only @mail.utoronto.ca is a UofT email ---------------------------------------------------------------------

def test_only_a_mail_utoronto_ca_address_verifies(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000), ("Di", "Fox", 6000))
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp(TS, "Ann", "Lee", "Ann.Lee@MAIL.UToronto.ca"),
        rsvp(TS, "Bo", "Chan", "bo@utoronto.ca"),
        rsvp(TS, "Cy", "Doe", "cy@gmail.com"),
        rsvp(TS, "Di", "Fox", "")])
    result = agm.process_agm(form, conn, output_dir=tmp_path / "out")
    assert [r["full_name"] for r in result["verified"]] == ["Ann Lee"]
    assert {r["full_name"]: r["reason"] for r in result["unverified"]} == {
        "Bo Chan": agm.EMAIL_REASON, "Cy Doe": agm.EMAIL_REASON, "Di Fox": agm.EMAIL_REASON}
    assert all(r["uoft_uuid"] is None for r in result["unverified"])


def test_the_more_information_email_asks_for_the_uoft_email():
    conn = students_db(("Ann", "Lee", 3161))
    cur = conn.cursor()
    app = {"full_name": "Ann Lee", "email": "ann@gmail.com"}
    assert diagnose(app, cur, "agm")[1] == [EMAIL_FIELD]
    assert diagnose(app, cur)[1] == []  # the other programs have no such rule
    assert diagnose({**app, "email": "ann@mail.utoronto.ca"}, cur, "agm")[1] == []


# ---- writes -------------------------------------------------------------------------------------------------------

def test_a_locked_output_file_stops_the_run_before_anything_changes(tmp_path, monkeypatch):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    first = form_csv(tmp_path / "1.csv", [rsvp(TS, "Ann", "Lee", "ann@mail.utoronto.ca")])
    agm.process_agm(first, conn, output_dir=out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    real_open = open

    def locked(path, mode="r", *args, **kwargs):  # Excel has the door list open
        if str(path).endswith("agm_list_all.csv") and "a" in mode:
            raise PermissionError("locked")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(csv_safe, "open", locked, raising=False)
    second = form_csv(tmp_path / "2.csv", [rsvp(TS, "Bo", "Chan", "bo@mail.utoronto.ca")])
    with pytest.raises(PermissionError, match="open in another program"):
        agm.process_agm(second, conn, output_dir=out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert conn.execute("SELECT COUNT(*) FROM agm_rsvp WHERE status = 'verified'").fetchone() == (1,)
    assert not list(out.glob("*.tmp"))


def test_a_failed_write_leaves_the_old_file_whole(tmp_path, monkeypatch):
    path = tmp_path / "list.csv"
    csv_safe.write_rows(path, ["a"], [{"a": "old"}])

    def refuse(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError, match="open in another program"):
        csv_safe.write_rows(path, ["a"], [{"a": "new"}])
    assert path.read_text(encoding="utf-8-sig").split() == ["a", "old"] and not list(tmp_path.glob("*.tmp"))


# ---- "no need" answers ---------------------------------------------------------------------------------------------

def test_none_and_na_are_not_accommodation_needs(tmp_path):
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111), ("Cy", "Doe", 5000))
    form = form_csv(tmp_path / "rsvp.csv", [
        rsvp("2026/10/01 9:00:00 a.m. AST", "Ann", "Lee", "a@mail.utoronto.ca", dietary="None"),
        rsvp("2026/10/01 9:01:00 a.m. AST", "Bo", "Chan", "b@mail.utoronto.ca", dietary="N/A."),
        rsvp("2026/10/01 9:02:00 a.m. AST", "Cy", "Doe", "c@mail.utoronto.ca", dietary="Vegan")])
    out = tmp_path / "out"
    result = agm.process_agm(form, conn, output_dir=out)
    assert result["summary"]["dietary_requests"] == 1
    rows = (out / "agm_accommodations.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(rows) == 2 and "Vegan" in rows[1]


# ---- timestamps --------------------------------------------------------------------------------------------------

def test_unreadable_timestamps_are_warned_about(tmp_path, caplog):
    conn = students_db(("Ann", "Lee", 3161))
    form = form_csv(tmp_path / "rsvp.csv", [rsvp("2026-10-01 9:00:00 a.m.", "Ann", "Lee", "a@mail.utoronto.ca"),
                                            rsvp("2026/10/02 9:00:00 a.m. AST", "Ann", "Lee", "a@mail.utoronto.ca")])
    with caplog.at_level(logging.WARNING):
        agm.process_agm(form, conn, output_dir=tmp_path / "out")
    assert "1 of 2 AGM submissions have a timestamp that could not be read" in caplog.text
    assert "2026-10-01 9:00:00 a.m." in caplog.text


def test_an_impossible_hour_is_unreadable_not_a_crash():
    assert selection.parse_submission_ts("2026/08/14 0:05:00 a.m.") is None
    assert selection.parse_submission_ts("2026/08/14 13:30:47 p.m.") is None
    assert selection.parse_submission_ts("2026/08/14 12:05:00 a.m.").hour == 0


# ---- the table is dropped and created again ------------------------------------------------------------------------

def test_an_older_table_shape_is_replaced(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    conn.executescript("CREATE TABLE agm_rsvp (uuid TEXT PRIMARY KEY, old_column TEXT); "
                       "INSERT INTO agm_rsvp VALUES ('stale', 'x');")
    form = form_csv(tmp_path / "rsvp.csv", [rsvp(TS, "Ann", "Lee", "a@mail.utoronto.ca")])
    agm.process_agm(form, conn, output_dir=tmp_path / "out")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(agm_rsvp)")}
    assert "old_column" not in columns and {"uoft_uuid", "verification_level"} <= columns
    assert conn.execute("SELECT COUNT(*) FROM agm_rsvp").fetchone() == (1,)


def test_a_failed_replacement_leaves_the_previous_table():
    conn = sqlite3.connect(":memory:")
    schema = "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    replace_table(conn, "t", schema, "INSERT INTO t VALUES (:id, :name)", [{"id": 1, "name": "keep"}])
    with pytest.raises(sqlite3.IntegrityError):
        replace_table(conn, "t", schema, "INSERT INTO t VALUES (:id, :name)",
                      [{"id": 2, "name": "new"}, {"id": 3, "name": None}])
    assert conn.execute("SELECT id, name FROM t").fetchall() == [(1, "keep")]


# ---- empty export, Excel encoding ---------------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["", "\n", ",,\n"])
def test_an_empty_export_is_a_clear_error(tmp_path, content):
    path = tmp_path / "empty.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="is empty"):
        agm.read_rsvps(path)


def test_output_files_open_in_excel_with_accents_and_read_back(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    row = rsvp(TS, "Ann", "Lee", "a@mail.utoronto.ca")
    row[2] = "Jos\u00e9"  # preferred name
    out = tmp_path / "out"
    agm.process_agm(form_csv(tmp_path / "rsvp.csv", [row]), conn, output_dir=out)
    raw = (out / "agm_list_all.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "Jos\u00e9".encode("utf-8") in raw
    from utsu_mail.recipients import read_csv
    assert read_csv(out / "agm_list_all.csv")[0]["preferred_name"] == "Jos\u00e9"  # the BOM is not in the first header
