PRAGMA foreign_keys = ON;

-- One row per AGM voter RSVP, rebuilt from the full form export on every run. Names and contact details are not
-- stored here.
--   uuid                typed id `application:agm:<uuid>` (submission timestamp + the student's identity key)
--   student_uuid        typed id of the student, `student:email:...` (else student number, else name)
--   uoft_uuid           the matching uoft_data.uuid once the student is verified, else NULL
--   verification_level  the number of factors (name, student number, division) that matched: 1, 2 or 3
--   status              verified | unverified; later superseded or non_binding (see senate_application)
CREATE TABLE IF NOT EXISTS agm_rsvp (
    uuid TEXT PRIMARY KEY,
    student_uuid TEXT,
    uoft_uuid TEXT,
    verification_level INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    match_method TEXT,
    submission_ts TEXT,
    meeting_preference TEXT,
    contact_ok TEXT,
    flags TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
