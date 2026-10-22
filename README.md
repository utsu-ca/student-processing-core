# student-matching

Full documentation (getting started, runbook, command-line reference, per-program guides, data model, architecture and
a code review) is in the [`wiki/`](wiki/Home.md) folder, which is written to be published as the GitHub Wiki. This
README is the detailed reference for how the processing behaves.

## Setup

From the project root (any Python 3.12+; there are no runtime dependencies):

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

The editable install puts `utsu_core`, `utsu_std` and `utsu_mail` on the import path, so every `python -m ...` command
below works from any directory. Relative paths in flags and `config.json` (`data/`, `secrets/`, `testing/`,
`templates/`) are always taken from the project root, never from the working directory.

## Updating with new data

The student data (`--import_uoft_data`) is a one-shot: it is imported only when the database has no (or an empty)
`uoft_data` table. Pass `--reimport_uoft_data` to replace it. The file is checked first, so a bad file leaves the
previous student data in place. Without `--testing`, settings come from `config.json` (flags override it, even when
equal to the default); `--program`, `--merge_reviews` and `--reimport_uoft_data` are never saved there.

The Senate, SABP and AGM form exports are meant to be re-run as they grow: give the **full export** each time. Every run
reads the whole export and verifies every application in it. Google Forms stamps an edited or re-submitted response
with a **new timestamp**, so an edit is simply a newer application. Every application has an internal typed `uuid` (see
[Identity and verification levels](#identity-and-verification-levels)), recorded in `senate_application`, `student_aid`
or `agm_rsvp`:

- the **Senate and AGM tables are rebuilt** from the export on every run (a row that is no longer in the export is
  gone);
- the **SABP table is kept**, because it holds the committee's work (batches, ratings, decisions);
- a **verified application is never downgraded** (Senate and SABP): if the student data changes and a verified student no
  longer matches, it stays verified and a `roster_mismatch` alert is raised. Only unverified applications can become
  verified. **AGM is not sticky**: its status follows the student data on every run.

**One binding application per student** (the same email; for SABP, per term): the newest *verified* one. A newer
unverified submission never replaces a verified one: it is stored as `non_binding` and raised as a warning (someone may
be using the student's details). With no verified application the newest one is used. Older submissions that lose are
`superseded`. SABP applications of one student with different but overlapping term selections (`Fall 2026` and
`Fall 2026;Winter 2027`) do not replace each other: an `overlapping_terms` alert asks a person to decide.

**Completed applications are final (SABP).** An application is complete once the committee's decision is recorded (the
`decided` stage: it passed verification, the reviewers' need levels, the reconciliation and the decision, and is ready
for disbursement, which this program does not track). Once a student has a complete application for a term, any other
application of theirs for **that term**, new or still in progress, is `not_considered`: it is raised as a warning. Other
terms are unaffected. (`COMPLETE_STAGE` in `sap.py` is the one place that says what complete means.)

**SABP applications that leave the export.** A stored SABP application that is no longer in the export (the student
edited or re-submitted it, or the row was deleted) is kept as it is, with its batch, ratings and decision. If the student
has a newer application for the term it is `superseded` by it; if it is still verified it is raised as a
`missing_from_export` alert.

Every run writes `alerts_senate.csv`, `alerts_sap.csv` and `alerts_agm.csv` (header only when there is nothing) listing
what a person has to look at: duplicates (`duplicate_verified`, ERROR), roster mismatches, missing-from-export and
overlapping-term applications, and this run's non-binding and not-considered applications (WARNING).

For SABP, every application keeps its batch and record. New applications start a new batch once the last one is full
or has been assigned to reviewers, and otherwise top it up; slots of superseded applications are never reused. Reviewer
sheets, decisions and awarded amounts are never touched.

A partial export (only the new responses) is not supported: the Senate and AGM outputs would lose the earlier ones.

**Handling a student's reply** to a "more information" email: the student edits or re-submits the form. Export the full
form again and re-run.

## Identity and verification levels

**IDs are internal database keys that say what they are.** Every id is written `<kind>:<uuid>`, for example
`application:sap:9c1d...`, `student:email:3f2a...` or `roster:77b0...` (the rows of `uoft_data`), so its type travels with
it (in SQLite, `uuid LIKE 'student:%'` selects a kind). Each kind has its own UUID namespace, so the same text hashed as
two kinds never gives the same uuid. `student_uuid` is derived from the student's **email** (kind `student:email`); an
application's `uuid` from its submission timestamp and the same email, per program (`application:senate|sap|agm`).
When a row has no email the student number is used (`student:student_number`), and when it has neither the name
(`student:name`), so the program still works with whatever the form collects, and the kind shows which one was used.
Case, spacing and look-alike text are ignored (the text is made ASCII, see below). Because the key is the email, editing a name or student
number in a row does not change its ids; but a row that gains, loses or changes its email gets new ids, so do not rely
on them outside the database.

The email is typed by the applicant and not checked, so someone could submit using another student's email. That cannot
replace a verified application: a new submission with the student's email only becomes binding if it is itself verified.
Someone who also knows the student's name and number would still pass; the warnings in the log are the way to notice.

**Verification needs a name; the student number and the division only narrow it down.** The roster keeps just the last
four digits of a student number, which many students share, so a number (or a division) alone never identifies anyone.
Each application is matched against `uoft_data` on its name, plus its student number and division when it gives them (a
form must have a name column; the number and division columns are optional). Every factor given must agree with the
same student, and a wrong value never falls back to another factor. The `verification_level` is the number of factors
that matched:

| factors that matched | `verification_level` | `match_method` |
|---|---|---|
| name | 1 | `name only` |
| name + student number, or name + division | 2 | `name + student number`, `name + division` |
| name + student number + division | 3 | `name + student number + division` |

The roster's division is its `division`, or its `faculty` when the division is blank
(the Senate form's divisional codes are colleges and faculties), compared as a code (`UC - University College` is `UC`).
The SABP form has no division, so its highest level is 2. A row that has a student number but no name is kept, left
`unverified` (`no name given`) and asked for its name.

The matching `uoft_data.uuid` is stored in `uoft_uuid` and the level in `verification_level`, on each application row
(`senate_application`, `student_aid`, `agm_rsvp`), next to the email-based `student_uuid`. An application that cannot
be decided stays `unverified` with a reason (`name not found`, `no name given`, `student number does not match`,
`division does not match`, `name shared by N students; a student number is needed`, ...).

**Backups.** Every run that uses a database first copies it to `<database folder>/backups/<name>_<YYYYmmdd-HHMMSS>.sqlite`
(SQLite's backup, so the copy is consistent). `--backup_dir DIR` changes the folder, `--keep_backups N` keeps only the
newest N (0 keeps all), and `--no_backup` skips it for one run. Backups hold the same private data as the database.

**Spreadsheet safety.** Anything an applicant typed can start with `=`, `+`, `-` or `@`, which Excel and Google Sheets run
as a formula. In every CSV this program writes, such a text cell gets a leading apostrophe (`'=HYPERLINK(...)`), so it
shows as text; plain numbers are untouched. The program removes the apostrophe again when it reads its own files back.

**ASCII only.** The program works in plain ASCII and does not support other scripts. Everything it compares or
derives from a name, email or student number is made ASCII first (`clean_text` / `normalize_case` in
`utsu_std/utils.py`): accents are removed (`José García` becomes `Jose Garcia`, so it matches a roster that has no
accents, and the roster itself is stored this way), look-alike forms become the plain ones (fullwidth letters,
ligatures, superscript digits, non-breaking spaces), curly apostrophes and every kind of dash are written the plain way
(`O’Neil` matches `O'Neil`), runs of spaces collapse, and anything else that is not ASCII (other scripts, emoji,
zero-width and control characters) is dropped. `ß` becomes `ss`, `Æ` becomes `AE`, `Ø` becomes `O`, and so on. So a
name written only in another script has nothing left: it cannot be verified (the reason says so and the "more
information" email asks for the name again), but it still gets its own ids, so two such students are never mistaken for
one. Student numbers typed with fullwidth or superscript digits, or with a stray invisible character, give the same
4 digits; nonsense numbers are simply not a match, never a crash. An email address with an invisible character is not
valid for sending.

Free text the applicant writes (for example the SABP general statement) is not changed, so reviewers read what was
written; it is written to the output files as UTF-8 with a BOM, so Excel shows accents correctly. Files that people re-save from a spreadsheet are read whether they
are UTF-8, UTF-16 or Excel's plain "CSV" (Windows-1252, with a warning); bytes that are not text become `\ufffd` instead
of stopping the run.

Student names and emails are not written to the log (the old `--log_sample` and `--dry_run` flags of `utsu_core.main`
are gone).

## Testing mode

```
python -m utsu_core.main --testing
```

This first generates the fake data (the generators are in `testing/`), then runs the whole pipeline on it. The fake
roster and form exports (`fake_uoft_data.csv`, `fake_senator.csv`, `fake_sabp.csv`, `fake_agm.csv`) are written to
`testing/output/` along with a fresh database, the log, and the results (`verified.csv`, `unverified.csv`,
`seats.csv`, `unseated.csv`, `seating_run.json`). That folder is gitignored and rebuilt every run; the generators are
seeded, so the data is the same each time. The pipeline uses the seats in `data/seats_senate.csv`. `config.json` is
not read or changed, and flags such as `--seed` or `--seats_file` still override the defaults.

`python testing/generate_fake_data.py` regenerates just the fake data, with other settings if you want: the student
roster (from the seats file) first, then the Senate, SABP and AGM form exports, each drawing its applicants from the
roster. Use `--only` to pick some, `--students` to set the roster size (it must be at least `--sabp-rows`), and
`--help` for the seed and row counts. The shared code lives in that file; the per-form logic is in
`testing/generate_fake_*.py`.

## Student Aid Bursary Program (SABP)

`utsu_core/sap.py` processes the SABP form export (Stages 1-3 of SAS-002). From `src/`:

```
python -m utsu_core.main --program sap --sap_file path/to/export.csv [--batch_size 50]
```

(`--testing` supplies `testing/output/fake_sabp.csv`.) Column names come from `data/conversion_table_sap.csv`; columns not
listed there are ignored. For each student (by email) one application is binding (the newest verified one, see above), then it:

- **verifies** against `uoft_data` by name, student number or both (a form may omit either column; see
  [Identity and verification levels](#identity-and-verification-levels)), and records one `student_aid` row per
  application (internal `uuid`, `student_uuid`, `uoft_uuid`, `verification_level`, stream, requested amount, WFNI, flags,
  batch/record). Reprocessing updates those rows but never overwrites `awarded_amount` or `decision`;
- **scores** the Weighted Financial Need Index for living-costs applications from the weights in
  `data/wfni_weights.csv` (`field,answer,points`; edit it and re-run, no code change), and adds review
  flags (`membership_not_confirmed`, `amount_over_max`, `instant_approval_eligible`, `high_wfni`, `no_statement`, ...).
  The file is re-read whenever it changes and its hash is saved in `sap_run.json`. Answers are matched to the form's own
  option text (case, spacing and "to" vs "-" ignored); a reworded or mistyped option is not scored and flags the
  application `wfni_incomplete`. The maximum score and the `high_wfni` threshold (over half the maximum) follow from the
  weights. A re-run recomputes `wfni` for every application, but review sheets already handed out keep the old score;
- **writes** to `<output_dir>`: `sap_verified.csv` / `sap_unverified.csv` (private: names, ID, email),
  `sap_run.json`, and `sap_batches/batch_NNN.csv` + `.txt`: anonymized batches of at most 50 applications, oldest
  first, with the general statement, requested amount, WFNI and blank `need_level` / `notes` columns for reviewers.
  Names, emails, phone numbers, student numbers, postal codes and links are redacted from statements; the
  committee-facing files carry only the application `uuid` and a record id like `B001-07`. Faculty is left out.

**Two reviewers per batch.** Pass `--reviewers "Ann,Bo,Cy"` (two or more names; testing mode uses `Reviewer_A,B,C`).
Each batch is assigned to two of them, the least loaded first, and saved in `sap_batch_reviewer`, so existing batches
keep their reviewers when more are added. Each reviewer gets their own copy in
`sap_batches/by_reviewer/<name>/batch_NNN.csv` (and `.txt` to read): they fill in `need_level` (0-5) and `notes` and
return the file to the same place. Reprocessing never overwrites a reviewer's sheet; if a batch has changed since it was
issued (e.g. the last, partial batch grew) a warning says so and merging reports the missing ratings.

Once the sheets are filled in, merge them:

```
python -m utsu_core.main --program sap --merge_reviews
```

This writes `sap_merged_ratings.csv` (and the `sap_review` table): per application the reviewers, each rating, the mean,
the spread, a `suggested_amount` (the mean of the rubric maximums, capped at the amount requested) and a `status`:
`agreed`, `discuss` (levels differ by 2 or more), `incomplete` (one rating) or `unrated`. Invalid levels and unknown
applications are logged as warnings. Only the reviewers assigned to a batch count.

## SABP stages and decisions

Every verified application has a `stage` in `student_aid` (empty for unverified ones), moved forward by the program and
never backwards by reprocessing:

| stage | set when |
|---|---|
| `scored` | verified against `uoft_data` and scored (WFNI, flags), in a batch not yet handed out |
| `in_review` | its batch is handed to reviewers: `--reviewers` assigns it, or `--issue_batches all` (or `1,2`) records a hand-out |
| `discuss` / `reconciled` | `--merge_reviews` finds the two levels 2 or more apart / in agreement (one rating stays `in_review`) |
| `decided` | `--record_decisions decisions.csv` (columns `uuid`, `decision`, optional `awarded_amount`; other columns, such as those of `sap_merged_ratings.csv`, are ignored) |

A batch that has left `scored` is closed: new applications start a new batch, and its blank sheets in `sap_batches/` are
no longer rewritten. Rows that cannot be recorded (unknown or unverified application, missing
decision, bad amount) are listed and skipped; a decision recorded before reconciliation is accepted with a warning (e.g.
instant approvals). `decided` is the last stage: the application is complete and ready for disbursement, which this
program does not track (see "Completed applications are final" above). `sap_run.json` counts the verified applications
per stage. Databases from before this column get it filled in on the next run (`decided` if there is a decision,
`in_review` if the batch has reviewers, else `scored`).

```
python -m utsu_core.main --program sap --issue_batches all          # sheets were handed out without --reviewers
python -m utsu_core.main --program sap --merge_reviews
python -m utsu_core.main --program sap --record_decisions decisions.csv
```

## AGM voter RSVPs

`utsu_core/agm.py` processes the AGM voter RSVP export. From `src/`:

```
python -m utsu_core.main --program agm --agm_file "path/to/RSVP.csv"
```

(`--testing` supplies `testing/output/fake_agm.csv`; `python testing/generate_fake_data.py --only agm` regenerates it.) Columns come from
`data/conversion_table_agm.csv`. For each person (by email) one RSVP is binding (the newest verified one, see above), verified against `uoft_data`:

- **Only a `@mail.utoronto.ca` email counts.** An RSVP with a blank email or another domain (even `@utoronto.ca`) is
  unverified (`email is not a mail.utoronto.ca address`);
- the form may have **no student number**, so people are then matched on their ACORN-registered name alone
  (`name_only_match` flag, level 1). A name shared by several students stays unverified (`name shared by N students`)
  until the form adds a `Student Number` or `Division` column, which is then used automatically (level 2 or 3).
  The form must have the name columns; a student number alone never matches anyone;
- **ambiguous cases are warned about.** An RSVP that fits more than one student is logged
  (`AMBIGUOUS AGM RSVP ... more than one student matches`), flagged `ambiguous_name`,
  counted as `ambiguous_matches` in `agm_run.json` and listed in `alerts_agm.csv` (kind `ambiguous_match`, severity
  WARNING) on every run until it is resolved, e.g. by correcting the row with a student number or division. It is
  never put on the door list;
- every RSVP gets an `agm_rsvp` row (uuid, `student_uuid`, `uoft_uuid`, `verification_level`, status, reason, flags; no
  names or emails);
- `<output_dir>` gets these files (all but `agm_accommodations.csv` carry names and emails: keep them private):

  | file | what is in it |
  |---|---|
  | `agm_list_all.csv` | the **door list**: every verified attendee, sorted by last name. Columns `last_name, first_name, student_number, preferred_name, pronouns, email, meeting_preference, contact_ok, flags, verification_level, checked_in`. `student_number` is the truncated number from `uoft_data`. `meeting_preference` is just **`In-person`** or **`Virtual`**: anything that is not clearly in person, including a blank answer, is Virtual. `checked_in` is a blank column for the door |
  | `agm_list_in_person.csv` | the same columns, only the people who are `In-person` |
  | `agm_unverified.csv` | RSVPs that could not be verified, with the `reason`; keeps the ids (`uuid`, `student_uuid`) the "more information" emails use |
  | `agm_accommodations.csv` | dietary and accessibility needs of verified attendees, by `uuid` only, no names; answers such as `None` or `N/A` are not needs and are left out |
  | `alerts_agm.csv` | what a person has to look at: duplicate, ambiguous-match and non-binding RSVPs |
  | `agm_run.json` | counts (below) |

  The `meeting_preference`
  column in every AGM file uses the same two values; the database keeps the answer as it was given.

  `agm_run.json` keys: `submissions_in_export` (rows read), `binding_rsvps` (one per person, which the lists are built
  from), `verified_rsvps`, `verified_in_person`, `unverified_rsvps`, `attendance` (In-person / Virtual counts),
  `match_method`, `unverified_reasons`, `dietary_requests`, `accessibility_requests`, `flags`, `non_binding`,
  `integrity_duplicates` and `alerts`. `sap_run.json` uses the same names (`binding_applications`,
  `verified_applications`, ...).

**Verification on its own.** The same module (`utsu_core/agm.py`) can be run without producing any
lists or database rows, to check an RSVP export against a database that already holds `uoft_data`:

```
python -m utsu_core.agm --agm_file "path/to/RSVP.csv" --db path/to/db.sqlite [--output_dir DIR]
python -m utsu_core.agm --testing     # testing/output/fake_agm.csv against the last `main --testing` database
```

It writes `agm_verification.csv` (status, reason, match method and flags per person).

## Emailing (`utsu_mail`)

Mail-merge emails sent directly over SMTP:

```
python -m utsu_mail --list_fields                 # templates + columns from src/schema/*.sql
python -m utsu_mail senate_verified --dry_run     # render a preview, never connects to SMTP
python -m utsu_mail senate_verified               # preview, confirm, send
```

**Templates** live in `templates/email/`. `<name>.txt` starts with a `Subject: ...` line, a blank line, then the
plain-text body; an optional `<name>.html` is added as the HTML version (values are HTML-escaped). Placeholders are
`{{ field }}`. To change an email, edit the file; to add one, drop in a new `.txt`.

**Recipients** come from a CSV (default `<output_dir>/verified.csv`, or `--recipients`) or a read-only
`--query "SELECT ... FROM uoft_data ..."` against `--db`; each column is a field, so alias columns to match a template.
Templates are checked against the recipient columns before anything is sent, and the error points at the schema table
that has a missing field. Rows with a bad/duplicate address or a blank field a template needs are skipped and listed.

**SMTP** settings go in the `smtp` section of `config.json` (`host`, `from_addr`, and optionally `port`, `security`
= `starttls`|`ssl`|`none`, `username`, `from_name`, `reply_to`, `delay`). The password is never read from config: set
`UTSU_SMTP_PASSWORD` or you will be prompted.

**Safety:** a sample is shown first; type `send` to confirm (`t` mails the sample to the sender address only, `r` shows
another sample). Every outcome is appended to `<output_dir>/mail_log_<template>.csv` (with the `application_uuid` and
`student_uuid` when the recipient file has them), and addresses already sent are
skipped on rerun unless `--resend` is given.

**Rejections and requests for more information** (AGM, SABP, Senate): templates `agm_more_info`, `agm_rejection`,
`sabp_more_info`, `sabp_rejection`, `senate_more_info` and `senate_rejection`. Every subject carries
`[Ref <application uuid>]` and every body lists the application uuid, the student uuid and the submission timestamp.
Both ids are internal database keys, kept in the email only as a reference for replies (a reply is matched back by the
student's email, student number or name, not by the id). The application uuid is the `uuid` of the application row
(program + submission timestamp + the student's email); the student uuid is derived from the student's email, so it is
the same before and after verification (the link to `uoft_data` is stored in `uoft_uuid`). `notices` builds the
recipient file, which also lists which details could not be verified (name, student number) for `more_info`, judged
from what the application actually gave:

```
python -m utsu_mail.notices senate more_info                          # reads <output_dir>/unverified.csv
python -m utsu_mail.notices sabp more_info                            # reads <output_dir>/sap_unverified.csv
python -m utsu_mail.notices agm rejection --reason "Not a UTSU member"  # reads <output_dir>/agm_unverified.csv
python -m utsu_mail senate_more_info --recipients <output_dir>/notices_senate_more_info.csv
```

`--input` and `--output` override the files, and `--db` the database holding `uoft_data`.

Tests: `python -m pytest` (the `pythonpath` and test folder are set in `pyproject.toml`).