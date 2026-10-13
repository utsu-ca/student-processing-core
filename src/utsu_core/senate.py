import csv
import logging
import re
import sqlite3
from pathlib import Path

from utsu_core.selection import add_flag, dedupe_by_uuid
from utsu_core.pipeline import Run, ensure_table
from utsu_core.verification import match_students, setup_reader
from utsu_std.csv_safe import write_rows
from utsu_std.utils import absfile, application_uuid_for, student_uuid_for

logger = logging.getLogger(__name__)

OUTPUT_FIELDS = ["uuid", "submission_ts", "full_name", "id", "email", "personal_email", "division", "delegated"]
# what verified.csv / unverified.csv carry: the form's fields plus the internal ids and how it was verified
RESULT_FIELDS = OUTPUT_FIELDS + ["student_uuid", "uoft_uuid", "verification_level", "match_method", "flags"]
OPTIONAL_FIELDS = ("id", "full_name")
IDENTITY_FIELDS = ("full_name",)  # the form needs a name column; the student number is optional and only narrows a match
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


def application_uuid(row: dict[str, str]) -> str:
    """Internal id for an application, so re-running on the same form yields the same uuid."""
    return application_uuid_for("senate", row.get("submission_ts", ""), row.get("email", ""), row.get("id", ""),
                                row.get("full_name", ""))


def write_results(verified: list[dict[str, str]], unverified: list[dict[str, str]], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    pref_fields = list(dict.fromkeys(
        k for row in (*verified, *unverified) for k in row if k.startswith(PREF_PREFIX)))
    for name, rows in (("verified.csv", verified), ("unverified.csv", unverified)):
        write_rows(output_dir / name, RESULT_FIELDS + pref_fields, rows)


_INSERT = """
INSERT INTO senate_application (uuid, student_uuid, uoft_uuid, verification_level, match_method, status,
                                submission_ts, delegated)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :match_method, :status, :submission_ts, :delegated)
"""


def ensure_schema(conn: sqlite3.Connection):
    ensure_table(conn, "src/schema/senate.sql")


def verify_senators(csv_file: Path, cursor: sqlite3.Cursor, output_dir: Path | str = "secrets/output"):
    """
    Verify senate applications against uoft_data, by name, student number or both (whatever the application
    gives). Give the full export every time: every application in it is verified, so a correction (an edit, which
    Google Forms stamps with a new timestamp) is just a newer submission. Per student, the newest verified
    application is the binding one; a newer unverified one never replaces it (it becomes non-binding), and an
    application that was verified before is never downgraded if the student data changes. The senate_application
    table is rebuilt from the export.
    Writes verified.csv and unverified.csv (the whole export's result) to output_dir (relative paths resolve
    from the project root), and alerts_senate.csv.
    """
    if isinstance(csv_file, str):
        csv_file = Path(csv_file)
    mapping = absfile("data/conversion_table_senate.csv")
    senators: csv.DictReader = setup_reader(csv_file, mapping, optional=OPTIONAL_FIELDS, identity=IDENTITY_FIELDS)

    rows = []
    for row in senators:
        app = {k: (row.get(k) or "").strip() for k in OUTPUT_FIELDS if k != "uuid"}
        app["delegated"] = "yes" if app["delegated"].lower().startswith("yes") else "no"
        app.update(read_preferences(row))
        if not app["id"] and not app["full_name"]:
            continue
        app["uuid"] = application_uuid(app)
        app["student_uuid"] = student_uuid_for(app["email"], app["id"], app["full_name"])
        app["flags"] = ""
        rows.append(app)
    applications = dedupe_by_uuid(rows)

    conn = cursor.connection
    ensure_schema(conn)
    run = Run(conn, "senate_application", "Senate", "senate", applications)

    for app, match in zip(applications, match_students(applications, cursor)):
        app.update(uoft_uuid=match.uoft_uuid, verification_level=match.level, match_method=match.method,
                   status="verified" if match.verified else "unverified")
        if match.fuzzy_flag:
            add_flag(app, match.fuzzy_flag)
    run.keep_verified()
    for app in applications:
        logger.log(logging.INFO if app["status"] == "verified" else logging.WARNING,
                   f"{app['status'].upper()} application {app['uuid']}")

    run.select()
    run.replace_all(_INSERT, "src/schema/senate.sql")
    run.settle()

    verified = [a for a in run.current if a["status"] == "verified"]
    unverified = [a for a in run.current if a["status"] == "unverified"]
    out = Path(absfile(output_dir))
    write_results(verified, unverified, out)
    alerts = run.alert(out)
    return {"verified": verified, "unverified": unverified, "duplicates": run.duplicates, "alerts": alerts,
            "kept": run.kept, "non_binding": [u for u, s in run.statuses.items() if s == "non_binding"]}




