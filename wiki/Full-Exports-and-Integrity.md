# Full Exports and Integrity

The Senate, SABP and AGM exports are meant to be re-run as they grow. **Always give the full export**, not just the new
responses. Every run reads the whole export and checks every application in it; nothing is skipped because an earlier
run saw it.

## Edits are newer submissions

Google Forms gives an edited or re-submitted response a **new timestamp**. Every application has an internal `uuid`
(timestamp + the student's identity key), so an edit is simply a newer application, and the old row is no longer in the
export. Per student the newest verified application is binding and older ones are `superseded` (below), so there is
nothing to "correct in place". To answer a "more information" email, the student edits or re-submits the form.

The timestamp decides which submission is the newest. If some cannot be read (the expected form is
`2026/08/14 1:30:47 p.m. AST`), the run logs a warning with examples, and for those the order in the file decides.

## What happens to each run

1. Read and clean the export, build the `uuid` of every row (the last row wins when two share a `uuid`).
2. **Verify every application** against the roster. See [Identity and Verification](Identity-and-Verification).
3. A verified application is **never downgraded** (Senate and SABP): if the roster is re-imported and a student who was
   verified no longer matches, the application stays verified and a `roster_mismatch` alert is raised. Only unverified
   applications can become verified. **AGM is not sticky**: its status follows the roster on every run.
4. Choose the binding application per student ([below](#one-binding-application-per-student)).
5. Save. The **Senate and AGM tables are dropped and created again from their schema, in one transaction** (so rows that
   are no longer in the export, and tables from an older version, are gone). The SABP
   table is **kept** (it holds the committee's work), see [SABP](#sabp-keeps-the-committees-work).
6. Write the outputs and the alerts files.

`--reimport_uoft_data` replaces the roster; because every run verifies everything, the next run uses it.

### Statuses

| `status` | Meaning |
|---|---|
| `verified` | matched to the roster |
| `unverified` | could not be matched (with a reason) |
| `superseded` | an older application replaced by a newer one from the same student |
| `non_binding` | a newer **unverified** submission that did not replace a verified one (warning raised) |
| `not_considered` | SABP only: the student already has a completed application for that term |

## One binding application per student

One application is binding per student (the same identity key, so normally the same email; for SABP, per `terms` value):
the **newest verified** one. A newer unverified submission never replaces a verified one: it is `non_binding` and raised
as a warning, because someone may be using the student's details. If nobody has a verified application, the newest one is
used. Older submissions that lose are `superseded`.

> For SABP the "term" is the whole `terms` answer (for example `Fall 2026;Winter 2027`). Applications from one student
> whose term selections are different but share a term (`Fall 2026` and `Fall 2026;Winter 2027`) do not replace each
> other; they are raised as an `overlapping_terms` alert for a person to decide.

## SABP keeps the committee's work

The `student_aid` table is not rebuilt, because it holds batch slots, reviewer ratings and decisions. Every application
keeps its batch and record. New applications start a new batch once the last one is full or has been assigned to
reviewers, and otherwise top it up. Slots of superseded applications are never reused. Reviewer sheets, decisions and
awarded amounts are never touched.

A stored SABP application that is **no longer in the export** (the student edited or re-submitted it, or the row was
deleted) stays as it is, with its batch, ratings and decision:

- if the student has a newer application for the term it is `superseded` by it;
- if it is still verified it is raised as a `missing_from_export` alert for a person.

**Completed applications are final.** An application is complete once the committee's decision is recorded (`decided`
stage; `COMPLETE_STAGE` in `sap.py` is the single definition), even if its row has left the export. Any other application
of the student for that term becomes `not_considered`. Other terms are unaffected. See
[Student Aid Bursary Program](Student-Aid-Bursary-Program).

## Alerts files

Every run writes `alerts_senate.csv`, `alerts_sap.csv` and `alerts_agm.csv` (header only when there is nothing). Columns:
`severity, kind, program, uuid, student_uuid, detail`. The integrity checks live in `src/utsu_core/integrity.py`.

| Severity | Kind | When |
|---|---|---|
| ERROR | `duplicate_verified` | more than one verified application for a student (per term for SABP) |
| WARNING | `roster_mismatch` | an application that was verified before no longer matches the roster (Senate, SABP); it stays verified |
| WARNING | `ambiguous_match` | AGM: more than one student fits the RSVP, so it cannot be verified |
| WARNING | `missing_from_export` | SABP: a verified application whose row is not in the export any more |
| WARNING | `overlapping_terms` | SABP: one student's applications name different but overlapping terms |
| WARNING | `non_binding` | an unverified submission that did not replace a verified one |
| WARNING | `not_considered` | an application ignored because a completed one exists |
