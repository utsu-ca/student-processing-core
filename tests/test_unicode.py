"""Unusual Unicode in names, numbers, emails and files must neither crash the program nor cause false mismatches."""
import csv
import logging

import pytest

from test_agm import students_db
from test_sap import LC_ROW, form_csv as sap_form
from test_identity import run_senate, senate_file, write_rows
from utsu_core import sap, verification
from utsu_core.importing_data import ascii_digits
from utsu_mail.recipients import read_csv, valid_email
from utsu_std.csv_safe import open_csv, read_text
from utsu_std.parsing_utils import get_trunc_id
from utsu_std.utils import clean_text, identity_key, normalize_case, student_uuid_for

# (what was typed, what it should become): everything is folded to plain ASCII
NAMES = [
    ("Jose\u0301 Garcia", "Jose Garcia"),                     # decomposed accent
    ("JOS\u00c9  garc\u00eda", "Jose Garcia"),                # accents, case and spacing
    ("\uff21nn Lee", "Ann Lee"),                              # fullwidth letter
    ("\ufb01nn Lee", "Finn Lee"),                             # ligature
    ("Ann\u200b Lee", "Ann Lee"),                             # zero-width space
    ("\ufeffAnn Lee", "Ann Lee"),                             # byte order mark
    ("An\u00adn Lee", "Ann Lee"),                             # soft hyphen
    ("\u200eAnn Lee\u200f", "Ann Lee"),                       # bidi marks
    ("Ann\u00a0\u2009Lee", "Ann Lee"),                        # non-breaking and thin spaces
    ("D\u2019Angelo", "D'Angelo"),                            # curly apostrophe
    ("O\u02bcNeil", "O'Neil"),
    ("Mary\u2010Jane", "Mary-Jane"),                          # unicode hyphen
    ("Mary\u2013Jane", "Mary-Jane"),                          # en dash
    ("Nguye\u0302\u0303n", "Nguyen"),                         # stacked accents
    ("STRA\u1e9eE", "Strasse"),                               # capital sharp s
    ("\u00d8lafur \u00c6sir \u0141ukasz", "Olafur Aesir Lukasz"),  # letters that are not an accent on a letter
    ("Ann\ud800 Lee", "Ann Lee"),                             # lone surrogate
    ("Ma\u0331nuel", "Manuel"),                               # a combining mark with no composed letter
    ("\u0645\u062d\u0645\u062f \u0639\u0644\u064a", ""),      # Arabic: not kept
    ("\u674e \u660e", ""),                                    # Chinese: not kept
    ("Ann \U0001f600 Lee", "Ann Lee"),                        # emoji: not kept
    ("", ""),
    ("  \u200b ", ""),
]


@pytest.mark.parametrize("typed, expected", NAMES)
def test_names_are_made_plain_ascii_without_crashing(typed, expected):
    got = normalize_case(typed)
    assert got == expected and got.isascii()
    assert normalize_case(got) == got  # normalising twice changes nothing


def test_the_turkish_dotted_capital_i_is_folded():
    assert normalize_case("\u0130BRAH\u0130M \u0130\u015f\u0131k") == "Ibrahim Isik"


def test_text_that_is_not_text_does_not_crash():
    assert clean_text(None) == "" and clean_text(12345) == "12345"
    assert normalize_case("\x00\x01Ann\x7f Lee\x9f") == "Ann Lee"


@pytest.mark.parametrize("number", [
    "1006639715", " 1006639715 ", "1006639715\u200b", "\u200b1006639715",
    "\uff11\uff10\uff10\uff16\uff16\uff13\uff19\uff17\uff11\uff15",   # fullwidth
    "\u00b9\u2070\u2070\u2076\u2076\u00b3\u2079\u2077\u00b9\u2075",   # superscript
    "xxxxx3971x", "3971", "\uff13\uff19\uff17\uff11",
])
def test_a_student_number_gives_the_same_four_digits_however_it_was_typed(number):
    assert get_trunc_id(number) == 3971


def test_digits_of_other_scripts_are_not_supported():
    arabic_indic = "\u0661\u0660\u0660\u0666\u0666\u0663\u0669\u0667\u0661\u0665"
    assert get_trunc_id(arabic_indic) is None


@pytest.mark.parametrize("number", ["10066397\u00b215", "12\u00b3", "\u2460\u2461\u2462\u2463\u2464\u2465\u2466\u2467\u2468",
                                    "abc", "\U0001f600" * 10, "\ud800" * 10])
def test_nonsense_numbers_never_crash(number):
    get_trunc_id(number)


def test_ascii_digits():
    assert ascii_digits("xxxxx3971x") == "3971" and ascii_digits("\uff13\uff19\u00b3") == "393"
    assert ascii_digits("\u0667\u0661") == "" and ascii_digits("none") == ""


def test_ids_are_the_same_for_text_that_looks_the_same():
    assert student_uuid_for("Ann@X.ca") == student_uuid_for("\uff21nn@x.ca") == student_uuid_for("A\u200bnn@x.ca ")
    assert student_uuid_for(full_name="Jose\u0301 Garc\u00eda") == student_uuid_for(full_name="JOSE garcia")
    assert identity_key("\u200b", "", "Ann") == "ann"  # an email that is only invisible counts as missing


def test_names_with_no_ascii_letters_get_their_own_ids_and_cannot_be_verified(tmp_path):
    # two different students whose names are entirely in another script must not be treated as one person
    assert identity_key(full_name="\u674e\u660e") != identity_key(full_name="\u738b\u82b3")
    assert student_uuid_for(full_name="\u674e\u660e") != student_uuid_for(full_name="\u738b\u82b3")
    conn = students_db(("Ann", "Lee", 3161))
    form = senate_file(tmp_path / "s.csv", [("2026/10/01 9:00:00 a.m. AST", "\u674e\u660e", "", ""),
                                            ("2026/10/01 9:05:00 a.m. AST", "\u738b\u82b3", "", "")], with_id=False)
    r = run_senate(form, conn, tmp_path)
    assert r["verified"] == [] and len(r["unverified"]) == 2  # both are kept, neither replaced the other
    matches = verification.match_students([{"full_name": "\u674e\u660e"}], conn.cursor())
    assert matches[0].reason.startswith("name has no plain letters")


def test_addresses_with_invisible_characters_are_not_valid():
    assert not valid_email("ann\u200b@x.ca") and not valid_email("\ufeffann@x.ca") and valid_email("jos\u00e9@x.ca")


# ---- end to end ---------------------------------------------------------------------------------------------

def test_a_name_typed_oddly_still_matches_the_roster(tmp_path):
    conn = students_db(("Jose", "Garcia", 3971), ("Dario", "O'Neil-Smith", 1111))  # the roster is stored as ASCII
    form = senate_file(tmp_path / "s.csv", [
        ("2026/10/01 9:00:00 a.m. AST", "Jose\u0301\u200b GARC\u00cdA", "\uff11\uff10\uff10\uff16\uff16\uff13\uff19\uff17\uff11\uff15",
         "jose@x.ca"),
        ("2026/10/01 9:05:00 a.m. AST", "Dar\u00edo O\u2019Neil\u2013Smith", "1000011119\u200b", "dario@x.ca")])
    r = run_senate(form, conn, tmp_path)
    assert [a["verification_level"] for a in r["verified"]] == [3, 3] and r["unverified"] == []


def test_the_roster_is_imported_as_ascii(tmp_path):
    from utsu_core.importing_data import import_uoft_data
    roster = tmp_path / "roster.csv"
    roster.write_bytes("First Name,Last Name,Truncated Student Number,Faculty,Division\r\n"
                       "Jos\u00e9,Garc\u00eda,xxxxx3971x,Arts \u00c6,UC\r\n".encode("cp1252", "ignore"))
    conn = students_db()
    conn.execute("DROP TABLE uoft_data")
    assert import_uoft_data(conn, roster)
    assert conn.execute("SELECT full_name, trunc_id, faculty FROM uoft_data").fetchall() == [
        ("Jose Garcia", 3971, "ARTS AE")]


def test_emoji_and_right_to_left_text_in_a_statement_are_written_and_read_back(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    statement = "Rent is due \U0001f605 \u0645\u0631\u062d\u0628\u0627 \u202eevil\u202c \u0000 end"
    form = sap_form(tmp_path / "f.csv", [{**LC_ROW, "LC General Statement": statement}])
    out = tmp_path / "out"
    sap.process_sap(form, conn, output_dir=out)
    sheet = next((out / "sap_batches").glob("batch_*.csv"))
    rows = list(csv.DictReader(open(sheet, encoding="utf-8-sig")))
    assert "\U0001f605" in rows[0]["general_statement"] and "\u0645\u0631\u062d\u0628\u0627" in rows[0]["general_statement"]


# ---- files saved by spreadsheet programs --------------------------------------------------------------------

def test_a_file_saved_as_excels_plain_csv_is_read_not_rejected(tmp_path, caplog):
    path = tmp_path / "r.csv"
    path.write_bytes("email,full_name\r\nj@x.ca,Jos\u00e9 Garc\u00eda\r\n".encode("cp1252"))
    with caplog.at_level(logging.WARNING):
        assert read_csv(path) == [{"email": "j@x.ca", "full_name": "Jos\u00e9 Garc\u00eda"}]
    assert "Windows-1252" in caplog.text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_utf8_and_utf16_files_are_read(tmp_path, encoding):
    path = tmp_path / "r.csv"
    path.write_bytes("email,full_name\r\nj@x.ca,Jos\u00e9 \u674e\r\n".encode(encoding))
    assert read_csv(path) == [{"email": "j@x.ca", "full_name": "Jos\u00e9 \u674e"}]


def test_bytes_that_are_not_text_do_not_stop_the_run(tmp_path):
    path = tmp_path / "r.csv"
    path.write_bytes(b"email,note\r\nj@x.ca,caf\xe9 \x81\x8d bad\r\n")
    text = read_text(path)
    assert "j@x.ca" in text and "\ud800" not in text
    assert list(csv.reader(open_csv(path)))[1][0] == "j@x.ca"


def test_an_excel_resaved_decisions_sheet_is_still_recorded(tmp_path):
    conn = students_db(("Ann", "Lee", 3161))
    sap.process_sap(sap_form(tmp_path / "f.csv", [LC_ROW]), conn, output_dir=tmp_path / "out")
    (uuid,) = conn.execute("SELECT uuid FROM student_aid").fetchone()
    path = tmp_path / "decisions.csv"
    path.write_bytes(f"uuid,decision,awarded_amount,notes\r\n{uuid},approved,650,caf\u00e9\r\n".encode("cp1252"))
    assert sap.record_decisions(conn, path)["recorded"] == 1
