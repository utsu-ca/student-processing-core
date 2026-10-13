PRAGMA foreign_keys = ON;

-- One row per Student Aid Bursary Program application. `uuid` is derived from the submission, so reprocessing the
-- same form updates the same row. `student_uuid` is the matching uoft_data.uuid (NULL when unverified). The
-- decision columns are filled in later by the committee and are never overwritten by reprocessing.
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

