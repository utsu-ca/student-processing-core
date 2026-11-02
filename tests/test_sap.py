import csv
import sqlite3
from pathlib import Path

import pytest

from utsu_programs.sap import program as sap
from utsu_core.resources import package_text
from utsu_core.verification import construct_lookup_tables
from utsu_std.utils import absfile

LC_ROW = {
    "Timestamp": "2026/09/30 10:39:14 a.m. AST", "Application Semester": "Fall 2026",
    "Confirmation of UTSU Membership": "Yes", "ACORN-registered First Name": "Ann",
    "ACORN-registered Last Name": "Lee", "Student Number": "1001231617", "UofT Email Address": "ann@mail.utoronto.ca",
    "Faculty": "ARTSC - Faculty of Arts & Science", "Bursary Type": "Living-costs Bursary (long-form)",
    "LC General Statement": "I am Ann. Reach me at ann@mail.utoronto.ca or 416-555-0100.",
    "LC Subsidies": "Transit Mobility Subsidy (up-to $500/term);Emergency Bursary (expedited processing)",
    "LC Requested Amount": "1500", "Disability Status - Mobility": "No", "Disability Status - Invisible": "Yes",
    "Living Situation": "Imminent risk of losing housing", "Access to Disposable Income": "Less than $100",
    "Liquidity": "$0 to $500", "Number of Dependents and/or Remittance members": "0",
    "Access to External Supports": "No", "Extenuating Circumstances": "Recent estrangement",
}
AP_ROW = {**LC_ROW, "Timestamp": "2026/10/01 9:00:00 a.m. AST", "Student Number": "1009991111",
          "ACORN-registered First Name": "Bo", "ACORN-registered Last Name": "Chan",
          "UofT Email Address": "bo@mail.utoronto.ca", "Bursary Type": "Academic Pursuits Bursary (short-form)",
          "AP General Statement": "Conference travel.", "AP Subsidies": "Conference Reimbursement (~$350/term)",
          "AP Request Amount": "350", "LC General Statement": "", "LC Subsidies": "", "LC Requested Amount": "",
          "Living Situation": "", "Access to Disposable Income": "", "Liquidity": ""}


def form_csv(path, rows):
    keys = list(construct_lookup_tables(Path(absfile("data/conversion_table_sap.csv")))[0])
    keys[keys.index("Faculty")] = "Faculty "  # the real export has a trailing space
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(keys)
        for row in rows:
            writer.writerow([row.get(k.strip(), "") for k in keys])
    return path


def students_db(*people):
    conn = sqlite3.connect(":memory:")
    conn.executescript(package_text("utsu_core", "schema/uoft_data.sql"))
    for i, (first, last, trunc) in enumerate(people):
        conn.execute("INSERT INTO uoft_data (uuid, trunc_id, first_name, last_name, full_name, division, faculty) "
                     "VALUES (?, ?, ?, ?, ?, 'UC', 'ARTSC')", (f"u{i}", trunc, first, last, f"{first} {last}"))
    conn.commit()
    return conn


def test_wfni_follows_the_policy_table():
    app = sap.build_application({k.lower(): v for k, v in {
        "disability_invisible": "Yes", "living_situation": "Imminent risk of losing housing",
        "disposable_income": "A negative amount (taking on short-term debt to satisfy necessities)",
        "liquidity": "$0 to $500", "dependents": "4+", "external_supports": "No",
        "circumstances": "a;b;c;d", "disability_mobility": "No"}.items()})
    # 2 + 4 + 5 + 3 + 4 + 1 + min(10, 12)
    assert app["wfni"] == 29 and "high_wfni" in app["flags"]


def test_redact_removes_identifying_details():
    text = sap.redact("Ann here: ann@x.ca, 1001231617, M5S 1A1, https://drive.google.com/x", ["Ann", "Lee"])
    assert text == "[name] here: [email], [student number], [postal code], [link]"


def test_process_sap_verifies_batches_and_saves_rows(tmp_path):
    rows = [LC_ROW, AP_ROW, {**LC_ROW, "Student Number": "1005550000", "ACORN-registered First Name": "Cy",
                              "ACORN-registered Last Name": "Doe", "UofT Email Address": "cy@mail.utoronto.ca"}]
    form = form_csv(tmp_path / "sabp.csv", rows)
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"

    result = sap.process_sap(form, conn, output_dir=out, batch_size=1)
    assert [a["full_name"] for a in result["verified"]] == ["Ann Lee", "Bo Chan"]
    assert [a["full_name"] for a in result["unverified"]] == ["Cy Doe"]
    assert len(result["batches"]) == 2 and len(list((out / "sap_batches").glob("batch_*.csv"))) == 2

    review = (out / "sap_batches" / "batch_001.csv").read_text(encoding="utf-8-sig")
    assert "Ann" not in review and "1001231617" not in review and "ann@" not in review and "416-555" not in review
    assert "B001-01" in review

    db_rows = conn.execute("SELECT status, uoft_uuid, verification_level, aid_type, batch FROM student_aid "
                           "ORDER BY submission_ts").fetchall()
    assert sorted(db_rows, key=str) == sorted([("verified", "u0", 2, "LC", 1), ("verified", "u1", 2, "AP", 2),
                                               ("unverified", None, None, "LC", None)], key=str)

    conn.execute("UPDATE student_aid SET awarded_amount = 650, decision = 'approved' WHERE aid_type = 'LC' "
                 "AND status = 'verified'")
    sap.process_sap(form, conn, output_dir=out, batch_size=1)
    assert conn.execute("SELECT COUNT(*) FROM student_aid").fetchone() == (3,)
    assert conn.execute("SELECT awarded_amount, decision FROM student_aid WHERE decision IS NOT NULL").fetchall() \
        == [(650.0, "approved")]


def fill_sheet(path, levels):
    """Rate the rows of a reviewer sheet in order with the given levels (None leaves a row unrated)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for row, level in zip(rows, levels):
        row["need_level"] = "" if level is None else level
        row["notes"] = "ok" if level is not None else ""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_two_reviewers_per_batch_are_balanced_and_remembered():
    conn = students_db()
    first = sap.assign_reviewers(conn, 3, ["a", "b", "c"])
    assert first == {1: ["a", "b"], 2: ["a", "c"], 3: ["b", "c"]}
    # existing batches keep their reviewers; a new batch goes to the least loaded two
    assert sap.assign_reviewers(conn, 4, ["a", "b", "c", "d"]) == {**first, 4: ["a", "d"]}
    with pytest.raises(ValueError):
        sap.assign_reviewers(conn, 1, ["only_one"])


def test_review_sheets_per_reviewer_are_merged(tmp_path):
    form = form_csv(tmp_path / "sabp.csv", [LC_ROW, AP_ROW])
    conn = students_db(("Ann", "Lee", 3161), ("Bo", "Chan", 9111))
    out = tmp_path / "out"
    result = sap.process_sap(form, conn, output_dir=out, batch_size=2, reviewers="Ann M,Bo")
    assert result["assignments"] == {1: ["Ann_M", "Bo"]}

    sheets = out / "sap_batches" / "by_reviewer"
    first, second = sheets / "Ann_M" / "batch_001.csv", sheets / "Bo" / "batch_001.csv"
    assert first.exists() and (sheets / "Bo" / "batch_001.txt").exists()
    fill_sheet(first, [4, 2])
    fill_sheet(second, [3, None])

    sap.process_sap(form, conn, output_dir=out, batch_size=2, reviewers="Ann M,Bo")  # must not wipe the ratings
    assert "4" in first.read_text(encoding="utf-8-sig")

    merged = {r["record_id"]: r for r in sap.merge_reviews(conn, out)["rows"]}
    one, two = merged["B001-01"], merged["B001-02"]
    assert (one["status"], one["ratings"], one["mean_level"], one["reviewers"]) == ("agreed", "Ann_M=4; Bo=3", "3.5",
                                                                                   "Ann_M; Bo")
    assert one["suggested_amount"] == "775"  # mean of $900 and $650, below the $1500 requested
    assert (two["status"], two["n_ratings"]) == ("incomplete", 1)
    assert conn.execute("SELECT COUNT(*) FROM sap_review").fetchone() == (3,)
    assert (out / "sap_merged_ratings.csv").exists()

    fill_sheet(second, [0, 2])
    again = {r["record_id"]: r for r in sap.merge_reviews(conn, out)["rows"]}
    assert again["B001-01"]["status"] == "discuss" and again["B001-01"]["spread"] == 4
    assert again["B001-02"]["status"] == "agreed"


def test_batches_hold_at_most_the_batch_size():
    apps = [{"uuid": f"{i:03d}", "submission_ts": "2026/09/30 10:39:14 a.m. AST"} for i in range(120)]
    batches = sap.assign_batches(apps, 50)
    assert [len(b) for b in batches] == [50, 50, 20]
    assert (apps[0]["batch"], apps[0]["record"], apps[119]["batch"], apps[119]["record"]) == (1, 1, 3, 20)
