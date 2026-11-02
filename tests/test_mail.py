import csv
import random
import sqlite3
from pathlib import Path

import pytest

from utsu_mail import campaign, transport
from utsu_mail.__main__ import main
from utsu_mail.notices import NAME_FIELD, NUMBER_FIELD, diagnose, main as notices_main
from utsu_mail.recipients import read_csv as read_csv_rows, read_sql, split_by_validity
from utsu_core.resources import package_text, schema_dirs
from utsu_mail.schema import schema_fields
from utsu_mail.templates import Template, TemplateError, load_template, parse_template
from utsu_std.utils import student_uuid_for

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = transport.SmtpSettings(host="localhost", from_addr="elections@utsu.ca", from_name="UTSU", security="none", delay=0)


def test_render_and_missing():
    t = Template("t", "Hi {{ full_name }}", "Dear {{full_name}} of {{ division }}", "<p>{{ full_name }}</p>")
    out = t.render({"full_name": "A <b>", "division": "UC"})
    assert out.subject == "Hi A <b>" and out.html == "<p>A &lt;b&gt;</p>"
    assert t.missing_fields({"full_name": "x"}) == ["division"]
    with pytest.raises(TemplateError):
        t.render({"full_name": "x"})


def test_subject_cannot_inject_headers():
    t = Template("t", "Hi {{ n }}", "body")
    assert "\n" not in t.render({"n": "a\r\nBcc: x@y.ca"}).subject


def test_parse_requires_subject():
    with pytest.raises(TemplateError):
        parse_template("x", "no header here")
    with pytest.raises(TemplateError):
        parse_template("x", "From: a\n\nbody")


def test_bundled_templates_only_use_schema_or_form_fields():
    allowed = {"full_name", "division", "id"}
    for name in ("senate_verified", "senate_unverified"):
        assert load_template(name, ROOT / "templates" / "email").fields <= allowed


def test_notice_templates_are_tracked_and_use_known_fields():
    tracking = {"application_uuid", "student_uuid", "submission_ts"}
    extra = {"more_info": {"unverified_fields"}, "rejection": {"rejection_reason"}}
    for program, own in (("senate", {"full_name", "division"}), ("sabp", {"full_name", "terms"}),
                         ("agm", {"full_name"})):
        for kind, kind_fields in extra.items():
            t = load_template(f"{program}_{kind}", ROOT / "templates" / "email")
            assert tracking <= t.fields and kind_fields <= t.fields
            assert t.fields <= tracking | kind_fields | own
            assert "{{ application_uuid }}" in t.subject


def test_schema_fields_reads_sql_files():
    fields = schema_fields(schema_dirs("utsu_core", "utsu_programs"))
    assert "first_name" in fields["uoft_data"]
    assert {"senate_application", "student_aid", "agm_rsvp"} <= set(fields)  # every program's schema is found


def test_split_by_validity():
    rows = [{"email": "a@b.ca"}, {"email": "A@b.ca"}, {"email": "bad"}, {"email": ""}]
    kept, skipped = split_by_validity(rows, "email")
    assert kept == [rows[0]] and [n for n, _ in skipped] == [2, 3, 4]


def test_read_sql_is_readonly(tmp_path):
    import sqlite3
    db = tmp_path / "d.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table t (a text)")
    conn.execute("insert into t values ('x')")
    conn.commit()
    conn.close()
    assert read_sql(db, "select a as email from t") == [{"email": "x"}]
    with pytest.raises(ValueError):
        read_sql(db, "delete from t")


def test_review_requires_exact_confirmation():
    ready = [campaign.Prepared("a@b.ca", Template("t", "s", "b").render({}))]
    answers = iter(["yes", "r", "send"])
    out = []
    assert campaign.review(SETTINGS, ready, [], lambda i: None, lambda _: next(answers), out.append, random.Random(1))
    assert not campaign.review(SETTINGS, ready, [], lambda i: None, lambda _: "n", out.append)
    assert not campaign.review(SETTINGS, ready, [], lambda i: None, lambda _: (_ for _ in ()).throw(EOFError), out.append)


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k): pass
    def ehlo(self): pass
    def quit(self): pass
    def close(self): pass
    def send_message(self, msg): FakeSMTP.sent.append(msg)


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_send_all_logs_and_resumes(monkeypatch, tmp_path):
    monkeypatch.setattr(transport.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    t = Template("t", "Hi {{ n }}", "body {{ n }}")
    ready, skipped = campaign.prepare(t, [{"email": "a@b.ca", "n": "A"}, {"email": "c@d.ca", "n": ""}], "email")
    assert len(ready) == 1 and len(skipped) == 1
    log = tmp_path / "log.csv"
    assert campaign.send_all(SETTINGS, ready, log)["sent"] == 1
    assert FakeSMTP.sent[0]["To"] == "a@b.ca" and FakeSMTP.sent[0]["Subject"] == "Hi A"
    assert campaign.already_sent(log) == {"a@b.ca"}


def test_cli_dry_run_and_cancel(monkeypatch, tmp_path, capsys):
    tdir = tmp_path / "tpl"
    tdir.mkdir()
    (tdir / "hello.txt").write_text("Subject: Hi {{ full_name }}\n\nBody for {{ division }}\n", encoding="utf-8")
    recipients = tmp_path / "r.csv"
    write_csv(recipients, [{"email": "a@b.ca", "full_name": "Ada L", "division": "UC"}])
    base = ["hello", "--template_dir", str(tdir), "--recipients", str(recipients), "--log_dir", str(tmp_path),
            "--config", str(tmp_path / "none.json")]

    assert main(base + ["--dry_run"]) == 0
    assert "Subject: Hi Ada L" in capsys.readouterr().out

    # a missing field is reported against the schema, before any SMTP use
    (tdir / "bad.txt").write_text("Subject: x\n\n{{ faculty }}\n", encoding="utf-8")
    assert main(["bad"] + base[1:]) == 2

    # real send path with no smtp config refuses
    assert main(base) == 2


def make_uoft_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(package_text("utsu_core", "schema/uoft_data.sql"))
    conn.executemany(
        "INSERT INTO uoft_data (uuid, trunc_id, first_name, last_name, full_name, division, faculty) "
        "VALUES (?, ?, 'f', 'l', ?, 'UC', 'A&S')",
        [("u-ada", 3971, "Ada Lovelace"), ("u-sam1", 1111, "Sam Lee"), ("u-sam2", 2222, "Sam Lee")])
    conn.commit()
    conn.close()


def test_diagnose_names_the_unverified_fields(tmp_path):
    make_uoft_db(tmp_path / "db.sqlite")
    cur = sqlite3.connect(tmp_path / "db.sqlite").cursor()
    ada = {"full_name": "Ada Lovelace", "id": "1006639715", "email": "ada@x.ca"}
    ada_id = student_uuid_for("ada@x.ca")  # the internal student id comes from the email, verified or not

    assert diagnose(ada, cur) == (ada_id, [])
    assert diagnose({**ada, "id": "1000000009"}, cur) == (ada_id, [NUMBER_FIELD])
    assert diagnose({**ada, "full_name": "Adda Lovelace"}, cur) == (ada_id, [NAME_FIELD])
    assert diagnose({"full_name": "Zed Nobody", "id": "1009999999", "email": "z@x.ca"}, cur) == \
        (student_uuid_for("z@x.ca"), [NAME_FIELD])
    # only a name: a shared name needs a student number, an unknown name needs a better name
    assert diagnose({"full_name": "Sam Lee", "email": "s@x.ca"}, cur) == (student_uuid_for("s@x.ca"), [NUMBER_FIELD])
    assert diagnose({"full_name": "Nobody", "email": "N@x.ca"}, cur) == (student_uuid_for("n@x.ca"), [NAME_FIELD])
    # a student number alone matches nobody, so the name is what is asked for
    assert diagnose({"id": "1000011119", "email": "s1@x.ca"}, cur) == (student_uuid_for("s1@x.ca"), [NAME_FIELD])
    # no email: the student number identifies them
    assert diagnose({"id": "1000099999"}, cur)[0] == student_uuid_for(student_number="1000099999")
    assert diagnose({"email": "e@x.ca"}, cur)[1] == [NAME_FIELD]


def test_notices_to_tracked_email(monkeypatch, tmp_path):
    monkeypatch.setattr(transport.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    make_uoft_db(tmp_path / "db.sqlite")
    applications = tmp_path / "unverified.csv"
    write_csv(applications, [
        {"uuid": "app-1", "submission_ts": "2026/09/12 3:19:43 p.m. AST", "full_name": "Ada Lovelace",
         "id": "1000000009", "email": "ada@x.ca", "division": "UC"},
        {"uuid": "app-2", "submission_ts": "2026/09/13 1:00:00 p.m. AST", "full_name": "Ada Lovelace",
         "id": "1006639715", "email": "ok@x.ca", "division": "UC"}])
    out = tmp_path / "notices.csv"
    base = ["senate", "more_info", "--input", str(applications), "--db", str(tmp_path / "db.sqlite"),
            "--config", str(tmp_path / "none.json"), "--output", str(out)]
    assert notices_main(base) == 0

    ada_id = student_uuid_for("ada@x.ca")
    rows = read_csv_rows(out)
    assert [r["application_uuid"] for r in rows] == ["app-1"]  # app-2 is fully matched, so nothing to ask
    assert rows[0]["student_uuid"] == ada_id and rows[0]["unverified_fields"] == NUMBER_FIELD

    ready, skipped = campaign.prepare(load_template("senate_more_info", ROOT / "templates" / "email"), rows, "email")
    assert not skipped and "[Ref app-1]" in ready[0].rendered.subject
    assert f"Student ID:     {ada_id}" in ready[0].rendered.text and NUMBER_FIELD in ready[0].rendered.text

    log = tmp_path / "log.csv"
    assert campaign.send_all(SETTINGS, ready, log)["sent"] == 1
    logged = read_csv_rows(log)
    assert (logged[0]["application_uuid"], logged[0]["student_uuid"]) == ("app-1", ada_id)

    # a rejection needs a reason
    assert notices_main(["sabp", "rejection"] + base[2:]) == 2
    assert notices_main(["senate", "rejection", "--reason", "Not eligible"] + base[2:]) == 0
    assert {r["rejection_reason"] for r in read_csv_rows(out)} == {"Not eligible"}
