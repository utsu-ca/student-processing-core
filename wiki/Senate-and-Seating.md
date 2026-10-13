# Senate and Seating

`src/utsu_core/senate.py` verifies Senate applications; `src/utsu_core/seating.py` assigns seats to the verified
applicants. Both run for `--program senate` or `all`.

```powershell
python -m utsu_core.main --program senate --verify_student secrets/senate.csv --seats_file data/seats_senate.csv --seed 2026
```

## Form export

Columns are renamed through `data/conversion_table_senate.csv`:

| Form column | Internal name | Required? |
|---|---|---|
| `Timestamp` | `submission_ts` | yes |
| `Username` | `personal_email` | yes |
| `Full ACORN-registered Name` | `full_name` | optional (at least one of name / student number must exist) |
| `Student Number` | `id` | optional |
| `UofT Email Address` | `email` | yes |
| `Divisional Code` | `division` | yes |
| `Are you an delegated applicant?` | `delegated` | yes (anything starting with "yes" = delegate) |
| `Constituency Choice [<name>]` | `pref:<name>` | read by pattern; one column per constituency |

Timestamps look like `2026/08/14 1:30:47 p.m. AST` (the zone label is ignored). Preference answers are
`First-choice`, `Second-choice`, `Third-choice`, `Interested`, or anything else (e.g. `None`, blank) meaning "not
interested".

Rows with no name and no student number are skipped; a row with a student number but no name is kept and stays
unverified. Verification follows
[Identity and Verification](Identity-and-Verification); re-run behaviour follows
[Incremental Processing](Incremental-Processing-and-Integrity).

## Outputs (in `--output_dir`)

| File | Contents |
|---|---|
| `verified.csv` | One binding, verified, not-held application per student: `uuid, submission_ts, full_name, id, email, personal_email, division, delegated, student_uuid, uoft_uuid, verification_level, match_method, flags, invalid_reason`, then `pref:<constituency>` columns. **Private.** This is the seating input. |
| `unverified.csv` | Same columns for binding applications that could not be verified. **Private.** |
| `alerts_senate.csv` | Held / duplicate / non-binding applications ([alerts](Incremental-Processing-and-Integrity#alerts-files)) |
| `seats.csv` | `uuid, full_name, constituency, division, seat_type, preference` |
| `unseated.csv` | `uuid, full_name, reason` |
| `seating_run.json` | `seed, seats_file, applicants, seated, unseated, by_preference, unfilled_seats` |

Held (invalid) applications are left out of `verified.csv` and therefore out of the seating.

## Seats file

`data/seats_senate.csv` has columns `constituency,division,seats`. Lines starting with `#` are ignored.

- `Faculty/College` rows are **per division** and need a division code (`TRIN`, `UC`, ...).
- Every other constituency must **not** have a division.
- Duplicate rows, non-integers and negative counts are rejected.
- Constituency names must match the form's `Constituency Choice [...]` names. A seats-file name no applicant ranked
  produces a warning.
- `General Member` is the catch-all constituency.

> The shipped file is a **placeholder**. Replace the seat counts before a real run.

## Seating algorithm

Everyone is seated at most once. In priority order:

1. **Delegates** take the Faculty/College seats of their own division. If a division has more delegates than seats, the
   seeded RNG draws who gets them; the rest join the general pool (a warning is logged for each).
2. **Ranked seats**: all remaining seats except General Member are filled to maximize First-choices, then Second, then
   Third, then Interested (strict lexicographic priority, solved exactly with the Hungarian algorithm on integer
   costs). Nobody is placed in a constituency they did not rank. Faculty/College seats only go to applicants of that
   division.
3. **Leftover Faculty/College seats** go to unseated applicants of that division who ranked *General Member* (best
   General Member tier first). Seat type `general_member_division`.
4. **General Member seats** are the catch-all for everyone still unseated. Those who ranked General Member go first;
   others are seated with preference `Not ranked`.

Seat types in `seats.csv`: `delegate`, `ranked`, `general_member_division`, `general_member`.

**Determinism.** Applicants are sorted by `uuid` and ties are broken by `random.Random(seed)`, so the same inputs and
seed always give the same result. Record the seed (it is in `seating_run.json`) with the election records. Changing the
verified set (for example a new application arriving) can change who is drawn, because the random stream is consumed
in a different order.

Warnings you may see: delegates not drawn; `Unfilled seat(s)` (a seat nobody eligible could take); constituencies in the
seats file that nobody ranked.
