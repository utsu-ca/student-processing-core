"""Helpers for re-running a program on an updated form export (the full export, with the new responses added)."""
import logging
import sqlite3

logger = logging.getLogger(__name__)

CHUNK = 500
# What is saved with each application about how it was verified (see verification.Match).
OUTCOME_COLUMNS = ("uoft_uuid", "verification_level", "match_method")
OUTCOME_SQL_TYPES = {"uoft_uuid": "TEXT", "verification_level": "INTEGER", "match_method": "TEXT"}


def add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add columns that a database created by an older version of the program does not have yet."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    conn.commit()


def known_rows(conn: sqlite3.Connection, table: str, extra: tuple[str, ...] = ()) -> dict[str, dict]:
    """{uuid: {status, *OUTCOME_COLUMNS, *extra}} for every application already recorded in `table`."""
    columns = ("uuid", "status", *OUTCOME_COLUMNS, *extra)
    return {row[0]: dict(zip(columns[1:], row[1:]))
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}")}


def apply_known(current: list[dict], known: dict[str, dict], reverify: bool = False) -> list[dict]:
    """
    Copy the stored outcome (status, uoft_uuid, verification_level, ...) onto the applications that were
    already verified and return the ones that still need checking: new applications, and applications that were
    unverified last time (they are cheap to repeat, and pick up a corrected row or new student data).
    `reverify` treats every application as new (use it after the student data has been re-imported).
    """
    to_check = []
    for app in current:
        stored = known.get(app["uuid"])
        if reverify or not stored or stored["status"] != "verified":
            to_check.append(app)
        else:
            app.update(stored)
    return to_check


def count_new(current: list[dict], known: dict[str, dict]) -> int:
    """How many applications have not been seen by an earlier run."""
    return sum(app["uuid"] not in known for app in current)


def mark_superseded(conn: sqlite3.Connection, table: str, uuids) -> None:
    """Flag older submissions that a newer one from the same student has replaced."""
    uuids = sorted(uuids)
    for start in range(0, len(uuids), CHUNK):
        chunk = uuids[start:start + CHUNK]
        conn.execute(f"UPDATE {table} SET status = 'superseded' WHERE status != 'superseded' "
                     f"AND uuid IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
