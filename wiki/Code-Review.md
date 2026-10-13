# Code Review

**Scope:** all of `src/` (`utsu_core`, `utsu_std`, `utsu_mail`, schema), `data/`, `templates/`, `testing/` generators at a
skim level, and the existing tests.
**Method:** read the whole code base, ran the test suite (179 tests and 39 doctests, all passing), ran the `--testing`
pipeline end to end, and wrote small scripts to **reproduce** the suspected defects. Findings marked *reproduced* were
confirmed by running code; *by inspection* means read from the code but not executed.
**No source code was changed** as part of this review.

## Summary

The code base is in good shape for its size: small modules with clear responsibilities, thoughtful handling of messy
real-world input (encodings, look-alike characters, spreadsheet formula injection), idempotent re-runs driven by
content-derived ids, an integrity "hold" for edited rows, consistent backups, and a test suite that passes. The
design is explained in [Architecture](Architecture).

The issues below are mostly at the edges: failure paths that leave bad state, a couple of places where behaviour does
not match what the README promises, and some cleanup.

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| H1 | **High** | Roster import | A failed or duplicate-row import leaves an **empty `uoft_data`** that later runs trust; the old roster is dropped before the new file is validated | reproduced |
| H2 | **High** | SABP | Run **crashes** if the export has no `Student Number` column, although name-only matching is documented | reproduced |
| M1 | Medium | WFNI | Prefix matching scores unknown answers silently instead of flagging `wfni_incomplete` | reproduced |
| M2 | Medium | Logging | `--verbosity` comparison is a string comparison: `info`, `WARNING` and others become `TRACE` | reproduced |
| M3 | Medium | SABP | "Per term" means the whole `terms` string, so overlapping terms bypass the one-per-term / completed-is-final rules | by inspection |
| M4 | Medium | Matching | Two roster students with the same name and same last 4 digits: the first is picked silently | by inspection |
| M5 | Medium | CLI | `quit()` exits with status 0 on a missing roster file | by inspection |
| L1-L12 | Low | various | see [Low](#low) | mixed |

## High

### H1. A failed roster import leaves an empty roster that later runs trust

**Where:** `src/schema/uoft_data.sql` (`DROP TABLE IF EXISTS uoft_data`), `src/utsu_core/importing_data.py`
(`import_uoft_data`, `preprocess_uoft_csv`), `src/utsu_core/main.py` (`if reimport or not test`).

**What happens.** `import_uoft_data` runs the schema script first (which **drops** the existing table and commits), and
only then reads and validates the CSV. If validation fails (missing columns) or the insert fails, the new table is
empty or missing.

**Reproduced:**

- importing a valid file, then re-importing a file without the `Faculty`/`Division` columns raised `ValueError` and
  left `uoft_data` with **0 rows** (the previous roster was gone);
- a roster with two **identical rows** (same name, last-4, faculty and division give the same `roster:` uuid) raised
  `IntegrityError: UNIQUE constraint failed: uoft_data.uuid` and left an empty table.

**Why it matters.** `main()` decides whether to import by checking that the *table exists*. After a failed import the
table exists, so the next run logs "Using the UofT student data already in the database" and verifies **nobody**
(everything becomes `unverified`, and those unverified rows are then not retried until edited or `--reimport_uoft_data`
is used). The automatic backup is the only safety net.

**Suggested fix.**

1. Parse and validate the CSV **before** touching the database.
2. Do `DROP`/`CREATE`/`INSERT`/`CREATE INDEX` in **one transaction** so a failure rolls back to the old roster (avoid
   `executescript`, which commits implicitly).
3. Drop exact duplicate rows with a warning (or `INSERT OR IGNORE` and count them).
4. Treat an empty `uoft_data` as "missing" in `main()` and fail loudly rather than verifying against nothing.
5. Add tests for all three failure cases.

### H2. SABP export without a `Student Number` column crashes

**Where:** `src/utsu_core/sap.py` (`_UPSERT` binds `:id`; `save_to_db` builds the row dicts at the end of the module).

**What happens.** The identity columns are intentionally optional (`IDENTITY_FIELDS = ("id", "first_name", "last_name")`),
and name-only matching works, but `student_aid.student_number` is `NOT NULL` and the upsert binds `:id`, which only
exists when the column is in the export.

**Reproduced:** `process_sap` on the fake export with the `Student Number` column removed fails with
`sqlite3.ProgrammingError: You did not supply a value for binding parameter :id`. (The same experiment on the Senate
export works.)

**Suggested fix.** In `save_to_db` add `"id": a.get("id", "")` to the row dict (and decide whether the column should
stay `NOT NULL`). Add a test with a no-student-number SABP export, as there already is for AGM.

## Medium

### M1. WFNI answer matching is a prefix match for every question

**Where:** `src/utsu_core/wfni.py::points_for`.

The documentation (README, the comment at the top of `data/wfni_weights.csv`) says only the long "A negative amount
(...)" option matches by its start and that any other unlisted answer is "unknown" and flags `wfni_incomplete`. The code
falls back to `key.startswith(k)` for **all** tables.

**Reproduced** with the shipped weights: `external_supports = "None of the above"` scores **1** (matches `No`),
`disability_mobility = "Not sure"` scores **0**, and `dependents = "10"` scores **1** (matches `1`). The application is
not flagged, and the score can move across the `high_wfni` threshold.

**Suggested fix.** Allow prefix matching only where the weights file says so (for example an answer ending in `*`, or a
dedicated list), keep exact matching elsewhere, and add a test for an unlisted answer.

### M2. `--verbosity` is compared as a string

**Where:** `src/utsu_std/utils.py::setup_logging` (`elif verbosity >= 'TRACE'`).

`INFO` and `DEBUG` are matched exactly; anything else is compared alphabetically with `'TRACE'`.

**Reproduced:** `WARNING`→ `TRACE` (the *most* verbose level), `WARN`→`TRACE`, lowercase `info`/`debug` → `TRACE`,
while `ERROR` and `CRITICAL` → `WARNING`. A user lowering the noise gets more, and the log file grows.

**Suggested fix.** `level = {"TRACE": logging.TRACE, ...}.get(verbosity.upper(), logging.WARNING)` (or use
`logging.getLevelName`), and validate with argparse `choices`. Also `addLoggingLevel` raises `AttributeError` if
`setup_logging` is ever called twice in one process; make it idempotent.

### M3. SABP "term" is the whole `terms` string

**Where:** `src/utsu_core/sap.py::application_key`, `completion_key`, `completed_applications`; schema comment on
`terms`.

The form's `Application Semester` answer is a multi-select (`Fall 2026;Winter 2027` appears in the generated data). The
one-binding-per-term and "completed is final for that term" rules compare the cleaned **string**. A student with a
completed `Fall 2026` application who applies again for `Fall 2026;Winter 2027` is therefore treated as applying for a
different term and is considered normally; the same goes for `duplicate_verified` grouping by `terms`.

**Suggested fix.** Split terms (`split_multi`) and apply the rules per term, or document that students must apply with
identical term selections and have the form enforce it.

### M4. Ambiguous name + number match is resolved arbitrarily

**Where:** `src/utsu_core/verification.py::decide` (`hits[0]`).

When the name matches several roster students and **more than one** of them has the same last-4 digits (for example the
roster contains two "Ann Lee" with the same last 4), the lowest `uuid` is chosen and the application is verified at
level 2. The name-only and number-only paths correctly refuse to guess. The probability is small (roughly 1 in 10,000
per same-name pair) but it would be a silent mis-match in an identity process.

**Suggested fix.** If `len(hits) > 1` return an unverified `Match` with a reason such as
`name and student number match N students`.

*Residual risk to document and accept consciously:* matching on a name plus only 4 digits is a deliberately weak factor
(level 2). Level 3 (name, student number and division) exists for the cases that matter.

### M5. Missing roster file exits with status 0

**Where:** `src/utsu_core/main.py` (`quit()` when the roster file does not exist).

`quit()` with no argument raises `SystemExit(None)`, whose exit status is **0**. Any script or scheduler that checks the
exit code sees success. Raise an exception (as the "no import file" branch already does) or `sys.exit(2)`.

## Low

| # | Where | Issue |
|---|---|---|
| L1 | `sap.py::parse_amount` | `float()` accepts `nan`, `inf`, `-50` and `1e3` (*reproduced*). `nan`/`inf` slip past the `amount > max` and `< 150` checks and are written as `nan`/`inf`; a negative amount is flagged `instant_approval_eligible`. Reject non-finite and non-positive values (flag `no_amount`). |
| L2 | `agm.py::is_in_person` | "No, I will not be attending in person" is classified **In-person** (*reproduced*). Matters only if the form has a free-text answer; today's answers are fixed options. Match the specific option text instead of a regex. |
| L3 | `sap.py::reviewer_id`, `merge_reviews` | The reviewer name becomes a folder name: `..` survives sanitizing (`'..'` *reproduced*) and would write sheets one level up. Also, `merge_reviews` accepts ratings from **any** folder under `by_reviewer/`, not just the batch's assigned reviewers. Reject `.`/`..` and filter to assigned reviewers (warn on others). |
| L4 | `sap.py::merge_reviews` | `int(float("inf"))` raises `OverflowError`, which is not caught (only `ValueError`); one bad cell aborts the merge. |
| L5 | `sap.py::record_decisions` | Re-recording a decision without `awarded_amount` overwrites a stored amount with NULL, and silently replaces an earlier decision. Warn when the application is already `decided`, and keep the existing amount when the column is blank. |
| L6 | `main.py::apply_testing_mode` | `--testing` rebuilds the database but leaves `testing/output/sap_batches/by_reviewer/`, so reviewer sheets from older runs trigger "no longer matches batch N" warnings forever (seen on the first run during this review). The README says the folder is rebuilt every run. Clear `sap_batches/` in testing mode. |
| L7 | `utils.py::load_and_merge_config` | The CLI value only wins when it differs from the **default**, so `--seed 2026` cannot override a different value in `config.json`; and `config.json` is rewritten on every run, so old flags (for example a previous `--sap_file`) are silently reused. Documented in [Command-Line Reference](Command-Line-Reference#configuration-and-flag-precedence); consider `default=None` sentinels or an explicit "write config" flag. |
| L8 | `main.py`, `data_structures.py`, `hashing.py`, `database.py`, `parsing_utils.py` | **Dead code (done):** `run_verification_of_csv` in `main.py` (hard-coded `quit(0)`, a `Student ID` header the rest of the code never uses; it was being removed in the working tree while this review was written, so also drop the imports it leaves unused), a trailing `pass`, `data_structures.py`, `hashing.py`, `check_for_student` (queries a `students` table that does not exist), `export_db_to_csv`, `return_unique_values`, `return_all_values`, `get_uoft_trunc_format`, `strip_uoft_trunc_placeholders`. `bulk_check_for_students` is referenced only by a test. All of these, and the unused imports, have been removed. |
| L9 | Docs | `README.md` says SABP/AGM commands run "From `src/`" while Setup says any directory; `utsu_mail/__main__.py`'s docstring shows `--list-fields`/`--dry-run` (real flags use underscores); `agm.py::write_csv` docstring mentions "Not stated" (blank answers are `Virtual`); the `--verify_student` flag is the **Senate** export but is described generically; the shipped `config.json` still contains retired keys (`parse_students`, `log_sample`, `dry_run`). |
| L10 | Privacy | `student_aid.student_number` stores the number **as typed** (possibly the full 10 digits) while the roster only keeps 4; `senate_application` and `agm_rsvp` store none. Consider storing only the last 4. The log contains application ids but no names or emails (good). |
| L11 | Packaging | `data/`, `templates/`, `src/schema/` are located through the project root rather than packaged as package data, so only an editable install works (documented in [Getting Started](Getting-Started#requirements)). `uv.lock` is gitignored, so there is no lock for the dev dependency. |
| L12 | `importing_data.py` | `open("src/schema/uoft_data.sql")` is not closed and has no encoding; the roster uuid is built from concatenated values with no separator (`"Ann"+"Lee"` vs `"AnnL"+"ee"`; negligible in practice because of the other fields). Other modules read schema with `Path.read_text(encoding=...)`. |

## Things done well

- **Idempotence by design:** content-derived UUIDv5 ids, fingerprints and the locked/unlocked status model make re-runs
  safe and explainable.
- **Careful input handling:** encoding detection, ASCII folding, header cleaning, tolerant lookup tables.
- **Security hygiene:** spreadsheet-formula neutralization everywhere, read-only SQL for mail queries
  (`mode=ro` + SELECT check), header-injection protection (subjects collapsed, addresses with control characters
  rejected), SMTP passwords never read from config, credentials refused over plain text to remote hosts, confirmation
  word before sending, resume log.
- **Human-in-the-loop controls** at the decisions that matter (held applications, closed batches, never overwriting
  reviewer sheets).
- **Seating** is deterministic and the strict-priority cost construction is sound (each tier outweighs all lower tiers
  combined; ties are broken by a seeded integer).
- Good docstrings with doctests; the tests read like specifications.

## Test gaps

The suite is strong around identity, incremental runs and SABP, but these paths have no coverage:

- `utsu_core.main`: config merge/precedence, testing mode, action-flag ordering, exit codes (would have caught M2, M5, L6).
- Roster import failure paths (H1) and `--reimport_uoft_data`.
- `seating.py`: add property tests (each applicant at most once, no constituency over capacity, nobody seated where
  they marked none, same seed gives the same result, delegates take division seats).
- SABP and AGM exports without a student-number column in the same test (H2).
- WFNI with unlisted answers (M1).
- `SmtpSender` (connect/reconnect/login rules), perhaps with a fake `smtplib`.
- `merge_reviews` with malformed cells (`inf`, `1e1`, blanks), and reviewers who are not assigned.

## Suggested order of work

1. **H1, H2** (data safety and a crash): small, contained changes with clear tests.
2. **M1, M2, M5** (quick, high value-to-effort).
3. **M3, M4** after a policy decision (what counts as "a term"; what to do on an ambiguous match).
4. The Low items as housekeeping; remove the dead code first because it reduces the surface to maintain.
