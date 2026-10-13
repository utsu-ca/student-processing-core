"""
Internal integrity checks. These do not decide anything about an application; they look for states that should not
happen, hold the application for a person, and report it as an alert. Log lines start with "INTEGRITY".

- a verified (or superseded) application is immutable, but its form row has changed since it was processed. The
  application is *invalid*: it keeps its status and stage (it stays in progress) and is noted as invalid
  (`invalid_reason`, flag `invalid`), but it is held: it is left out of anything further (review batches, the
  reconciliation, decisions, the Senate list that goes to seating, the AGM lists) until a
  person deals with it. If the row is put back as it was, the hold is released by itself; if the edit is fine,
  `--resolve_invalid <id>` accepts the edited row as the new baseline;
- more than one verified application for the same student (and, for SABP, the same term).

Every run writes `alerts_<program>.csv` for the people who have to look at these (and at the non-binding and
not-considered applications).
"""
import logging
import sqlite3
from pathlib import Path

from utsu_core.incremental import CHANGED_FLAG, LOCKED, _changed, add_flag
from utsu_std.csv_safe import write_rows

logger = logging.getLogger("utsu.integrity")

INVALID_CHANGED = "row changed after processing"
INVALID_FLAG = "invalid"
TABLES = ("senate_application", "student_aid", "agm_rsvp")
ALERT_FIELDS = ["severity", "kind", "program", "uuid", "student_uuid", "detail"]


def changed_after_processing(rows: list[dict], known: dict[str, dict], program: str) -> list[str]:
    """
    Locked rows that are different in this export (edited in the form's spreadsheet). Each is marked invalid
    (`invalid_reason`, flags), is not processed again, and is logged as an error. Returns their uuids.
    """
    changed = []
    for app in rows:
        stored = known.get(app["uuid"])
        if stored and stored["status"] in LOCKED and _changed(app, stored):
            changed.append(app["uuid"])
            app["invalid_reason"] = INVALID_CHANGED
            add_flag(app, CHANGED_FLAG)
            logger.error(f"INTEGRITY: {program} application {app['uuid']} is {stored['status']} and can no longer be "
                         f"changed, but its form row has changed since. It is invalid: it stays in progress but is "
                         f"held, and was NOT processed again. A person needs to investigate.")
    return changed


def restored_after_processing(rows: list[dict], known: dict[str, dict]) -> list[str]:
    """Held rows whose form row is back as it was processed: the hold is released. Returns their uuids."""
    released = []
    for app in rows:
        stored = known.get(app["uuid"])
        if (stored and stored.get("invalid_reason") == INVALID_CHANGED and stored["status"] in LOCKED
                and not _changed(app, stored)):
            app["invalid_reason"] = None
            released.append(app["uuid"])
            logger.info(f"INTEGRITY: application {app['uuid']} is back as it was processed; released from hold")
    return released


def hold(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split rows into (usable, held): held rows are invalid and are noted with the flag `invalid`."""
    usable, held = [], []
    for app in rows:
        if app.get("invalid_reason"):
            add_flag(app, INVALID_FLAG)
            held.append(app)
        else:
            usable.append(app)
    return usable, held


def record_invalid(conn: sqlite3.Connection, table: str, changed: list[str], released: list[str]) -> None:
    """Save which applications are held and which were released."""
    conn.executemany(f"UPDATE {table} SET invalid_reason = ? WHERE uuid = ? AND invalid_reason IS NULL",
                     [(INVALID_CHANGED, uuid) for uuid in changed])
    conn.executemany(f"UPDATE {table} SET invalid_reason = NULL WHERE uuid = ? AND invalid_reason = ?",
                     [(uuid, INVALID_CHANGED) for uuid in released])
    conn.commit()


def resolve_invalid(conn: sqlite3.Connection, uuids: list[str]) -> list[str]:
    """
    A person has looked at held applications and accepts them: the hold is lifted and the next run takes the
    application's current form row as its baseline. Returns the uuids that were held.
    """
    have = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    resolved = []
    for uuid in uuids:
        for table in (t for t in TABLES if t in have):
            if conn.execute(f"UPDATE {table} SET invalid_reason = NULL, source_hash = NULL "
                            f"WHERE uuid = ? AND invalid_reason IS NOT NULL", (uuid,)).rowcount:
                resolved.append(uuid)
                logger.info(f"INTEGRITY: {uuid} accepted by a person; hold released")
    conn.commit()
    return resolved


def duplicate_verified(conn: sqlite3.Connection, table: str, program: str,
                       group_columns: tuple[str, ...] = ("student_uuid",)) -> list[tuple]:
    """Students with more than one verified application (per term for SABP): there should be exactly one."""
    columns = ", ".join(group_columns)
    found = conn.execute(f"SELECT {columns}, COUNT(*) FROM {table} WHERE status = 'verified' "
                         f"AND {group_columns[0]} IS NOT NULL GROUP BY {columns} HAVING COUNT(*) > 1").fetchall()
    for *key, count in found:
        logger.error(f"INTEGRITY: {program} has {count} verified applications for one student ({', '.join(map(str, key))}); "
                     f"there should be one. A person needs to investigate.")
    return found


def build_alerts(conn: sqlite3.Connection, table: str, program: str, rows: list[dict], statuses: dict[str, str],
                 duplicates: list[tuple], ambiguous: list[dict] = ()) -> list[dict]:
    """
    What a person has to look at: held (invalid) applications, which stay listed until resolved; more than one
    verified application per student; ambiguous matches (more than one student fits an application, so it
    cannot be verified; listed until it is resolved); and, from this run, unverified submissions that did not
    replace a verified one and applications not considered because the student already has a completed one.
    """
    alerts = [{"severity": "ERROR", "kind": "invalid", "program": program, "uuid": uuid, "student_uuid": student,
               "detail": f"{reason}; held (in progress, not processed further) until resolved"}
              for uuid, student, reason in conn.execute(
                  f"SELECT uuid, student_uuid, invalid_reason FROM {table} "
                  f"WHERE status = 'verified' AND invalid_reason IS NOT NULL ORDER BY uuid")]
    for *key, count in duplicates:
        alerts.append({"severity": "ERROR", "kind": "duplicate_verified", "program": program, "uuid": "",
                       "student_uuid": key[0], "detail": f"{count} verified applications ({', '.join(map(str, key))})"})
    for row in ambiguous:
        alerts.append({"severity": "WARNING", "kind": "ambiguous_match", "program": program, "uuid": row["uuid"],
                       "student_uuid": row.get("student_uuid", ""),
                       "detail": f"{row.get('reason', '')}: more than one student matches, so it cannot be verified; "
                                 f"a person has to decide which student this is (or the row needs a student number "
                                 f"or division)"})
    by_uuid = {row["uuid"]: row for row in rows}
    details = {"non_binding": "unverified submission; does not replace the student's verified application "
                              "(check it is not someone else using their details)",
               "not_considered": "not considered: the student already has a completed application for this term"}
    for uuid, status in sorted(statuses.items()):
        if status in details:
            alerts.append({"severity": "WARNING", "kind": status, "program": program, "uuid": uuid,
                           "student_uuid": by_uuid.get(uuid, {}).get("student_uuid", ""), "detail": details[status]})
    return alerts


def write_alerts(output_dir: Path, name: str, alerts: list[dict]) -> Path:
    """Write `alerts_<name>.csv` (header only when there is nothing to look at)."""
    path = Path(output_dir) / f"alerts_{name}.csv"
    write_rows(path, ALERT_FIELDS, alerts, extrasaction="raise")
    if alerts:
        logger.warning(f"{len(alerts)} alert(s) for a person to look at in {path}")
    return path
