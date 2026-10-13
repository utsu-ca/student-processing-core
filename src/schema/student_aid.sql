PRAGMA foreign_keys = ON;

-- One row per Student Aid Bursary Program application. `uuid` is the typed id `application:sap:<uuid>`, derived from
-- the submission timestamp and the student's identity key (email; else student number; else name), so reprocessing
-- the same form updates the same row. `student_uuid` is the typed per-student id; `uoft_uuid` is the matching
-- uoft_data.uuid (NULL when unverified); `verification_level` is the number of factors (name, student number,
-- division) that matched: 1, 2 or 3 (the SABP form has no division, so at most 2). `status` is verified or
-- unverified; later superseded or non_binding (see
-- senate_application). `source_hash` fingerprints the form row as it was processed. `invalid_reason` is set when a
-- verified application's row was edited after it was processed: it stays at its stage but is held (not batched,
-- reconciled or decided) until a person deals with it; NULL means valid.
-- The decision columns are filled in later by the committee and are never overwritten by reprocessing.
-- `stage` tracks a verified application through the process and is never moved backwards by reprocessing:
--   scored      verified and scored, in a batch that has not been handed to reviewers
--   in_review   its batch was handed to reviewers (assigned, or marked with --issue_batches)
--   discuss     the reviewers' levels differ by 2 or more
--   reconciled  the reviewers agree
--   decided     the final decision is recorded
-- It is NULL for applications that are unverified or were never scored.
CREATE TABLE IF NOT EXISTS student_aid (
    uuid TEXT PRIMARY KEY,
    student_uuid TEXT,
    uoft_uuid TEXT,
    verification_level INTEGER,
    match_method TEXT,
    student_number TEXT NOT NULL,
    submission_ts TEXT,
    terms TEXT,
    status TEXT NOT NULL,
    stage TEXT,
    aid_type TEXT NOT NULL,
    requested_amount REAL,
    wfni INTEGER,
    emergency INTEGER NOT NULL DEFAULT 0,
    flags TEXT,
    batch INTEGER,
    record INTEGER,
    awarded_amount REAL,
    decision TEXT,
    source_hash TEXT,
    invalid_reason TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The two committee members each batch is assigned to, and the need level (0-5, General Statement Rubric) each
-- of them gave an application.
CREATE TABLE IF NOT EXISTS sap_batch_reviewer (
    batch INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    PRIMARY KEY (batch, reviewer)
);

CREATE TABLE IF NOT EXISTS sap_review (
    uuid TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    need_level INTEGER NOT NULL,
    notes TEXT,
    PRIMARY KEY (uuid, reviewer)
);

