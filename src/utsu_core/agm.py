"""
Annual General Meeting (AGM) voter RSVP processor.

Reads the RSVP form export, keeps each person's newest RSVP, verifies them (see agm_verify.py), records every RSVP in
the agm_rsvp table, and writes two attendee lists (everyone, and in-person only) plus a separate name-free
accommodations list for the organizers.
"""
import csv
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from utsu_core.agm_verify import is_yes, verify_rsvps
from utsu_core.incremental import (OUTCOME_SQL_TYPES, TRACKING_SQL_TYPES, add_missing_columns, apply_known,
                                   count_new, dedupe_by_uuid, fingerprint, known_rows, select_current, set_statuses)
from utsu_core.integrity import (build_alerts, changed_after_processing, duplicate_verified, hold, record_invalid,
                                 restored_after_processing, write_alerts)
from utsu_core.senate import parse_submission_ts, submission_rank
from utsu_core.verification import mapped_names, setup_reader
from utsu_std.csv_safe import SafeDictWriter
from utsu_std.utils import absfile, application_uuid_for, identity_key, normalize_case, student_uuid_for

logger = logging.getLogger(__name__)

LIST_FIELDS = ["last_name", "first_name", "preferred_name", "pronouns", "email", "meeting_preference", "contact_ok",
               "flags", "verification_level", "checked_in"]
UNVERIFIED_FIELDS = ["uuid", "student_uuid", "reason", "submission_ts", "full_name", "student_number", "email",
                     "personal_email", "meeting_preference", "flags"]
ACCOMMODATION_FIELDS = ["uuid", "meeting_preference", "dietary", "accessibility"]
# the form needs at least one of these; an RSVP may give a name, a student number ("Student Number" column) or both
IDENTITY_COLUMNS = ("first_name", "last_name", "Student Number")


def agm_uuid(rsvp: dict[str, str]) -> str:
    """Internal id for an RSVP, so reprocessing the same form yields the same uuid."""
    return application_uuid_for("agm", rsvp.get("submission_ts", ""), rsvp.get("email", ""),
                                rsvp.get("student_number", ""), rsvp.get("full_name", ""))


def person_key(rsvp: dict[str, str]) -> str:
    """Who an RSVP belongs to: their email, else student number, else name."""
    return identity_key(rsvp.get("email", ""), rsvp.get("student_number", ""), rsvp.get("full_name", ""))


def is_in_person(meeting_preference: str) -> bool:
    """
    Whether a meeting preference means attending in person.

    >>> [is_in_person(v) for v in ("In-person", "in person", "In Person (Hart House)", "Online", "Virtual", "")]
    [True, True, True, False, False, False]
    """
    return bool(re.search(r"\bin[\s-]*person\b", meeting_preference or "", re.I))


def attendance(meeting_preference: str) -> str:
    """
    How an RSVP's meeting preference is shown in the output files: In-person when the answer says so, otherwise
    Virtual (a blank or unclear answer defaults to Virtual).

    >>> attendance("Yes, I'll be attending in-person."), attendance("No, I will be attending virtually.")
    ('In-person', 'Virtual')
    >>> attendance("Online (Zoom)"), attendance(""), attendance("Undecided"), attendance(None)
    ('Virtual', 'Virtual', 'Virtual', 'Virtual')
    """
    return "In-person" if is_in_person(meeting_preference) else "Virtual"


def build_rsvp(row: dict[str, str]) -> dict:
    rsvp = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
    rsvp["full_name"] = normalize_case(f"{rsvp.get('first_name', '')} {rsvp.get('last_name', '')}")
    rsvp["student_number"] = rsvp.get("Student Number", "")  # optional column, kept under its original header
    rsvp["contact_ok"] = "yes" if is_yes(rsvp.get("contact_consent", "")) else "no"
    rsvp["uuid"] = agm_uuid(rsvp)
    rsvp["student_uuid"] = student_uuid_for(rsvp.get("email", ""), rsvp["student_number"], rsvp["full_name"])
    return rsvp


def read_rsvps(csv_file: Path) -> list[dict]:
    mapping = absfile("data/conversion_table_agm.csv")
    reader = setup_reader(csv_file, mapping, optional=("first_name", "last_name"), identity=IDENTITY_COLUMNS)
    fields = mapped_names(mapping) + ["Student Number"]
    rsvps = []
    for row in reader:
        rsvp = build_rsvp(row)
        rsvp["source_hash"] = fingerprint(row, fields)
        rsvps.append(rsvp)
    return dedupe_by_uuid([r for r in rsvps if r["full_name"] or r["student_number"]])


def current_rsvps(rsvps: list[dict]) -> list[dict]:
    """
    The binding RSVP per person (see incremental.select_current): the newest verified one, so an unverified
    RSVP never replaces a verified one; with none verified, the newest. Needs each RSVP's status to be set.
    """
    return select_current(rsvps, person_key, submission_rank)[0]


def attendee_list(verified: list[dict], in_person_only: bool = False) -> list[dict]:
    """Verified attendees sorted by last then first name, for check-in; optionally only those attending in person."""
    rows = [r for r in verified if not in_person_only or is_in_person(r.get("meeting_preference", ""))]
    return sorted(rows, key=lambda r: (r.get("last_name", "").lower(), r.get("first_name", "").lower(), r["uuid"]))


_UPSERT = """
INSERT INTO agm_rsvp (uuid, student_uuid, uoft_uuid, verification_level, status, reason, match_method,
                      submission_ts, meeting_preference, contact_ok, flags, source_hash)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :status, :reason, :match_method, :submission_ts,
        :meeting_preference, :contact_ok, :flags, :source_hash)
ON CONFLICT(uuid) DO UPDATE SET student_uuid = excluded.student_uuid, uoft_uuid = excluded.uoft_uuid,
    verification_level = CASE WHEN excluded.status = 'verified' AND agm_rsvp.verification_level = 3
                              THEN 3 ELSE excluded.verification_level END,
    status = excluded.status, reason = excluded.reason, match_method = excluded.match_method,
    source_hash = COALESCE(excluded.source_hash, agm_rsvp.source_hash),
    flags = excluded.flags
"""


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(Path(absfile("src/schema/agm.sql")).read_text(encoding="utf-8"))
    add_missing_columns(conn, "agm_rsvp", {**TRACKING_SQL_TYPES, **OUTCOME_SQL_TYPES})


def save_to_db(conn: sqlite3.Connection, rsvps: list[dict]):
    """Add (or refresh) one agm_rsvp row per RSVP. No names or contact details are stored."""
    ensure_schema(conn)
    conn.executemany(_UPSERT, rsvps)
    conn.commit()
    logger.info(f"Saved {len(rsvps)} RSVP row(s) to agm_rsvp")


def write_csv(rows: list[dict], fields: list[str], path: Path):
    """Write rows to a CSV, with the meeting preference shown simply as In-person / Virtual / Not stated."""
    shown = [{**r, "meeting_preference": attendance(r.get("meeting_preference", ""))} for r in rows]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = SafeDictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(shown)
    logger.info(f"Wrote {len(rows)} rows to {path}")


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value or "(blank)"] = counts.get(value or "(blank)", 0) + 1
    return dict(sorted(counts.items()))


def process_agm(csv_file: Path | str, conn: sqlite3.Connection, output_dir: Path | str = "secrets/output",
                reverify: bool = False) -> dict:
    """
    Process an AGM voter RSVP export and write, under output_dir:
      agm_list_all.csv        every verified attendee (door list; no ids; meeting preference as In-person / Virtual)
      agm_list_in_person.csv  verified attendees whose meeting preference is in person
      agm_unverified.csv      RSVPs that could not be verified, with the reason
      agm_accommodations.csv  dietary and accessibility needs of verified attendees, by uuid only (no names)
      alerts_agm.csv          what a person has to look at (invalid, duplicate, non-binding RSVPs)
      agm_run.json            counts
    The lists carry names and emails and are private. Every RSVP is also recorded in the agm_rsvp table.

    Give the full export each time. An RSVP is looked at once: a new one is verified. An unverified one whose row
    was edited is processed again (that is how a correction is made); a verified one is immutable and an edit to
    its row makes it invalid: it stays verified but is held out of the attendee lists and raised in
    alerts_agm.csv until a person deals with it (integrity.py). With `reverify` (after new student data)
    everything is verified again. Per person, the newest verified RSVP is the binding one; a newer unverified one
    never replaces it (it becomes non-binding). The lists always cover the whole export.
    """
    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)

    rsvps = read_rsvps(Path(csv_file))
    ensure_schema(conn)
    known = known_rows(conn, "agm_rsvp", ("reason", "flags"))
    to_check = apply_known(rsvps, known, reverify)
    changed = changed_after_processing(rsvps, known, "AGM")
    released = restored_after_processing(rsvps, known)
    verify_rsvps(to_check, conn.cursor())
    current, statuses = select_current(rsvps, person_key, submission_rank)
    logger.info(f"{len(rsvps)} AGM RSVPs read; {count_new(rsvps, known)} new, {len(to_check)} verified now, "
                f"{len(changed)} changed since processed; {len(current)} binding after "
                f"{sum(s == 'superseded' for s in statuses.values())} superseded and "
                f"{sum(s == 'non_binding' for s in statuses.values())} non-binding")
    unverified = [r for r in current if r["status"] == "unverified"]

    save_to_db(conn, [r for r in dedupe_by_uuid(to_check + current) if r["uuid"] not in changed])
    set_statuses(conn, "agm_rsvp", statuses)
    record_invalid(conn, "agm_rsvp", changed, released)
    duplicates = duplicate_verified(conn, "agm_rsvp", "AGM")
    # an invalid (held) RSVP stays verified but is left out of the attendee lists until a person resolves it
    verified, held = hold([r for r in current if r["status"] == "verified"])
    everyone, in_person = attendee_list(verified), attendee_list(verified, in_person_only=True)
    write_csv(everyone, LIST_FIELDS, out / "agm_list_all.csv")
    write_csv(in_person, LIST_FIELDS, out / "agm_list_in_person.csv")
    write_csv(unverified, UNVERIFIED_FIELDS, out / "agm_unverified.csv")
    write_csv([r for r in everyone if r.get("dietary") or r.get("accessibility")], ACCOMMODATION_FIELDS,
              out / "agm_accommodations.csv")
    alerts = build_alerts(conn, "agm_rsvp", "AGM", rsvps, statuses, duplicates)
    write_alerts(out, "agm", alerts)

    summary = {"submissions_in_export": len(rsvps),
               "new_submissions_this_run": count_new(rsvps, known),
               "processed_this_run": len(to_check),
               "binding_rsvps": len(current),
               "verified_rsvps": len(verified), "verified_in_person": len(in_person),
               "unverified_rsvps": len(unverified), "changed_after_processing": len(changed),
               "held_invalid": len(held), "alerts": len(alerts), "integrity_duplicates": len(duplicates),
               "non_binding": sum(s == "non_binding" for s in statuses.values()),
               "attendance": _tally(attendance(r.get("meeting_preference", "")) for r in verified),
               "match_method": _tally(r["match_method"] for r in verified),
               "unverified_reasons": _tally(r["reason"] for r in unverified),
               "dietary_requests": sum(bool(r.get("dietary")) for r in verified),
               "accessibility_requests": sum(bool(r.get("accessibility")) for r in verified),
               "flags": _tally(f for r in current for f in (r.get("flags") or "").split(";") if f)}
    (out / "agm_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"AGM: {len(verified)} verified ({len(in_person)} in person), {len(unverified)} unverified")
    return {"verified": verified, "in_person": in_person, "unverified": unverified, "held": held, "alerts": alerts,
            "summary": summary}


if __name__ == "__main__":
    import doctest
    doctest.testmod()
