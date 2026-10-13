# Operations Runbook

A checklist for running a real cycle. Replace paths as needed; all commands run from any directory once the package is
[installed editable](Getting-Started#install).

## 0. Before anything

- [ ] Work only on a trusted machine. Everything personal stays under `secrets/` (gitignored).
- [ ] `data/seats_senate.csv` has the **real** seat counts (the shipped file is a placeholder).
- [ ] Form column headings still match `data/conversion_table_*.csv`.
- [ ] `data/wfni_weights.csv` matches the current rubric (SABP).
- [ ] Roster CSV validated: it has `First Name`, `Last Name`, `Truncated Student Number`, `Faculty`, `Division`, and no
      identical duplicate rows (see the caution in [Input and Output Files](Input-and-Output-Files#roster-import-behaviour)).

## 1. Import the roster and verify applications

```powershell
python -m utsu_core.main --db secrets/db.sqlite --import_uoft_data secrets/uoft_data.csv `
    --verify_student secrets/senate.csv --sap_file secrets/sabp.csv --agm_file secrets/agm.csv `
    --reviewers "Ann,Bo,Cy"
```

Then read the log and these files in `secrets/output/`:

- [ ] `alerts_*.csv`: anything with `ERROR` needs a person; see [alerts](Incremental-Processing-and-Integrity#alerts-files).
- [ ] `*_run.json`: do the counts look right (rows read, verified, unverified)?
- [ ] `unverified.csv` / `sap_unverified.csv` / `agm_unverified.csv`: why (`reason` column)? Typos are fixed by editing
      the row in the form export, not in the program.

## 2. Update as responses arrive

Export the **full** form again and re-run the same command (flags are remembered in `config.json`). New rows are processed;
unverified rows that were edited are retried; verified rows are left alone. See [Incremental Processing](Incremental-Processing-and-Integrity).

If the roster itself changed: add `--reimport_uoft_data` (this verifies everything again).

## 3. Handle problems

| Situation | Do this |
|---|---|
| Student replied to a "more information" email | Correct their row in the form export (same timestamp and email) and re-run |
| `INTEGRITY ... invalid` alert | Decide: restore the row (auto-released) or `--resolve_invalid <id>` |
| `non_binding` warning | An unverified submission used a verified student's email. Check it is not impersonation |
| `duplicate_verified` | Two verified applications for one student (per term): investigate in the database |
| Wrong run | Restore the matching file from `<db folder>/backups/` |

## 4. Senate

- [ ] After the final run, `seats.csv`, `unseated.csv`, `seating_run.json` (note the **seed**) are produced.
- [ ] Review `Unfilled seat(s)` and `Delegate ... not drawn` warnings.
- [ ] Email results with `senate_verified` / `senate_rejection` ([Emailing](Emailing)).

## 5. SABP

1. Run with `--reviewers` (or run without and hand the sheets out yourself, then `--issue_batches all`).
2. Send each reviewer **only** their folder `sap_batches/by_reviewer/<name>/`. They fill in `need_level` (0-5) and `notes`
   in the `.csv` and return it to the same place.
3. `python -m utsu_core.main --program sap --merge_reviews`, then resolve `discuss` and `incomplete` rows in
   `sap_merged_ratings.csv`.
4. Add `decision` (and `awarded_amount`) to that file or a new one, then
   `python -m utsu_core.main --program sap --record_decisions decisions.csv`.
5. Decided applications are complete and final for their term.

## 6. AGM

1. Run with `--agm_file`. Hand out `agm_list_all.csv` (or `agm_list_in_person.csv`) to the door team and
   `agm_accommodations.csv` (no names) to the organizers.
2. Check `alerts_agm.csv` for `ambiguous_match` warnings (an RSVP that fits more than one student) and fix those rows
   with a student number or division.

## 7. Emails

Always `--dry_run` first, then send. See [Emailing](Emailing#safety-workflow).

```powershell
python -m utsu_mail.notices agm more_info
python -m utsu_mail agm_more_info --recipients secrets/output/notices_agm_more_info.csv --dry_run
```

## 8. After the cycle

- [ ] Archive the database, backups and `secrets/output/` per your retention policy, then delete working copies.
- [ ] `--keep_backups N` trims old backups on later runs.
- [ ] Do not commit anything under `secrets/`.

## Quick troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing required names in columns: {...}` | A form heading changed: update the left column in the lookup table |
| `The export needs at least one of these columns to identify people` | The export has no name or student-number column |
| Everything is `unverified` | The roster table is empty or the wrong file was imported; check `SELECT COUNT(*) FROM uoft_data` |
| `Seats file not found, skipping seating` | `--seats_file` path wrong |
| `FileNotFoundError` for the Senate file on an SABP-only run | Use `--program sap` (the default `all` always runs Senate) |
| Reviewer sheet warning "no longer matches batch" | The batch changed after issue; the reviewer's sheet is left untouched on purpose |
| `Config file not found` | `config.json` is created on the first normal run |
