PRAGMA foreign_keys = ON;

-- One row per AGM voter RSVP. Names and contact details are not stored here.
--   uuid                internal id of this RSVP (program + submission timestamp + the student's identity key)
--   student_uuid        internal id of the student (from their email; else student number; else name)
--   uoft_uuid           the matching uoft_data.uuid once the student is verified, else NULL
--   verification_level  1 name or student number alone, 2 name + student number, 3 + checked in person
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
