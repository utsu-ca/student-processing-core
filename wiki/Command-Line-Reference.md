# Command-Line Reference

There is one command per program, and `utsu-all` for the three together (`python -m utsu_programs.<senate|sap|agm>` and
`python -m utsu_programs` are the same commands). They share the common and infrastructure flags below and each
takes only the flags of its own program(s):

| Command | Program | Its own flags |
|---|---|---|
| `utsu-senate` | Senate verification and seating | `--verify_student`, `--seats_file`, `--seed` |
| `utsu-sap` | Student Aid Bursary Program | `--sap_file`, `--batch_size`, `--reviewers`, `--merge_reviews`, `--issue_batches`, `--record_decisions` |
| `utsu-agm` | AGM voter RSVPs | `--agm_file`, `--verify_only` |
| `utsu-all` | all three, in one run | all of the above |

Run on its own, a program needs its export (`--verify_student`, `--sap_file` or `--agm_file`) and stops with an error if
it is missing. With `utsu-all`, SABP and AGM run only if their file is given, and Senate (and seating) is skipped, with a
warning, if `--verify_student` does not exist.

### Program flags

| Flag | Default | Meaning |
|---|---|---|
| `--testing` | off | Generate fake data, run on it, write everything to `testing/output/`. Ignores `config.json`. |
| `--verify_student` | `secrets/verify.csv` | **Senate** application export (CSV). Despite the name, this is the Senate form export. |
| `--sap_file` | *(none)* | SABP form export. Required by `utsu-sap`. |
| `--agm_file` | *(none)* | AGM RSVP export. Required by `utsu-agm`. |
| `--verify_only` | off | `utsu-agm` only: report the verification (`agm_verification.csv`) and write no lists or database rows. A one-off action. |
| `--batch_size` | `50` | Applications per SABP review batch. |
| `--reviewers` | *(none)* | Comma-separated committee members (2+). Each SABP batch is assigned to two of them. |
| `--seats_file` | `data/seats_senate.csv` | Seats per constituency (see [Senate and Seating](Senate-and-Seating#seats-file)). |
| `--seed` | `2026` | Seed for seating tie-breaks. Same inputs + seed = same seating. |

### Common flags

| Flag | Default | Meaning |
|---|---|---|
| `--import_uoft_data` | `secrets/uoft_data.csv` | Student roster CSV. Imported **only** if the database has no `uoft_data` table, or it is empty. A bad file leaves the previous roster in place. |
| `--reimport_uoft_data` | off | Replace the roster with `--import_uoft_data`. Every run verifies the whole export, so the next run uses it. Never saved to `config.json`. |
| `--output_dir` | `secrets/output` | Where result files are written. |

### One-off actions

These work on an existing database, do their one job and exit; they are never saved to `config.json`. If several are
given, only the first in this order runs: `--merge_reviews`, `--issue_batches`, `--record_decisions`.

| Flag | Argument | Meaning |
|---|---|---|
| `--merge_reviews` | *(switch)* | Merge reviewer sheets into `sap_merged_ratings.csv` and the `sap_review` table; advance stages. Only the batch's assigned reviewers count. |
| `--issue_batches` | `all` or `1,2` | Record that batch sheets were handed out outside the program. Those batches are then closed. |
| `--record_decisions` | CSV path | Columns `uuid`, `decision`, optional `awarded_amount` (`0` is allowed). Moves applications to `decided`; recording again replaces the decision (with a warning). |
| `--verify_only` | *(switch)* | See above (`utsu-agm`). |

### Infrastructure flags

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `secrets/db.sqlite` | SQLite database file. Created if missing. |
| `--config` | `config.json` | JSON settings file. A file that cannot be read stops the run and is left unchanged. |
| `--verbosity` | `TRACE` | `TRACE`, `DEBUG`, `INFO`, `WARNING` (or `WARN`), `ERROR` or `CRITICAL`, in any case. Anything else is refused. |
| `--log_file` | `secrets/app.log` | Log (appended). Names and emails are not logged; application ids are. |
| `--backup_dir` | *(next to the db, in `backups/`)* | Where timestamped database backups go. |
| `--keep_backups` | `0` | Keep only the newest N backups (`0` keeps all). |
| `--no_backup` | off | Skip the pre-run backup for this run only. |

### Configuration and flag precedence

For a normal (non-testing) run the final settings are built as:

1. a flag **given on the command line** wins (even if its value equals the built-in default);
2. otherwise the value in `config.json`;
3. otherwise the built-in default.

Then **`config.json` is rewritten** with the result (except the one-off actions, `--reimport_uoft_data` and
`--no_backup`). Consequences worth knowing:

- Last run's `--sap_file`, `--reviewers`, etc. are remembered and reused automatically on the next run.
- Relative paths are saved as typed and resolved from the project root when used.
- `config.json` is gitignored. It may also hold the `smtp` section used by [Emailing](Emailing#smtp-settings); that
  section is preserved when the file is rewritten.
- Keys not used any more (`parse_students`, `log_sample`, `dry_run`) can be deleted from an old `config.json`.

Example `config.json`:

```json
{
  "verify_student": "secrets/senate.csv",
  "sap_file": "secrets/sabp.csv",
  "agm_file": "secrets/agm.csv",
  "db": "secrets/db.sqlite",
  "import_uoft_data": "secrets/uoft_data.csv",
  "output_dir": "secrets/output",
  "seed": 2026,
  "verbosity": "INFO",
  "log_file": "secrets/app.log",
  "smtp": { "host": "smtp.example.org", "from_addr": "elections@example.org" }
}
```

### Backups

Before every run that uses a database, a consistent copy (SQLite's online backup API) is written to
`<database folder>/backups/<name>_<YYYYmmdd-HHMMSS>.sqlite`; two runs in one second get a `_1`, `_2` suffix. Nothing is
written if the database does not exist yet. Backups hold the same private data as the database.

## `utsu-agm --verify_only`

Verify an AGM RSVP export against a database that already holds `uoft_data`, without writing lists or database rows
(it writes `agm_verification.csv`). It uses `--agm_file`, `--db` and `--output_dir` as above; with `--testing` it uses
`testing/output/fake_agm.csv` and `testing/output/test.sqlite` (left by the last `--testing` run).

## `python -m utsu_mail`

See [Emailing](Emailing#command-reference).

## `python testing/generate_fake_data.py`

| Flag | Default | Meaning |
|---|---|---|
| `--only` | all | Any of `students`, `senate`, `sabp`, `agm` |
| `--seed` | fixed | Seed for reproducible data |
| `--students` | roster size default | Roster size (must be at least `--sabp-rows`) |
| `--sabp-rows` | default | SABP rows |
| `--sabp-unmatched` | `3` | Percentage of SABP rows that do not match the roster |
| `--sabp-start`, `--sabp-end` | `2026-09-16`, `2026-10-09` | SABP application window |
| `--agm-rows` | `200` | AGM RSVPs before duplicates |

Run `--help` for the exact current defaults.
