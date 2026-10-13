PRAGMA foreign_keys = ON;

-- One row per Senate application. `uuid` is the typed id `application:senate:<uuid>`, derived from the submission
-- timestamp and the student's identity key, so a re-exported form is recognised: an application that was processed
-- is never processed again (a row edited afterwards is logged as an error instead). `student_uuid` is the typed
-- per-student id (`student:email:...`, from their email; else student number; else name). `uoft_uuid` is the
-- matching uoft_data.uuid (NULL when unverified). `verification_level` is 1 for a name or student number alone, 2
-- for both, 3 once also checked in person. `source_hash` fingerprints the form row as it was processed.
-- `status` is verified or unverified; later superseded (a newer submission from the same student replaced it) or
-- non_binding (an unverified submission that did not replace a verified one) or not_considered. `invalid_reason` is
-- set when a verified application's form row was edited after it was processed (it stays in progress, held for a
-- person: see integrity.py); NULL means valid.
CREATE TABLE IF NOT EXISTS senate_application (
    uuid TEXT PRIMARY KEY,
    student_uuid TEXT,
    uoft_uuid TEXT,
    verification_level INTEGER,
    match_method TEXT,
    status TEXT NOT NULL,
    submission_ts TEXT,
    delegated TEXT,
    source_hash TEXT,
    invalid_reason TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
