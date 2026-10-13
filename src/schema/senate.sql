PRAGMA foreign_keys = ON;

-- One row per Senate application (the newest per student is the one processed). `uuid` is derived from the
-- submission, so a re-exported form is recognised: rows already here are not verified again. `student_uuid` is the
-- matching uoft_data.uuid (NULL when unverified). Older submissions replaced by a newer one are marked 'superseded'.
CREATE TABLE IF NOT EXISTS senate_application (
    uuid TEXT PRIMARY KEY,
    student_uuid TEXT,
    status TEXT NOT NULL,
    submission_ts TEXT,
    delegated TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
