# Code Review

**Scope:** all of `src/` (`utsu_core`, `utsu_std`, `utsu_mail`, schema), `data/`, `templates/`, and the tests.
**Method:** read the code, ran the test suite and doctests, ran the `--testing` pipeline end to end, and wrote small
scripts to reproduce suspected defects before fixing them.

## Summary

The code base is in good shape for its size: small modules with clear responsibilities, careful handling of messy
real-world input (encodings, look-alike characters, spreadsheet formula injection), consistent backups and a test suite
that passes. The design is explained in [Architecture](Architecture).

This review found defects mostly at the edges (failure paths, documented behaviour that the code did not match). They
have been fixed, and one design decision changed: **the incremental machinery was removed** (see below).

## Design change: full exports

Google Forms stamps an edited or re-submitted response with a new timestamp, so a form row is never edited in place:
an edit is a newer submission. The code used to fingerprint every row, remember what it had processed, lock verified
rows and hold edited ones as "invalid". That is gone. Every run reads the full export and verifies every application in
it; per student the newest verified application is binding. See [Full Exports and Integrity](Full-Exports-and-Integrity).

Decisions that came with it:

- Senate and AGM tables are rebuilt from the export on every run.
- SABP keeps its table (batches, ratings, decisions). A stored application that left the export is kept, superseded if the
  student has a newer one, and raised as an alert if it is still verified.
- A verified application is never downgraded for Senate and SABP (a `roster_mismatch` alert is raised); AGM follows the
  roster on every run.
- Editing `data/wfni_weights.csv` changes the scores on the next run (intended).

## Findings and status

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| H1 | High | Roster import | A failed or duplicate-row import left an empty `uoft_data` that later runs trusted | **Fixed**: the file is validated first, the replacement is one transaction, duplicates are kept once, and an empty table counts as missing |
| H2 | High | SABP | Crash if the export has no `Student Number` column | **Fixed** |
| N1 | High | Config | A malformed `config.json` was read as `{}` and then overwritten, losing the `smtp` section | **Fixed**: the run stops and the file is left alone; writes are atomic |
| N2 | High | Identity | An edited email gave a new application id and left the old one verified | **Resolved** by the full-export design: the edit is a newer submission and supersedes the old row |
| M1 | Medium | WFNI | Prefix matching scored unknown answers (`"10"` dependents scored as `1`) | **Fixed**: only options written with a trailing `*` match by prefix |
| M2 | Medium | Logging | `--verbosity` compared as a string | **Fixed**: a map of names, any case, unknown values refused |
| M3 | Medium | SABP | "Per term" is the whole `terms` string | **Mitigated**: overlapping selections raise an `overlapping_terms` alert for a person |
| M4 | Medium | Matching | Same name and same last 4 digits picked the first student | **Fixed earlier**: the match is refused as ambiguous |
| M5 | Medium | CLI | A missing roster file exited with status 0 | **Fixed**: raises `FileNotFoundError` |
| N3 | Medium | SABP | Editing the weights changes stored scores | **Intended** (scores are recomputed every run) |
| N4 | Medium | Mail | A failed reconnect left no client and aborted `send_all` | **Fixed** |
| N5 | Medium | Integrity | An edited superseded row was held without an alert | **Gone** with the hold machinery |
| N6 | Medium | CLI | `--program all` aborted when the Senate export was missing | **Fixed**: Senate and seating are skipped with a warning |
| N7 | Medium | SABP | Redaction missed names shorter than 3 letters and accented spellings | **Fixed** |
| L1 | Low | SABP | `parse_amount` accepted `nan`, `inf`, negatives | **Fixed** (zero is allowed for an awarded amount) |
| L2 | Low | AGM | "not in person" counted as in person | **Fixed** |
| L3 | Low | SABP | Reviewer name `..` survived; ratings accepted from any folder | **Fixed** |
| L4 | Low | SABP | `inf` rating crashed the merge | **Fixed** |
| L5 | Low | SABP | Re-recording a decision silently replaced it and cleared the amount | **Fixed**: warns, keeps the amount for the same decision |
| L6 | Low | Testing | Old reviewer sheets survived `--testing` | **Fixed** |
| L7 | Low | Config | A flag equal to its default could not override the config | **Fixed**: flags given on the command line win |
| N12 | Low | Config | `--program` was saved and reused | **Fixed**: never saved |
| L8 | Low | Cleanup | Dead code | Done earlier; `import_csv_to_db` also removed |
| L9 | Low | Docs | Out-of-date flags and descriptions | **Fixed** |
| L10 | Low | Privacy | `student_aid` stored the full student number | **Fixed**: only the last four digits |
| N8, N9 | Low | Roster | Blank line crashed the import; cleaned copy left on disk | **Fixed** (no copy is written any more) |
| N10 | Low | Mail | A shared name asked for the number again | **Fixed** (the email still says which detail failed, by design) |
| N11 | Low | SABP | A rating that was blanked stayed in `sap_review` | **Fixed** |
| L11 | Low | Packaging | `data/`, `templates/`, `src/schema/` are found through the project root, so only an editable install works | Open, documented in [Getting Started](Getting-Started#requirements) |
| L12 | Low | Roster | The roster uuid joins the values without a separator | Open (negligible: other fields differ) |

### AGM trace (second pass)

| # | Severity | Issue | Status |
|---|---|---|---|
| A1 | High | Any email was accepted, so one student could be listed twice through two addresses (`@mail.` and `@utoronto.ca`) | **Fixed**: only `@mail.utoronto.ca` verifies, blank included. Two different `@mail.` addresses for one student (a typo) can still both verify; check the door list for repeated names |
| A2 | Medium | Non-UofT addresses verified | **Fixed** with A1 |
| A3 | Medium | The database was replaced before the files were written; a file open in Excel stopped the run half way | **Fixed**: `write_rows` writes to a temporary file and moves it into place, and the run checks every output is writable before changing anything |
| A4 | Low | "None" / "N/A" counted as accommodation needs | **Fixed** |
| A5 | Low | Accommodations are in door-list order | Accepted |
| A6 | Low | Unreadable timestamps fell back to file order silently; `0:05 a.m.` crashed | **Fixed**: one warning with examples; such a timestamp is unreadable, not an error |
| A7 | Low | An empty export raised a bare `StopIteration` | **Fixed**: `setup_reader` raises `ValueError("... is empty: it has no header row")` |
| A8 | Low | Output CSVs had no BOM, so Excel showed accented names badly | **Fixed**: `write_rows` writes UTF-8 with a BOM; `open_csv` reads it back |
| A9 | Low | A rebuilt table kept an older shape | **Fixed**: dropped and created again in one transaction |
| A10 | Low | Strict form assumptions (required headers, division codes) | Accepted, given a correct `conversion_table_agm.csv` |

## Residual risks

- A name plus four digits is a deliberately weak match (level 2). Level 3 (name, number and division) exists for cases
  that matter.
- Application and student ids are UUIDv5 (SHA-1) of the timestamp and email; with a guess of both they can be confirmed.
  They are database keys, not secrets.
- The "more information" email says which detail could not be verified, which lets someone learn whether a name is on
  the roster. That is what the email is for; keep the recipient list to the applicants' own addresses.
- Seating results for a growing list of applicants are not stable between runs (a new applicant changes the random
  tie-breaks); run seating once on the final list.

## Things done well

- **Deterministic by design:** content-derived UUIDv5 ids and a seeded seating make re-runs explainable.
- **Careful input handling:** encoding detection, ASCII folding, header cleaning, tolerant lookup tables.
- **Security hygiene:** spreadsheet-formula neutralization everywhere, read-only SQL for mail queries
  (`mode=ro` + SELECT check), header-injection protection (subjects collapsed, addresses with control characters
  rejected), SMTP passwords never read from config, credentials refused over plain text to remote hosts, confirmation
  word before sending, resume log.
- **Human-in-the-loop controls** at the decisions that matter (alerts, closed batches, never overwriting reviewer
  sheets).
- **Seating** is deterministic and the strict-priority cost construction is sound (each tier outweighs all lower tiers
  combined; ties are broken by a seeded integer).
- Good docstrings with doctests; the tests read like specifications.

## Test gaps

- The argparse entry point of `utsu_core.main` (the pieces are tested, the wiring is not).
- `seating.py`: add property tests (each applicant at most once, no constituency over capacity, nobody seated where
  they marked none, same seed gives the same result, delegates take division seats).
- The real `SmtpSender` connect and login rules, perhaps with a fake `smtplib`.
