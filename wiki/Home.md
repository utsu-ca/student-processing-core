# UTSU Student Matching

`utsu-student-matching` is the toolkit the UTSU uses to **verify applicants against the University of Toronto student
roster** and to run the processes built on top of that verification. It has no runtime dependencies (Python 3.12+ and
the standard library only; SQLite is the database).

| Program | What it does | Page |
|---|---|---|
| **Senate applications** | Verifies applicants, then seats them in Senate constituencies (deterministic, seeded) | [Senate and Seating](Senate-and-Seating) |
| **Student Aid Bursary Program (SABP)** | Verifies, scores (WFNI), cuts anonymized review batches, merges reviewer ratings, records decisions | [Student Aid Bursary Program](Student-Aid-Bursary-Program) |
| **AGM voter RSVPs** | Verifies RSVPs and produces door lists, an accommodations list and an unverified list | [AGM RSVPs](AGM-RSVPs) |
| **Mail merge (`utsu_mail`)** | Sends verified / more-info / rejection emails over SMTP with a preview-and-confirm step | [Emailing](Emailing) |

## Where to start

- **First time?** [Getting Started](Getting-Started) (install, run the fake-data demo, first real run).
- **Running a real cycle?** [Operations Runbook](Operations-Runbook) is the step-by-step checklist.
- **Looking up a flag?** [Command-Line Reference](Command-Line-Reference).
- **Wondering what a file or column is?** [Input and Output Files](Input-and-Output-Files) and [Data Model](Data-Model).
- **Why was an application superseded / not considered / flagged?** [Full Exports and Integrity](Full-Exports-and-Integrity).
- **How is somebody matched to the roster?** [Identity and Verification](Identity-and-Verification).
- **Changing the code?** [Architecture](Architecture) and [Testing and Development](Testing-and-Development).
- **Code review findings:** [Code Review](Code-Review).

## Ideas that explain most of the behaviour

1. **One roster, three forms.** `uoft_data` (the roster) is imported once. Every form export (Senate, SABP, AGM) is
   matched against it by name, by the last 4 digits of the student number, or both.
2. **Give the full export every time.** Every run re-reads the whole form export and verifies every row. Google Forms
   stamps an edit or re-submission with a new timestamp, so an edit is just a newer application.
3. **A verified application is never downgraded** (Senate and SABP). If the roster changes and a verified student no
   longer matches, the application stays verified and an alert is raised. AGM follows the roster on every run.
4. **One binding application per student** (per term for SABP): the newest *verified* one. An unverified newcomer can
   never replace a verified application.
5. **Private by default.** Outputs with names and emails stay in `secrets/` (gitignored). Committee-facing files are
   anonymized; every CSV is made spreadsheet-safe against formula injection.
6. **Everything is backed up.** Every run that touches the database first writes a consistent timestamped copy.

## Repository layout

```
src/utsu_core/   verification pipeline (main.py is the CLI)
src/utsu_std/    shared helpers: text cleaning, ids, database, safe CSV
src/utsu_mail/   mail-merge CLI and notice builder
src/schema/      SQLite table definitions
data/            lookup tables, seats, WFNI weights
templates/email/ email templates
testing/         fake-data generators (output goes to testing/output/)
tests/           pytest suite
wiki/            these pages
```

> The wiki pages live in the repository's `wiki/` folder. To publish them, copy the files into the GitHub wiki
> repository (`utsu-ca/student-matching.wiki.git`) and push. See [Testing and Development](Testing-and-Development#publishing-this-wiki).
