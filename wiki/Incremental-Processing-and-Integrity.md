# Incremental Processing and Integrity

The Senate, SABP and AGM exports are meant to be re-run as they grow. **Always give the full export**, not just the new
responses (a partial export is not supported: its outputs would replace the earlier ones).

## What happens to each row

Every row gets an internal `uuid` (timestamp + the student's identity key) and a `source_hash`, a fingerprint of the
form fields the program uses (columns it ignores do not count, so adding a form question does not make every row look
edited).

| The application is | When it is in the export again |
|---|---|
| **new** | verified (and, for SABP, scored and batched) |
| **unverified**, unchanged | left alone, not retried |
| **unverified**, edited | **processed again**. This is how a correction is made: fix the row (same timestamp and email) |
| **verified** (and everything after: in review, reconciled, decided), unchanged | left alone, keeps its result |
| **verified**, edited | **immutable**: not processed again; becomes *invalid* and is **held** |
| **superseded** or **not considered** | left alone |

A run logs `N new, M verified now, K changed since processed`. The same counts are in `sap_run.json` /
`agm_run.json` as `new_submissions_this_run` and `processed_this_run`. Output files are rebuilt from the whole export.

`--reimport_uoft_data` verifies everything again against the new roster, except an edited verified row (which stays
locked).

### Statuses

| `status` | Meaning |
|---|---|
| `verified` | matched to the roster |
| `unverified` | could not be matched (with a reason) |
| `superseded` | an older application replaced by a newer one from the same student |
| `non_binding` | a newer **unverified** submission that did not replace a verified one (warning raised) |
| `not_considered` | SABP only: the student already has a completed application for that term |

## One binding application per student

One application is binding per student (the same identity key; for SABP, per `terms` value): the **newest verified**
one. A newer unverified submission never replaces a verified one: it is stored as `non_binding` and raised as a
warning, because someone may be using the student's details. If nobody has a verified application, the newest one is
used. Older submissions that lose are `superseded`.

> For SABP the "term" is the whole `terms` answer (for example `Fall 2026;Winter 2027`), compared as a string. An
> application for `Fall 2026` and another for `Fall 2026;Winter 2027` are different "terms" to the program. See
> [Code Review](Code-Review#medium).

## Completed applications are final (SABP)

An application is complete once the committee's decision is recorded (`decided` stage; `COMPLETE_STAGE` in `sap.py` is
the single definition). Once a student has a complete application for a term, any other application of theirs for that
term, new or in progress, becomes `not_considered`: not verified, scored or batched, and raised as a warning. Other terms
are unaffected.

## Invalid applications are held

If a verified application's form row is edited after it was processed, the integrity check
(`src/utsu_core/integrity.py`) marks it **invalid** (`invalid_reason = 'row changed after processing'`, flag `invalid`) and
logs an `INTEGRITY` error on every run. The application **stays in progress** (status and stage do not change, it keeps
its result) but goes no further. It is left out of:

- SABP review batches, the merge of reviewer ratings, recording decisions;
- Senate `verified.csv` (and therefore the seating);
- the AGM attendee lists.

A person decides:

- **Put the row back as it was**: the hold is released by itself on the next run; or
- **The edit is fine**: `python -m utsu_core.main --resolve_invalid <id>[,<id>...]` lifts the hold, and the next run
  takes the edited row as the new baseline.

### Duplicate verified applications

The same check reports more than one verified application for one student (and, for SABP, the same term) as
`duplicate_verified`. There should be exactly one.

## Alerts files

Every run writes `alerts_senate.csv`, `alerts_sap.csv` and `alerts_agm.csv` (header only when there is nothing). Columns:
`severity, kind, program, uuid, student_uuid, detail`.

| Severity | Kind | When |
|---|---|---|
| ERROR | `invalid` | a held application, listed until resolved |
| ERROR | `duplicate_verified` | more than one verified application for a student (per term for SABP) |
| WARNING | `non_binding` | this run: an unverified submission that did not replace a verified one |
| WARNING | `not_considered` | this run: an application ignored because a completed one exists |

## Handling a student's reply to "more information"

Correct the student's row in the form export (same timestamp and email). An unverified application is then processed
again. If it was already verified, the edit makes it invalid and held, which is exactly what the alert is for.

## SABP batch behaviour on re-runs

Every SABP application keeps its batch and record number. New applications start a new batch once the last one is full
or has been assigned to reviewers, and otherwise top it up. Slots of superseded applications are never reused. Reviewer
sheets, decisions and awarded amounts are never touched. See [Student Aid Bursary Program](Student-Aid-Bursary-Program).
