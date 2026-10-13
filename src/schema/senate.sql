PRAGMA foreign_keys = ON;

-- One row per Senate application, rebuilt from the full form export on every run. `uuid` is the typed id
-- `application:senate:<uuid>`, derived from the submission timestamp and the student's identity key (Google Forms
-- stamps an edited or re-submitted response with a new timestamp, so that is a newer application). `student_uuid` is
-- the typed per-student id (`student:email:...`, from their email; else student number; else name). `uoft_uuid` is
-- the matching uoft_data.uuid (NULL when unverified). `verification_level` is the number of factors (name, student
-- number, division) that matched the same student: 1 a name or student number alone, 2 any two, 3 all three.
-- `status` is verified or unverified; later superseded (a newer submission from the same student replaced it) or
-- non_binding (an unverified submission that did not replace a verified one) or not_considered.
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
