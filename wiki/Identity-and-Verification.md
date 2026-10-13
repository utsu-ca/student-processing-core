# Identity and Verification

## Typed ids

Every id the program stores is a **typed id**: `<kind>:<uuid>`. The kind says what the id identifies, and each kind has
its own UUID namespace (UUIDv5), so the same text hashed as two kinds never collides.

| Kind | Example | Derived from |
|---|---|---|
| `student:email` | `student:email:3f2a...` | the student's email |
| `student:student_number` | | the student number (when the form gave no email) |
| `student:name` | | the name (when it gave neither) |
| `student:unreadable` / `student:none` | | nothing usable (see below) |
| `application:senate`, `application:sap`, `application:agm` | `application:sap:9c1d...` | submission timestamp + the student's identity key |
| `roster` | `roster:77b0...` | a row of `uoft_data` |

In SQLite, `uuid LIKE 'student:%'` selects a kind. The kind of a `student_uuid` tells you which field was used.

**Identity key.** What identifies a student across submissions is the **email**, else the **student number**, else the
**name**, all [cleaned to ASCII](#text-cleaning-ascii-only). If a value was given but nothing is left after cleaning (a
name entirely in another script), a hash of the raw text is used so different people never share a key.

Things to know:

- Ids are **internal database keys**, not identities. Editing a name or student number does not change them, but a row
  that **gains, loses or changes its email** gets new ids (so does an edit, which Google Forms stamps with a new
  timestamp). Do not rely on ids outside the database.
- The email is typed by the applicant and not checked. Someone could use another student's email; that cannot replace a
  verified application (see [Full Exports](Full-Exports-and-Integrity#one-binding-application-per-student)),
  but someone who also knows the name and last four digits would still pass. The warnings in the log are how you notice.

## How a person is matched to the roster

The roster (`uoft_data`) stores **only the last 4 digits** of the student number, plus name, faculty and division.
Each application is matched on its **name** (always needed), plus its student number and division when it gives them. The
last four digits of a student number are shared by many students, so a number or a division alone never identifies
anyone: they only narrow down the people with that name. There are three factors: **name, student number, division**.
Every factor given must agree with the same student (a wrong value never falls back to another factor) and the level is
the number of factors that matched:

| Factors that matched | `verification_level` | `match_method` |
|---|---|---|
| name | **1** | `name only` |
| name + student number, or name + division | **2** | `name + student number`, `name + division` |
| all three | **3** | `name + student number + division` |

The roster's division is its `division`, or its `faculty` when the division is
blank, and is compared as a code (`UC - University College` is `UC`). The SABP form has no division (maximum level 2).

### Fuzzy first names (undisclosed middle names)

Students often leave their middle names off the form. When **no roster student has the exact name**, a second pass
treats the **last name as correct** (the trailing words of the name, so `De La Cruz` works; it must match exactly) and
compares only the **first names** with `first_name_similarity` (`src/utsu_core/verification.py`): the first parts must
be alike (a `difflib` ratio of at least `FUZZY_THRESHOLD`, 0.85, so `Jon` ≈ `John`), and the extra parts of the shorter
name must be found, whole or as an initial, in the longer one (`Ann` ≈ `Ann Marie`, `Ann M` ≈ `Ann Marie`, but not
`Ann Lynn` ≈ `Ann Marie`). The students that pass are decided exactly like exact matches (student number and
division must still agree; several students left means ambiguous). The factor is then called `fuzzy name`
(`fuzzy name`, `fuzzy name + student number`, ...), the level is unchanged, and the application gets the flag
`fuzzy_name_match=<similarity>` (for example `fuzzy_name_match=0.86`). In AGM, the closest three students are written to
`agm_fuzzy_review.csv` for a person to reconcile (see [AGM RSVPs](AGM-RSVPs)).

Reasons an application stays `unverified`:

| Reason | Meaning |
|---|---|
| `name not found` | no roster student has that (normalized) name |
| `no name given` | the row has no name (a student number alone is never matched) |
| `student number does not match` | name exists, but none of those students has that last-4 |
| `division does not match` | the matched student is in a different division |
| `name shared by N students; a student number is needed` | name only, several students (AGM raises an alert) |
| `name has no plain letters (only A-Z letters are supported)` | the name was entirely non-ASCII |

The student number accepted can be a full 10-digit number, the `xxxxx3971x` truncated form, or just the 4 digits.
Unreadable numbers are simply "not a match", never a crash.

### Verification levels at a glance

- **Level 1**: the name alone. Weakest; flagged in AGM as `name_only_match`.
- **Level 2**: the name plus a student number or a division.
- **Level 3**: name, student number and division.

Physical (in-person) verification was removed; the division level replaces it.

## Text cleaning (ASCII only)

The program works in plain ASCII. Everything compared or derived from a name, email or student number is first made
ASCII by `clean_text` / `normalize_case` (`src/utsu_std/utils.py`):

- accents are removed (`José García` → `Jose Garcia`), including on the roster itself, which is stored this way;
- look-alike forms become plain ones (fullwidth letters, ligatures, superscript digits, non-breaking spaces);
- curly apostrophes and every kind of dash are written the plain way (`O’Neil` matches `O'Neil`);
- runs of whitespace collapse; `ß`→`ss`, `Æ`→`AE`, `Ø`→`O`, and similar;
- anything else not ASCII (other scripts, emoji, zero-width and control characters) is **dropped**.

A name written only in another script therefore has nothing left: it cannot be verified (the reason says so, and the
"more information" email asks for the name again), but it still gets its own ids.

Free text the applicant writes (for example the SABP general statement) is **not** changed and is written to outputs
as UTF-8 with a BOM (so Excel shows accents correctly).

## Reading files people re-saved

Input CSVs are read as UTF-8 (with or without BOM), UTF-16, or, if neither, Windows-1252 (Excel's plain "CSV") with a
warning. Undecodable bytes become `\ufffd` instead of stopping the run. Column headings are cleaned the same way
(stray padding like `"Faculty "` is ignored).

## Spreadsheet safety

A cell that begins with `=`, `+`, `-`, `@`, tab or carriage return would be run as a formula by Excel/Google Sheets.
In **every CSV this program writes**, such a text cell gets a leading apostrophe (`'=HYPERLINK(...)`); plain numbers
are left alone. When the program reads its own files back it removes that apostrophe again.
