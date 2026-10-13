# Testing and Development

## Tests

```powershell
python -m pytest                          # tests/ (179 tests at the time of writing)
python -m pytest --doctest-modules src    # doctests embedded in the source (39)
```

`pyproject.toml` sets `testpaths = ["tests"]` and `pythonpath = ["src"]`. With uv: `uv run --extra dev python -m pytest`.

| File | Covers |
|---|---|
| `test_identity.py` | typed ids, matching levels, physical check-in, backups |
| `test_unicode.py` | ASCII cleaning, odd inputs, roster import of accented names |
| `test_incremental.py` | re-runs, edited rows, held applications, decisions |
| `test_completed.py` | completed applications are final per term |
| `test_stages.py` | SABP stages, merge, decisions |
| `test_sap.py`, `test_wfni.py` | batches, reviewers, redaction, WFNI |
| `test_agm.py` | AGM lists and verification |
| `test_mail.py` | templates, recipients, notices, review prompt, send loop |
| `test_lookup.py` | lookup tables |

Not covered by tests (see [Code Review](Code-Review#test-gaps)): the `main.py` CLI and config merge, failed roster
imports, `seating.py`, `SmtpSender`, and the "no student number column" SABP export.

## Fake data

`python -m utsu_core.main --testing` generates and runs everything (see [Getting Started](Getting-Started#try-it-with-fake-data-no-personal-data-needed)).
`python testing/generate_fake_data.py` regenerates just the data: the roster first (from the seats file), then Senate,
SABP and AGM exports that each draw applicants from the roster. Flags: `--only`, `--seed`, `--students`, `--sabp-rows`,
`--sabp-unmatched`, `--sabp-start`, `--sabp-end`, `--agm-rows`. Per-form logic is in `testing/generate_fake_*.py`. The
generators are seeded, so output is reproducible.

## Conventions

- Python 3.12+, type hints on public functions, docstrings with doctests for pure helpers.
- No runtime dependencies; keep it that way unless there is a strong reason.
- Never write personal data to the log. Application ids are fine; names, emails and student numbers are not.
- Any CSV that people open must be written with `SafeDictWriter` / `SafeWriter`, and read with `open_csv`.
- Paths: use `absfile(...)`, never the current directory.
- New behaviour on re-runs needs a test in `test_incremental.py` style: build the DB in `tmp_path`, run twice.

## Publishing this wiki

These pages live in `wiki/` in the main repository so they are reviewed with the code. To publish:

```powershell
git clone https://github.com/utsu-ca/student-matching.wiki.git ..\student-matching.wiki
Copy-Item wiki\*.md ..\student-matching.wiki\
cd ..\student-matching.wiki
git add . ; git commit -m "Update wiki" ; git push
```

(The wiki must have at least one page created in the GitHub UI first so the wiki repository exists.) Links use GitHub
wiki page names (`[Text](Page-Name)`), which resolve on the wiki but not when browsing the `wiki/` folder on the code
side. `_Sidebar.md` and `_Footer.md` are picked up automatically by GitHub. Mermaid diagrams render on GitHub.
