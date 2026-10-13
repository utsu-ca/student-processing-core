"""Helpers for re-running a program on an updated form export (the full export, with the new responses added)."""
import logging
import sqlite3

logger = logging.getLogger(__name__)

DECIDED = ("verified", "unverified")
CHUNK = 500


def known_rows(conn: sqlite3.Connection, table: str, extra: tuple[str, ...] = ()) -> dict[str, dict]:
    """{uuid: {status, student_uuid, *extra}} for every application already recorded in `table`."""
    columns = ("uuid", "status", "student_uuid", *extra)
    return {row[0]: dict(zip(columns[1:], row[1:]))
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}")}


def apply_known(current: list[dict], known: dict[str, dict], reverify: bool = False) -> list[dict]:
    """
    Copy the stored outcome (status, student_uuid, ...) onto the applications that were already processed and
    return only the new ones, which still need verifying. `reverify` treats every application as new (use it
    after the student data has been re-imported).
    """
    new = []
    for app in current:
        stored = known.get(app["uuid"])
        if reverify or not stored or stored["status"] not in DECIDED:
            new.append(app)
        else:
            app.update(stored)
    return new


def mark_superseded(conn: sqlite3.Connection, table: str, uuids) -> None:
    """Flag older submissions that a newer one from the same student has replaced."""
    uuids = sorted(uuids)
    for start in range(0, len(uuids), CHUNK):
        chunk = uuids[start:start + CHUNK]
        conn.execute(f"UPDATE {table} SET status = 'superseded' WHERE status != 'superseded' "
                     f"AND uuid IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
