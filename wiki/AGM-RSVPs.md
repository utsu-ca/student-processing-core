# AGM RSVPs

`src/utsu_core/agm.py` processes the AGM voter RSVP export and does its verification.

```powershell
python -m utsu_core.main --program agm --agm_file "secrets/RSVP.csv"
```

## Form export

Columns are renamed through `data/conversion_table_agm.csv`:

| Form column | Internal name | Notes |
|---|---|---|
| `Timestamp` | `submission_ts` | required |
| `Username` | `personal_email` | required |
| `Preferred Name`, `Preferred Pronouns` | `preferred_name`, `pronouns` | required |
| `ACORN-registered First Name`, `...Last Name` | `first_name`, `last_name` | optional if there is a `Student Number` column |
| `UofT Email Address` | `email` | required |
| `Meeting Preference` | `meeting_preference` | required |
| `Do you have any dietary restrictions or food allergies?` | `dietary` | required |
| `Accessibility Requirements` | `accessibility` | required |
| `Data Processing Confirmation` | `data_confirmation` | required |
| `Contact Consent` | `contact_consent` | required |
| `Student Number` | `student_number` | **optional extra column**; used automatically when present |

At least one of the name columns or `Student Number` must exist.

## Verification

- With **no student number** column, people are matched on their ACORN-registered name alone (level 1, flag
  `name_only_match`).
- A name shared by several roster students stays unverified (`name shared by N students`, flag `ambiguous_name`) until
  the form adds `Student Number` or `Division`, which is then used (level 2 or 3).
- A row with a number and no name stays unverified (`no name given`): a student number alone never matches anyone.
- **Ambiguous cases raise a warning.** An RSVP that fits more than one student is logged
  (`AMBIGUOUS AGM RSVP ... more than one student matches`), counted as `ambiguous_matches` in `agm_run.json`, and listed
  in `alerts_agm.csv` (kind `ambiguous_match`, severity WARNING) on every run until resolved, e.g. by correcting the row
  with a student number or division. It never reaches the door list.

### Flags

| Flag | Meaning |
|---|---|
| `name_only_match` | matched on name alone |
| `ambiguous_name` | name shared by several students |
| `no_data_confirmation` | the data-processing confirmation is not "yes..." |
| `no_uoft_email` | no email given |

## Outputs (in `--output_dir`)

| File | Contents |
|---|---|
| `agm_list_all.csv` | **Door list**: every verified attendee, sorted by last name. Columns `last_name, first_name, preferred_name, pronouns, email, meeting_preference, contact_ok, flags, verification_level, checked_in`. No ids. `checked_in` is a blank column for the door. |
| `agm_list_in_person.csv` | The same columns, only people attending `In-person`. |
| `agm_unverified.csv` | RSVPs that could not be verified, with `reason`. Keeps `uuid` and `student_uuid` for the "more information" emails. |
| `agm_accommodations.csv` | Dietary and accessibility needs of verified attendees, by `uuid` only, no names, so a need cannot be traced to a person. |
| `alerts_agm.csv` | Duplicate, ambiguous-match and non-binding RSVPs. |
| `agm_run.json` | Counts (below). |

All but `agm_accommodations.csv` carry names and emails: keep them private.

**`meeting_preference`** is written as just `In-person` or `Virtual`. In-person means the answer contains "in person" /
"in-person" (case-insensitive); everything else, including a blank answer, is `Virtual`. The database keeps the answer as
given. A negated answer such as "not in person" is virtual; fixed options on the form avoid any doubt.

The `agm_rsvp` table is rebuilt from the export on every run, and the status follows the roster on every run (AGM is not
sticky).

### `agm_run.json`

`submissions_in_export`, `binding_rsvps`, `verified_rsvps`,
`verified_in_person`, `unverified_rsvps`, `ambiguous_matches`, `attendance` (In-person / Virtual counts), `match_method`,
`unverified_reasons`, `dietary_requests`, `accessibility_requests`, `flags`, `non_binding`, `kept_verified_despite_roster`,
`integrity_duplicates`, `alerts`. (`sap_run.json` uses the same names with `applications` instead of
`rsvps`.)

## Door check-in

Print or open `agm_list_all.csv` and mark `checked_in` for people who arrive. The program does not read it back.

## Verification on its own

```powershell
python -m utsu_core.agm --agm_file "secrets/RSVP.csv" --db secrets/db.sqlite [--output_dir DIR]
python -m utsu_core.agm --testing
```

Writes `agm_verification.csv` (status, reason, match method, flags per person) and no lists or database rows.
