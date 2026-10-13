import csv
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from utsu_core.incremental import (OUTCOME_SQL_TYPES, TRACKING_SQL_TYPES, add_missing_columns, apply_known,
                                   count_new, dedupe_by_uuid, fingerprint, known_rows, select_current, set_statuses)
from utsu_core.integrity import (build_alerts, changed_after_processing, duplicate_verified, hold, record_invalid,
                                 restored_after_processing, write_alerts)
from utsu_core.verification import mapped_names, match_students, setup_reader
from utsu_std.csv_safe import SafeDictWriter
from utsu_std.utils import absfile, application_uuid_for, identity_key, student_uuid_for

logger = logging.getLogger(__name__)

OUTPUT_FIELDS = ["uuid", "submission_ts", "full_name", "id", "email", "personal_email", "division", "delegated"]
# what verified.csv / unverified.csv carry: the form's fields plus the internal ids and how it was verified
RESULT_FIELDS = OUTPUT_FIELDS + ["student_uuid", "uoft_uuid", "verification_level", "match_method", "flags",
                                 "invalid_reason"]
IDENTITY_FIELDS = ("id", "full_name")  # the form needs at least one; each application may give either or both
PREF_PREFIX = "pref:"
_PREF_COLUMN = re.compile(r"^\s*Constituency Choice \[(.+)\]\s*$")





def read_preferences(row: dict[str, str]) -> dict[str, str]:
    """
    Pull the 'Constituency Choice [...]' answers out of a mapped form row. They are not in the lookup table,
    so they keep their original headers.

    >>> read_preferences({"id": "1", "Constituency Choice [Commuter]": " First-choice "})
    {'pref:Commuter': 'First-choice'}
    """
    prefs = {}
    for col, value in row.items():
        if isinstance(col, str) and (m := _PREF_COLUMN.match(col)):
            prefs[PREF_PREFIX + m.group(1)] = (value or "").strip()
    return prefs


def parse_submission_ts(value: str) -> datetime | None:
    """
    Parse a form timestamp such as '2026/08/14 1:30:47 p.m. AST'. The timezone label is ignored.

    >>> parse_submission_ts("2026/08/14 1:30:47 p.m. AST")
    datetime.datetime(2026, 8, 14, 13, 30, 47)
    >>> parse_submission_ts("2026/08/14 12:05:00 a.m. AST")
    datetime.datetime(2026, 8, 14, 0, 5)
    >>> parse_submission_ts("garbage") is None
    True
    """
    m = re.match(r"\s*(\d{4}/\d{2}/\d{2})\s+(\d{1,2}:\d{2}:\d{2})\s*([ap])\.?m\.?", value or "", re.I)
    if not m:
        return None
    date, time, meridiem = m.groups()
    return datetime.strptime(f"{date} {time} {meridiem.upper()}M", "%Y/%m/%d %I:%M:%S %p")


def application_uuid(row: dict[str, str]) -> str:
    """Internal id for an application, so re-running on the same form yields the same uuid."""
    return application_uuid_for("senate", row.get("submission_ts", ""), row.get("email", ""), row.get("id", ""),
                                row.get("full_name", ""))


def student_key(row: dict[str, str]) -> str:
    """Whose application a row is: their email, else student number, else name (see utsu_std.utils.identity_key)."""
    return identity_key(row.get("email", ""), row.get("id", ""), row.get("full_name", ""))


def submission_rank(row: dict) -> datetime:
    """When a row was submitted, for deciding which of a student's submissions is the newest."""
    return parse_submission_ts(row.get("submission_ts", "")) or datetime.min


def write_results(verified: list[dict[str, str]], unverified: list[dict[str, str]], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    pref_fields = list(dict.fromkeys(
        k for row in (*verified, *unverified) for k in row if k.startswith(PREF_PREFIX)))
    for name, rows in (("verified.csv", verified), ("unverified.csv", unverified)):
        with open(output_dir / name, "w", newline="", encoding="utf-8") as f:
            writer = SafeDictWriter(f, fieldnames=RESULT_FIELDS + pref_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Wrote {len(rows)} rows to {output_dir / name}")


_UPSERT = """
INSERT INTO senate_application (uuid, student_uuid, uoft_uuid, verification_level, match_method, status,
                                submission_ts, delegated, source_hash)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :match_method, :status, :submission_ts, :delegated,
        :source_hash)
ON CONFLICT(uuid) DO UPDATE SET student_uuid = excluded.student_uuid, uoft_uuid = excluded.uoft_uuid,
    match_method = excluded.match_method, status = excluded.status,
    source_hash = COALESCE(excluded.source_hash, senate_application.source_hash),
    verification_level = CASE WHEN excluded.status = 'verified' AND senate_application.verification_level = 3
                              THEN 3 ELSE excluded.verification_level END
"""


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(Path(absfile("src/schema/senate.sql")).read_text(encoding="utf-8-sig"))
    add_missing_columns(conn, "senate_application", {**TRACKING_SQL_TYPES, **OUTCOME_SQL_TYPES})


def verify_senators(csv_file: Path, cursor: sqlite3.Cursor, output_dir: Path | str = "secrets/output",
                    reverify: bool = False):
    """
    Verify senate applications against uoft_data, by name, student number or both (whatever the application
    gives). Applications are looked at once: a new one is verified. An unverified one whose row was edited is
    processed again (that is how a correction is made); a verified one is immutable, and an edit to its row makes
    it invalid: it stays verified but is held out of verified.csv (and so out of the seating) and raised in
    alerts_senate.csv until a person deals with it (integrity.py). With `reverify` (after new student data)
    everything is verified again. Per student, the newest verified application is the binding one; a newer
    unverified one never replaces it (it becomes non-binding).
    Writes verified.csv and unverified.csv (the whole export's result) to output_dir (relative paths resolve
    from the project root), and alerts_senate.csv.
    """
    if isinstance(csv_file, str):
        csv_file = Path(csv_file)
    mapping = absfile("data/conversion_table_senate.csv")
    senators: csv.DictReader = setup_reader(csv_file, mapping, optional=IDENTITY_FIELDS, identity=IDENTITY_FIELDS)
    fields = mapped_names(mapping)

    rows = []
    for row in senators:
        app = {k: (row.get(k) or "").strip() for k in OUTPUT_FIELDS if k != "uuid"}
        app["delegated"] = "yes" if app["delegated"].lower().startswith("yes") else "no"
        app.update(read_preferences(row))
        if not app["id"] and not app["full_name"]:
            continue
        app["uuid"] = application_uuid(app)
        app["student_uuid"] = student_uuid_for(app["email"], app["id"], app["full_name"])
        app["source_hash"] = fingerprint(row, fields)
        app["flags"] = ""
        rows.append(app)
    applications = dedupe_by_uuid(rows)

    conn = cursor.connection
    ensure_schema(conn)
    known = known_rows(conn, "senate_application")
    to_check = apply_known(applications, known, reverify)
    changed = changed_after_processing(applications, known, "Senate")
    released = restored_after_processing(applications, known)

    for app, match in zip(to_check, match_students(to_check, cursor)):
        app.update(uoft_uuid=match.uoft_uuid, verification_level=match.level, match_method=match.method,
                   status="verified" if match.verified else "unverified")
        logger.log(logging.INFO if match.verified else logging.WARNING,
                   f"{app['status'].upper()} application {app['uuid']}")

    current, statuses = select_current(applications, student_key, submission_rank)
    logger.info(f"{len(rows)} applications read; {count_new(applications, known)} new, {len(to_check)} verified now, "
                f"{len(changed)} changed since processed; {len(current)} binding after "
                f"{sum(s == 'superseded' for s in statuses.values())} superseded and "
                f"{sum(s == 'non_binding' for s in statuses.values())} non-binding")

    saved = [a for a in dedupe_by_uuid(to_check + current) if a["uuid"] not in changed]
    conn.executemany(_UPSERT, saved)
    set_statuses(conn, "senate_application", statuses)
    record_invalid(conn, "senate_application", changed, released)
    duplicates = duplicate_verified(conn, "senate_application", "Senate")

    # an invalid (held) application stays verified but is left out of what goes on to seating
    verified, held = hold([a for a in current if a["status"] == "verified"])
    unverified = [a for a in current if a["status"] == "unverified"]
    out = Path(absfile(output_dir))
    write_results(verified, unverified, out)
    alerts = build_alerts(conn, "senate_application", "Senate", applications, statuses, duplicates)
    write_alerts(out, "senate", alerts)
    return {"verified": verified, "unverified": unverified, "new": count_new(applications, known),
            "changed": changed, "held": held, "duplicates": duplicates, "alerts": alerts,
            "non_binding": [u for u, s in statuses.items() if s == "non_binding"]}




