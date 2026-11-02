PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS uoft_data;

CREATE TABLE IF NOT EXISTS uoft_data (
    uuid TEXT PRIMARY KEY,
    trunc_id INTEGER,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    division TEXT NOT NULL,
    faculty TEXT NOT NULL,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

