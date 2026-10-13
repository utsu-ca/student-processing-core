# Input and Output Files

## Inputs

| File | Used for | Notes |
|---|---|---|
| Roster CSV (`--import_uoft_data`) | `uoft_data` | Columns `First Name`, `Last Name`, `Truncated Student Number`, `Faculty`, `Division`. Imported **once**; it is read and checked first, so a bad file leaves the previous roster in place. Blank lines are skipped. |
| Senate export (`--verify_student`) | Senate | see [Senate and Seating](Senate-and-Seating#form-export) |
| SABP export (`--sap_file`) | SABP | see [Student Aid Bursary Program](Student-Aid-Bursary-Program#form-export) |
| AGM export (`--agm_file`) | AGM | see [AGM RSVPs](AGM-RSVPs#form-export) |
| `data/conversion_table_*.csv` | column names | below |
| `data/seats_senate.csv` | seating | [Seats file](Senate-and-Seating#seats-file) |
| `data/wfni_weights.csv` | SABP scoring | [WFNI](Student-Aid-Bursary-Program#wfni-weighted-financial-need-index) |
| `templates/email/*.txt` (+ `.html`) | mail | [Emailing](Emailing#templates) |

### Roster import behaviour

- The roster is imported **only when `uoft_data` does not exist** in the database, or with `--reimport_uoft_data`.
- Reimporting drops the table first and then verifies **every** application again (except edited verified rows).
- The roster file may be UTF-8, UTF-16 or Windows-1252 (see [Identity and Verification](Identity-and-Verification#reading-files-people-re-saved)).
- **Caution:** the table is dropped *before* the new file is validated and the import is not transactional. A
  roster file with missing columns, or with two identical rows, leaves an empty `uoft_data` table behind, which later runs
  treat as "already imported" (everything then fails to verify). Validate the file first and restore from
  `backups/` if this happens. See [Code Review](Code-Review#high).

### Lookup tables (form column names)

`data/conversion_table_senate.csv`, `..._sap.csv` and `..._agm.csv` map the **exact** heading of each form column
(left) to the internal name (right). Format:

```csv
Lookup Key, Replacement Key, Version 1
"Timestamp", "submission_ts"
"UofT Email Address", "email"
```

- The first row is a header and is skipped; the third column is only a label.
- Matching is exact (case-sensitive) after the program trims and cleans the form's headings (stray padding is fine).
- Columns not listed are ignored (and do not affect row fingerprints).
- If the form text changes, change the **left** column here; no code change is needed.
- A column missing from the export is an error naming the missing headings, except the optional/identity columns
  described on each program's page.

## Outputs (in `--output_dir`, default `secrets/output`)

| File | Program | Private? | Notes |
|---|---|---|---|
| `verified.csv`, `unverified.csv` | Senate | **yes** | names, student numbers, emails |
| `seats.csv`, `unseated.csv`, `seating_run.json` | Senate | names | |
| `sap_verified.csv`, `sap_unverified.csv` | SABP | **yes** | names, student numbers, emails |
| `sap_batches/batch_NNN.csv/.txt` | SABP | committee-safe | anonymized; ids only |
| `sap_batches/by_reviewer/<name>/...` | SABP | committee-safe | one copy per reviewer |
| `sap_merged_ratings.csv` | SABP | committee-safe | uuid + ratings |
| `sap_run.json`, `agm_run.json` | SABP, AGM | no personal data | counts only |
| `agm_list_all.csv`, `agm_list_in_person.csv`, `agm_unverified.csv` | AGM | **yes** | names, emails |
| `agm_accommodations.csv` | AGM | no names | by `uuid` only |
| `agm_verification.csv` | AGM (standalone) | **yes** | |
| `alerts_senate.csv`, `alerts_sap.csv`, `alerts_agm.csv` | all | ids only | rebuilt every run |
| `notices_<program>_<kind>.csv` | mail | **yes** | |
| `mail_log_<template>.csv` | mail | addresses | resume log |

Other generated files: `<db folder>/backups/*.sqlite` (private), the log file (`--log_file`; names/emails are not
written, application ids are), and `config.json` (may contain SMTP host/username).

## What is safe to share

- Safe for the committee: `sap_batches/**`, `sap_merged_ratings.csv`, `agm_accommodations.csv`, `*_run.json`.
- Never commit or email unprotected: anything marked **yes** above, the database and its backups, `config.json`.
- `.gitignore` already covers `secrets/*`, `*.sqlite`, `verified.csv`, `unverified.csv`, `config.json`,
  `*.log` and `testing/output/`. Other private files (for example `sap_verified.csv`) are only safe because they are
  written under `secrets/` by default. Do not point `--output_dir` at a tracked folder.
