# Getting Started

## Requirements

- Python **3.12 or newer**. There are no runtime dependencies. `pytest` is the only optional (dev) dependency.
- The package must be installed **editable** (see below). The code finds `data/`, `templates/`, `src/schema/`,
  `secrets/` and `testing/` by walking up to the project root (the folder with `pyproject.toml`), so a regular
  (non-editable) install into `site-packages` will not find them.

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

With [uv](https://docs.astral.sh/uv/) instead: `uv run --extra dev python -m pytest` creates the environment on demand.

The editable install puts `utsu_core`, `utsu_std` and `utsu_mail` on the import path, so every `python -m ...` command
works from **any** directory. Relative paths in flags and in `config.json` are always resolved from the project root,
never from the current directory.

## Try it with fake data (no personal data needed)

```powershell
python -m utsu_core.main --testing
```

This generates a fake roster and fake Senate, SABP and AGM exports, then runs the whole pipeline on them. Everything is
written to `testing/output/` (gitignored, rebuilt each run, seeded so the data is identical every time):

| File(s) | From |
|---|---|
| `fake_uoft_data.csv`, `fake_senator.csv`, `fake_sabp.csv`, `fake_agm.csv` | the generators in `testing/` |
| `test.sqlite`, `test.log`, `backups/` | the run |
| `verified.csv`, `unverified.csv`, `seats.csv`, `unseated.csv`, `seating_run.json` | Senate + seating |
| `sap_*.csv`, `sap_batches/`, `sap_run.json` | SABP |
| `agm_*.csv`, `agm_run.json` | AGM |
| `alerts_senate.csv`, `alerts_sap.csv`, `alerts_agm.csv` | integrity checks |

In testing mode `config.json` is neither read nor written, and a fresh database is built each time. Flags such as
`--seed` or `--seats_file` still override the testing defaults. Reviewers `Reviewer_A,Reviewer_B,Reviewer_C` are
assigned automatically.

One-off actions (`--merge_reviews`, `--issue_batches`, `--record_decisions`, `--record_physical`, `--resolve_invalid`)
combined with `--testing` do **not** rebuild the fake data or the database; they work on the last testing run's:

```powershell
python -m utsu_core.main --testing --merge_reviews
```

> Known quirk: testing mode deletes and rebuilds the database but not `testing/output/sap_batches/by_reviewer/`.
> Reviewer sheets left from an older run are never overwritten (by design), so you may see "no longer matches batch N"
> warnings. Delete `testing/output/` to start completely clean. See [Code Review](Code-Review).

## First real run

1. Put the files in `secrets/` (gitignored): the roster export and the Senate / SABP / AGM form exports.
2. Fill in `data/seats_senate.csv` with the real seat counts. The shipped file is a **placeholder** (see the comment at
   its top).
3. Check that the form's column headings match the lookup tables in `data/` (see
   [Input and Output Files](Input-and-Output-Files#lookup-tables-form-column-names)).
4. Run, for example:

```powershell
python -m utsu_core.main --db secrets/db.sqlite --import_uoft_data secrets/uoft_data.csv `
    --verify_student secrets/senate.csv --sap_file secrets/sabp.csv --agm_file secrets/agm.csv `
    --reviewers "Ann,Bo,Cy" --output_dir secrets/output
```

The settings of a normal (non-testing) run are saved to `config.json`, so the next run only needs the flags that change.
See [Command-Line Reference](Command-Line-Reference#configuration-and-flag-precedence) for the precedence rules.

For the whole cycle (updating exports, reviews, decisions, door check-in, emails) see the
[Operations Runbook](Operations-Runbook).

## Running the tests

```powershell
python -m pytest                          # 179 tests in tests/
python -m pytest --doctest-modules src    # 39 doctests embedded in the source
```
