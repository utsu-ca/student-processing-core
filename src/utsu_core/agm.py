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
from utsu_core.incremental import (OUTCOME_SQL_TYPES, add_missing_columns, apply_known, count_new, known_rows,
                                   mark_superseded)
from utsu_core.senate import parse_submission_ts
from utsu_core.verification import setup_reader
from utsu_std.csv_safe import SafeDictWriter
from utsu_std.utils import absfile, application_uuid_for, identity_key, normalize_case, student_uuid_for

logger = logging.getLogger(__name__)

LIST_FIELDS = ["last_name", "first_name", "preferred_name", "pronouns", "email", "meeting_preference", "contact_ok",
               "flags", "uuid", "student_uuid", "verification_level", "checked_in"]
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


def build_rsvp(row: dict[str, str]) -> dict:
    rsvp = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
    rsvp["full_name"] = normalize_case(f"{rsvp.get('first_name', '')} {rsvp.get('last_name', '')}")
    rsvp["student_number"] = rsvp.get("Student Number", "")  # optional column, kept under its original header
    rsvp["contact_ok"] = "yes" if is_yes(rsvp.get("contact_consent", "")) else "no"
    rsvp["uuid"] = agm_uuid(rsvp)
    rsvp["student_uuid"] = student_uuid_for(rsvp.get("email", ""), rsvp["student_number"], rsvp["full_name"])
    return rsvp


def read_rsvps(csv_file: Path) -> list[dict]:
    reader = setup_reader(csv_file, absfile("data/conversion_table_agm.csv"), optional=("first_name", "last_name"),
                          identity=IDENTITY_COLUMNS)
    rsvps = [build_rsvp(row) for row in reader]
    return [r for r in rsvps if r["full_name"] or r["student_number"]]


def keep_newest_rsvp(rsvps: list[dict]) -> list[dict]:
    """Keep each person's newest RSVP (rows with an unreadable timestamp rank by their position in the file)."""
    newest: dict[str, tuple[tuple, dict]] = {}
    for position, rsvp in enumerate(rsvps):
        rank = (parse_submission_ts(rsvp.get("submission_ts", "")) or datetime.min, position)
        key = person_key(rsvp)
        if key not in newest or rank > newest[key][0]:
            newest[key] = (rank, rsvp)
    return [r for _, r in sorted(newest.values(), key=lambda item: item[0][1])]


def attendee_list(verified: list[dict], in_person_only: bool = False) -> list[dict]:
    """Verified attendees sorted by last then first name, for check-in; optionally only those attending in person."""
    rows = [r for r in verified if not in_person_only or is_in_person(r.get("meeting_preference", ""))]
    return sorted(rows, key=lambda r: (r.get("last_name", "").lower(), r.get("first_name", "").lower(), r["uuid"]))


_UPSERT = """
INSERT INTO agm_rsvp (uuid, student_uuid, uoft_uuid, verification_level, status, reason, match_method,
                      submission_ts, meeting_preference, contact_ok, flags)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :status, :reason, :match_method, :submission_ts,
        :meeting_preference, :contact_ok, :flags)
ON CONFLICT(uuid) DO UPDATE SET student_uuid = excluded.student_uuid, uoft_uuid = excluded.uoft_uuid,
    verification_level = CASE WHEN excluded.status = 'verified' AND agm_rsvp.verification_level = 3
                              THEN 3 ELSE excluded.verification_level END,
    status = excluded.status, reason = excluded.reason, match_method = excluded.match_method,
    flags = excluded.flags
"""


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(Path(absfile("src/schema/agm.sql")).read_text(encoding="utf-8"))
    add_missing_columns(conn, "agm_rsvp", {"student_uuid": "TEXT", **OUTCOME_SQL_TYPES})


def save_to_db(conn: sqlite3.Connection, rsvps: list[dict]):
    """Add (or refresh) one agm_rsvp row per RSVP. No names or contact details are stored."""
    ensure_schema(conn)
    conn.executemany(_UPSERT, rsvps)
    conn.commit()
    logger.info(f"Saved {len(rsvps)} RSVP row(s) to agm_rsvp")


def write_csv(rows: list[dict], fields: list[str], path: Path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = SafeDictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
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
      agm_list_all.csv        every verified attendee
      agm_list_in_person.csv  verified attendees whose meeting preference is in person
      agm_unverified.csv      RSVPs that could not be verified, with the reason
      agm_accommodations.csv  dietary and accessibility needs of verified attendees, by uuid only (no names)
      agm_run.json            counts
    The lists carry names and emails and are private. Every RSVP is also recorded in the agm_rsvp table.

    Give the full export each time: RSVPs already in agm_rsvp keep their stored outcome and are not verified
    again (unless `reverify`); only the new ones are verified. The lists always cover the whole export.
    """
    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)

    rsvps = read_rsvps(Path(csv_file))
    current = keep_newest_rsvp(rsvps)
    ensure_schema(conn)
    known = known_rows(conn, "agm_rsvp", ("reason", "flags"))
    to_check = apply_known(current, known, reverify)
    logger.info(f"{len(rsvps)} AGM RSVPs read; {len(rsvps) - len(current)} older duplicates dropped; "
                f"{count_new(current, known)} new, {len(to_check)} to verify, "
                f"{len(current) - len(to_check)} already verified")
    verify_rsvps(to_check, conn.cursor())
    verified = [r for r in current if r["status"] == "verified"]
    unverified = [r for r in current if r["status"] == "unverified"]

    save_to_db(conn, current)
    mark_superseded(conn, "agm_rsvp", {r["uuid"] for r in rsvps} - {r["uuid"] for r in current})
    everyone, in_person = attendee_list(verified), attendee_list(verified, in_person_only=True)
    write_csv(everyone, LIST_FIELDS, out / "agm_list_all.csv")
    write_csv(in_person, LIST_FIELDS, out / "agm_list_in_person.csv")
    write_csv(unverified, UNVERIFIED_FIELDS, out / "agm_unverified.csv")
    write_csv([r for r in everyone if r.get("dietary") or r.get("accessibility")], ACCOMMODATION_FIELDS,
              out / "agm_accommodations.csv")

    summary = {"rsvps": len(current), "new": count_new(current, known), "verified": len(verified),
               "in_person": len(in_person),
               "unverified": len(unverified),
               "meeting_preference": _tally(r.get("meeting_preference") for r in verified),
               "match_method": _tally(r["match_method"] for r in verified),
               "unverified_reasons": _tally(r["reason"] for r in unverified),
               "dietary_requests": sum(bool(r.get("dietary")) for r in verified),
               "accessibility_requests": sum(bool(r.get("accessibility")) for r in verified),
               "flags": _tally(f for r in current for f in r["flags"].split(";") if f)}
    (out / "agm_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"AGM: {len(verified)} verified ({len(in_person)} in person), {len(unverified)} unverified")
    return {"verified": verified, "in_person": in_person, "unverified": unverified, "summary": summary}


if __name__ == "__main__":
    import doctest
    doctest.testmod()
