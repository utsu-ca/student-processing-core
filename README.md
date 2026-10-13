# student-matching

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

The student data (`--import_uoft_data`) is a one-shot: it is imported only when the database has no `uoft_data` table.
Pass `--reimport_uoft_data` to replace it (everything is then verified again). Without `--testing`, settings come from
`config.json` (flags override it); `--merge_reviews` and `--reimport_uoft_data` are never saved there.

The Senate, SABP and AGM form exports are meant to be re-run as they grow: give the **full export** each time. Every
application has a deterministic `uuid`, recorded in `senate_application`, `student_aid` or `agm_rsvp`, so a run:

- **verifies only the new rows** (`N new, M already processed` is logged and `new` is in `sap_run.json` /
  `agm_run.json`); already-processed rows keep their stored result. Output files (`verified.csv`, the AGM lists, ...)
  are rebuilt from the whole export;
- marks an older submission `superseded` when the same student submits again (the newest is the one used);
- for SABP, keeps every application's batch and record. New applications start a new batch once the last one is full
  or has been assigned to reviewers, and otherwise top it up; slots of superseded applications are never reused. Reviewer
  sheets, decisions and awarded amounts are never touched.

A partial export (only the new responses) is not supported: its outputs would replace the earlier ones.

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
listed there are ignored. For each student only the newest application is kept, then it:

- **verifies** name + truncated student number against `uoft_data`, and records one `student_aid` row per application
  (deterministic `uuid`, matching `student_uuid`, stream, requested amount, WFNI, flags, batch/record). Reprocessing
  updates those rows but never overwrites `awarded_amount` or `decision`;
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
applications are logged as warnings.

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
no longer rewritten. Rows that cannot be recorded (unknown or unverified application, missing decision, bad amount) are
listed and skipped; a decision recorded before reconciliation is accepted with a warning (e.g. instant approvals).
`sap_run.json` counts the verified applications per stage. Databases from before this column get it filled in on the
next run (`decided` if there is a decision, `in_review` if the batch has reviewers, else `scored`).

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
`data/conversion_table_agm.csv`. Each person's newest RSVP is kept, then verified against `uoft_data`:

- the form has **no student number**, so people are matched on their ACORN-registered name (`name_only_match` flag). A
  name shared by several students stays unverified (`name shared by N students`) until the form adds a
  `Student Number` column, which is then used automatically (`name + student number`);
- every RSVP gets an `agm_rsvp` row (uuid, matching `student_uuid`, status, reason, flags; no names or emails);
- `<output_dir>` gets **two attendee lists**, sorted by last name with a blank `checked_in` column for the door:
  `agm_list_all.csv` (every verified attendee) and `agm_list_in_person.csv` (those whose Meeting Preference says
  "in person"; anything else, including a blank answer, is left off), plus `agm_unverified.csv` (with the reason),
  `agm_accommodations.csv` (dietary and accessibility needs of verified attendees, by uuid only, no names) and
  `agm_run.json` (counts by meeting preference, match method and flag). The lists carry names and emails: keep them private.

**Verification on its own.** The verification lives in `utsu_core/agm_verify.py` and can be run without producing any
lists or database rows, to check an RSVP export against a database that already holds `uoft_data`:

```
python -m utsu_core.agm_verify --agm_file "path/to/RSVP.csv" --db path/to/db.sqlite [--output_dir DIR]
python -m utsu_core.agm_verify --testing     # testing/output/fake_agm.csv against the last `main --testing` database
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
The application uuid is the `uuid` of the application row (derived from its submission timestamp); the student uuid is
`uoft_data.uuid` when the student can be matched fully or partly (by student number or name), otherwise a stable uuid
derived from the student number (the email for AGM). `notices` builds the recipient file, which also lists which
details could not be verified (name, student number) for `more_info`:

```
python -m utsu_mail.notices senate more_info                          # reads <output_dir>/unverified.csv
python -m utsu_mail.notices sabp more_info                            # reads <output_dir>/sap_unverified.csv
python -m utsu_mail.notices agm rejection --reason "Not a UTSU member"  # reads <output_dir>/agm_unverified.csv
python -m utsu_mail senate_more_info --recipients <output_dir>/notices_senate_more_info.csv
```

`--input` and `--output` override the files, and `--db` the database holding `uoft_data`.

Tests: `python -m pytest` (the `pythonpath` and test folder are set in `pyproject.toml`).