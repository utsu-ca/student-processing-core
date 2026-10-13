"""
Internal integrity checks. These do not decide anything about an application; they look for states that should not
happen and report them as alerts. Log lines start with "INTEGRITY".

- more than one verified application for the same student (and, for SABP, the same term);
- a verified application that no longer matches the student data (it is kept verified: see pipeline.Run);
- for SABP, a verified application that is not in the export any more (the student edited or re-submitted it) and was
  not replaced by a newer one.

Every run writes `alerts_<program>.csv` for the people who have to look at these (and at the non-binding and
not-considered applications).
"""
import logging
import sqlite3
from pathlib import Path

from utsu_std.csv_safe import write_rows

logger = logging.getLogger("utsu.integrity")

ALERT_FIELDS = ["severity", "kind", "program", "uuid", "student_uuid", "detail"]


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


def build_alerts(program: str, rows: list[dict], statuses: dict[str, str], duplicates: list[tuple],
                 ambiguous: list[dict] = (), extra: list[dict] = ()) -> list[dict]:
    """
    What a person has to look at: more than one verified application per student; ambiguous matches (more than one
    student fits an application, so it cannot be verified); `extra` alerts a program raises itself; and, from this
    run, unverified submissions that did not replace a verified one and applications not considered because the
    student already has a completed one.
    """
    alerts = []
    for *key, count in duplicates:
        alerts.append({"severity": "ERROR", "kind": "duplicate_verified", "program": program, "uuid": "",
                       "student_uuid": key[0], "detail": f"{count} verified applications ({', '.join(map(str, key))})"})
    for row in ambiguous:
        alerts.append({"severity": "WARNING", "kind": "ambiguous_match", "program": program, "uuid": row["uuid"],
                       "student_uuid": row.get("student_uuid", ""),
                       "detail": f"{row.get('reason', '')}: more than one student matches, so it cannot be verified; "
                                 f"a person has to decide which student this is (or the row needs a student number "
                                 f"or division)"})
    alerts.extend(extra)
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
