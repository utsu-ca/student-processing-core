PRAGMA foreign_keys = ON;

-- One row per Senate application (the newest per student is the one processed). `uuid` is derived from the
-- submission timestamp and the student's identity key, so a re-exported form is recognised: rows already verified
-- are not checked again. `student_uuid` is the internal per-student id (from their email; else student number; else
-- name). `uoft_uuid` is the matching uoft_data.uuid (NULL when unverified). `verification_level` is 1 for a name or
-- student number alone, 2 for both, 3 once also checked in person. Older submissions replaced by a newer one are
-- marked 'superseded'.
CREATE TABLE IF NOT EXISTS senate_application (
    uuid TEXT PRIMARY KEY,
    student_uuid TEXT,
    uoft_uuid TEXT,
    verification_level INTEGER,
    match_method TEXT,
    status TEXT NOT NULL,
    submission_ts TEXT,
    delegated TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
