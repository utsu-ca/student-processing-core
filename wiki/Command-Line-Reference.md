# Command-Line Reference

## `python -m utsu_core.main`

### Processing flags

| Flag | Default | Meaning |
|---|---|---|
| `--testing` | off | Generate fake data, run on it, write everything to `testing/output/`. Ignores `config.json`. |
| `--program` | `all` | `senate`, `sap`, `agm` or `all`. With `all`, a program is run only if its file is given (Senate always runs: see note). |
| `--verify_student` | `secrets/verify.csv` | **Senate** application export (CSV). Despite the name, this is the Senate form export. |
| `--sap_file` | *(none)* | SABP form export. Required with `--program sap`. |
| `--agm_file` | *(none)* | AGM RSVP export. Required with `--program agm`. |
| `--import_uoft_data` | `secrets/uoft_data.csv` | Student roster CSV. Imported **only** if the database has no `uoft_data` table. |
| `--reimport_uoft_data` | off | Replace the roster with `--import_uoft_data` and verify every application again. Never saved to `config.json`. |
| `--batch_size` | `50` | Applications per SABP review batch. |
| `--reviewers` | *(none)* | Comma-separated committee members (2+). Each SABP batch is assigned to two of them. |
| `--seats_file` | `data/seats_senate.csv` | Seats per constituency (see [Senate and Seating](Senate-and-Seating#seats-file)). |
| `--seed` | `2026` | Seed for seating tie-breaks. Same inputs + seed = same seating. |
| `--output_dir` | `secrets/output` | Where result files are written. |

> **Note on `--program all`:** the Senate step runs whenever `program` is `senate` or `all`, using `--verify_student`.
> If that file does not exist the run stops with an error, so for SABP-only or AGM-only work pass `--program sap` or
> `--program agm`.

### One-off actions

These work on an existing database, do their one job and exit; they are never saved to `config.json`. If several are
given, only the first in this order runs: `--merge_reviews`, `--issue_batches`, `--record_decisions`,
`--resolve_invalid`, `--record_physical`.

| Flag | Argument | Meaning |
|---|---|---|
| `--merge_reviews` | *(switch)* | Merge reviewer sheets into `sap_merged_ratings.csv` and the `sap_review` table; advance stages. |
| `--issue_batches` | `all` or `1,2` | Record that batch sheets were handed out outside the program. Those batches are then closed. |
| `--record_decisions` | CSV path | Columns `uuid`, `decision`, optional `awarded_amount`. Moves applications to `decided`. |
| `--resolve_invalid` | comma-separated ids | A person accepts held (invalid) applications; the next run takes the edited row as the baseline. |
| `--record_physical` | CSV path | Students checked in person; raises verified level-2 applications to level 3. |

### Infrastructure flags

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `secrets/db.sqlite` | SQLite database file. Created if missing. |
| `--config` | `config.json` | JSON settings file. |
| `--verbosity` | `TRACE` | Use `INFO`, `DEBUG` or `TRACE`. Any other word currently falls back in a surprising way (see [Code Review](Code-Review#medium)). |
| `--log_file` | `secrets/app.log` | Log (appended). Names and emails are not logged; application ids are. |
| `--backup_dir` | *(next to the db, in `backups/`)* | Where timestamped database backups go. |
| `--keep_backups` | `0` | Keep only the newest N backups (`0` keeps all). |
| `--no_backup` | off | Skip the pre-run backup for this run only. |

### Configuration and flag precedence

For a normal (non-testing) run the final settings are built as:

1. a flag whose value **differs from its built-in default** wins;
2. otherwise the value in `config.json`;
3. otherwise the built-in default.

Then **`config.json` is rewritten** with the result (except the one-off actions, `--reimport_uoft_data` and
`--no_backup`). Consequences worth knowing:

- Last run's `--sap_file`, `--reviewers`, etc. are remembered and reused automatically on the next run.
- Because step 1 compares against the default, passing a flag **equal to its default** (for example `--seed 2026`) does
  not override a different value stored in `config.json`. Edit the file or pass a different value.
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

## `python -m utsu_core.agm_verify`

Verify an AGM RSVP export against a database that already holds `uoft_data`, without writing lists or database rows.

| Flag | Meaning |
|---|---|
| `--agm_file` | RSVP export (required unless `--testing`) |
| `--db` | Database with `uoft_data` (required unless `--testing`) |
| `--output_dir` | Where `agm_verification.csv` goes (default `secrets/output`) |
| `--testing` | Use `testing/output/fake_agm.csv` and `testing/output/test.sqlite` |

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
