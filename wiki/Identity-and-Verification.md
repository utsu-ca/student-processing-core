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
  that **gains, loses or changes its email** gets new ids. Do not rely on ids outside the database.
- The email is typed by the applicant and not checked. Someone could use another student's email; that cannot replace a
  verified application (see [Incremental Processing](Incremental-Processing-and-Integrity#one-binding-application-per-student)),
  but someone who also knows the name and last four digits would still pass. The warnings in the log are how you notice.

## How a person is matched to the roster

The roster (`uoft_data`) stores **only the last 4 digits** of the student number, plus name, faculty and division.
Each application is matched using whatever it gives (a form may omit either column; at least one must exist):

| Given | Matches when | `verification_level` | `match_method` |
|---|---|---|---|
| name + student number | both belong to the same roster student (a wrong number never falls back to the name) | **2** | `name + student number` |
| name only | the name belongs to exactly **one** student | **1** | `name only` |
| student number only | the 4-digit number belongs to exactly **one** student | **1** | `student number only` |
| + checked in person | recorded with `--record_physical` | **3** | |

Reasons an application stays `unverified`:

| Reason | Meaning |
|---|---|
| `name not found` | no roster student has that (normalized) name |
| `student number does not match` | name exists, but none of those students has that last-4 |
| `name shared by N students; a student number is needed` | name only, several students |
| `student number not found` | number only, nobody has it |
| `student number shared by N students; a name is needed` | number only, several students |
| `no name or student number given` | nothing to match on |
| `name has no plain letters (only A-Z letters are supported)` | the name was entirely non-ASCII |

The student number accepted can be a full 10-digit number, the `xxxxx3971x` truncated form, or just the 4 digits.
Unreadable numbers are simply "not a match", never a crash.

### Verification levels at a glance

- **Level 1**: one factor. Weakest; flagged in AGM as `name_only_match` / `student_number_only_match`.
- **Level 2**: name + last four digits.
- **Level 3**: level 2 plus a person checked them in. Never goes down on reprocessing.

## Physical verification

```powershell
python -m utsu_core.main --record_physical door.csv
```

`door.csv` needs one way to find each student: `student_uuid`, `email`, `student_number` (or `id`), or `full_name`. An
optional `checked_in` (or `physical`) column limits it to rows that say yes (blank, `no`, `n`, `0`, `false` are skipped), so
a filled-in `agm_list_all.csv` works as it is. Note that the exported `checked_in` column is **blank** until
someone fills it in; a file where every row is blank records nothing.

The student is looked up by the same key the application was filed under (email, else student number, else name). All
their **verified, non-held level-2** applications in all three programs are raised to level 3. A student with no
verified application, or only level 1, is reported and left as they are.

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
as UTF-8.

## Reading files people re-saved

Input CSVs are read as UTF-8 (with or without BOM), UTF-16, or, if neither, Windows-1252 (Excel's plain "CSV") with a
warning. Undecodable bytes become `\ufffd` instead of stopping the run. Column headings are cleaned the same way
(stray padding like `"Faculty "` is ignored).

## Spreadsheet safety

A cell that begins with `=`, `+`, `-`, `@`, tab or carriage return would be run as a formula by Excel/Google Sheets.
In **every CSV this program writes**, such a text cell gets a leading apostrophe (`'=HYPERLINK(...)`); plain numbers
are left alone. When the program reads its own files back it removes that apostrophe again.
