PRAGMA foreign_keys = ON;

-- One row per AGM voter RSVP. Names and contact details are not stored here.
--   uuid                typed id `application:agm:<uuid>` (submission timestamp + the student's identity key)
--   student_uuid        typed id of the student, `student:email:...` (else student number, else name)
--   uoft_uuid           the matching uoft_data.uuid once the student is verified, else NULL
--   verification_level  1 name or student number alone, 2 name + student number, 3 + checked in person
--   status              verified | unverified; later superseded or non_binding (see senate_application)
--   source_hash         fingerprint of the form row as it was processed
--   invalid_reason      set when a verified RSVP's row was edited after it was processed: held for a person
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
    source_hash TEXT,
    invalid_reason TEXT,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
