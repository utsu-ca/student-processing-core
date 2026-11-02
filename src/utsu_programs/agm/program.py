"""
Annual General Meeting (AGM) voter RSVP processor.

Reads the RSVP form export, keeps each person's newest RSVP, verifies them against uoft_data, records every RSVP in
the agm_rsvp table, and writes two attendee lists (everyone, and in-person only) plus a separate name-free
accommodations list for the organizers.

The verification can also be run on its own, which only reports (agm_verification.csv); it writes no voter lists
and no database rows:

    utsu-agm --verify_only --agm_file RSVP.csv --db path/to/db.sqlite [--output_dir DIR]
    utsu-agm --verify_only --testing   # fake RSVPs from the last `utsu-agm --testing` run, against its database

The RSVP form may have no student number, so a person is then matched on their ACORN-registered name alone. A name
shared by more than one student cannot be decided and stays unverified until the form also supplies a "Student
Number" or "Division" column, which is then used to tell them apart. A name is always needed.
"""
import json
import logging
import re
import sqlite3
from pathlib import Path

from utsu_core.pipeline import Run, read_rows, verify_rows
from utsu_core.selection import select_current
from utsu_core.spec import ProgramSpec
from utsu_core.verification import (MAX_CANDIDATES, METHOD_FUZZY_NAME, METHOD_NAME, Match, is_uoft_email,
                                    truncated_numbers)
from utsu_std.csv_safe import check_writable, write_rows
from utsu_std.utils import (UOFT_MAIL_DOMAIN, absfile, application_uuid_for, normalize_case, normalize_uoft_email,
                            student_uuid_for, EMAIL_FLAGS)

logger = logging.getLogger(__name__)

LIST_FIELDS = ["last_name", "first_name", "student_number", "preferred_name", "pronouns", "email",
               "meeting_preference", "contact_ok", "flags", "verification_level", "checked_in"]
UNVERIFIED_FIELDS = ["uuid", "student_uuid", "reason", "submission_ts", "full_name", "student_number", "email",
                     "personal_email", "meeting_preference", "flags"]
ACCOMMODATION_FIELDS = ["uuid", "meeting_preference", "dietary", "accessibility"]
REPORT_FIELDS = ["uuid", "status", "reason", "match_method", "verification_level", "student_uuid", "uoft_uuid",
                 "full_name", "email", "flags"]
FUZZY_REVIEW_FIELDS = (["uuid", "status", "reason", "submission_ts", "full_name", "student_number", "division", "email",
                        "match_method", "verification_level", "name_similarity", "uoft_uuid", "flags"]
                       + [f"match{i}_{field}" for i in range(1, MAX_CANDIDATES + 1)
                          for field in ("uoft_uuid", "name", "student_number", "division", "similarity", "selected")]
                       + ["reviewer_decision", "reviewer_notes"])
# the form needs a name column; "Student Number" and "Division" are optional and only narrow a match
IDENTITY_COLUMNS = ("first_name", "last_name")
EMAIL_REASON = f"email is not a {UOFT_MAIL_DOMAIN} address"
# answers to the dietary and accessibility questions that say there is nothing to accommodate
NO_NEED = {"", "none", "n/a", "na", "no", "nil", "nothing", "not applicable", "no restrictions", "no restriction",
           "none at this time", "no allergies", "no dietary restrictions", "no accessibility requirements"}


def real_need(answer: str) -> str:
    """
    The answer to a dietary or accessibility question, or '' when it only says there is no need ("None", "N/A", ...).

    >>> real_need("None"), real_need(" N/A. "), real_need("n.a."), real_need("Vegan"), real_need("No gluten")
    ('', '', '', 'Vegan', 'No gluten')
    """
    text = (answer or "").strip()
    return "" if re.sub(r"[^a-z/ ]", "", text.lower()).strip() in NO_NEED else text


def agm_uuid(rsvp: dict[str, str]) -> str:
    """Internal id for an RSVP, so reprocessing the same form yields the same uuid."""
    return application_uuid_for("agm", rsvp.get("submission_ts", ""), rsvp.get("email", ""),
                                rsvp.get("student_number", ""), rsvp.get("full_name", ""))


def is_yes(value: str) -> bool:
    """
    >>> is_yes(" Yes, I agree")
    True
    >>> is_yes("")
    False
    """
    return (value or "").strip().lower().startswith("yes")


def is_ambiguous(reason: str) -> bool:
    """
    Whether an unverified RSVP failed because more than one student matches it (a shared name).

    >>> is_ambiguous("name shared by 2 students; a student number is needed"), is_ambiguous("name not found")
    (True, False)
    """
    return "shared by" in (reason or "")


def review_flags(rsvp: dict, match_method: str, reason: str, fuzzy_flag: str = "") -> str:
    flags = []
    if match_method in (METHOD_NAME, METHOD_FUZZY_NAME):
        flags.append("name_only_match")
    if fuzzy_flag:
        flags.append(fuzzy_flag)
    if is_ambiguous(reason):
        flags.append("ambiguous_name")
    if not is_yes(rsvp.get("data_confirmation", "")):
        flags.append("no_data_confirmation")
    if not rsvp.get("email"):
        flags.append("no_uoft_email")
    if rsvp.get("email_status") in EMAIL_FLAGS:
        flags.append(EMAIL_FLAGS[rsvp["email_status"]])
    return ";".join(flags)


def annotate(rsvp: dict, match: Match) -> None:
    """What an RSVP records about how it was decided: the reason, review flags and the close roster names."""
    rsvp.update(reason=match.reason, flags=review_flags(rsvp, match.method, match.reason, match.fuzzy_flag),
                name_candidates=match.candidates, name_similarity=match.similarity)
    if not match.verified:
        logger.warning(f"UNVERIFIED AGM RSVP {rsvp['uuid']}: {match.reason}")
        if is_ambiguous(match.reason):
            logger.warning(f"AMBIGUOUS AGM RSVP {rsvp['uuid']}: more than one student matches ({match.reason}). "
                           f"A person has to decide which student this is.")


def require_uoft_email(rsvp: dict) -> Match | None:
    """An RSVP whose email is not a `@mail.utoronto.ca` address (or is blank) is never verified."""
    return None if is_uoft_email(rsvp.get("email")) else Match(None, None, "", EMAIL_REASON)


def build_rsvp(row: dict[str, str]) -> dict | None:
    rsvp = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
    rsvp["full_name"] = normalize_case(f"{rsvp.get('first_name', '')} {rsvp.get('last_name', '')}")
    rsvp["student_number"] = rsvp.get("Student Number", "")  # optional column, kept under its original header
    rsvp["division"] = rsvp.get("Division", "")  # optional column too; it takes part in the match when given
    rsvp["contact_ok"] = "yes" if is_yes(rsvp.get("contact_consent", "")) else "no"
    rsvp["email"], rsvp["email_status"] = normalize_uoft_email(rsvp.get("email", ""))
    rsvp["uuid"] = agm_uuid(rsvp)
    rsvp["student_uuid"] = student_uuid_for(rsvp.get("email", ""), rsvp["student_number"], rsvp["full_name"])
    return rsvp if rsvp["full_name"] or rsvp["student_number"] else None


_INSERT = """
INSERT INTO agm_rsvp (uuid, student_uuid, uoft_uuid, verification_level, status, reason, match_method,
                      submission_ts, meeting_preference, contact_ok, flags)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :status, :reason, :match_method, :submission_ts,
        :meeting_preference, :contact_ok, :flags)
"""

SPEC = ProgramSpec(
    slug="agm", label="AGM", table="agm_rsvp", package="utsu_programs.agm",
    mapping="data/conversion_table_agm.csv", build_row=build_rsvp, optional=IDENTITY_COLUMNS,
    identity=IDENTITY_COLUMNS, insert=_INSERT, id_key="student_number", sticky=False, veto=require_uoft_email,
    annotate=annotate, log_outcomes=False)


def verify_rsvps(rsvps: list[dict], cursor: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
    """
    Verify RSVPs (one per person) against uoft_data, by name, student number or both. Sets uoft_uuid,
    verification_level, match_method, reason, status and flags on each and returns (verified, unverified).
    An RSVP whose email is not a `@mail.utoronto.ca` address (or is blank) is never verified.
    """
    return verify_rows(SPEC, rsvps, cursor)


def is_in_person(meeting_preference: str) -> bool:
    """
    Whether a meeting preference means attending in person.

    >>> [is_in_person(v) for v in ("In-person", "in person", "In Person (Hart House)", "Online", "Virtual", "")]
    [True, True, True, False, False, False]
    >>> is_in_person("No, I will not be attending in person"), is_in_person("Yes, I'll be attending in-person.")
    (False, True)
    """
    text = meeting_preference or ""
    if not re.search(r"\bin[\s-]*person\b", text, re.I):
        return False
    return not re.search(r"\b(not|never|cannot|can't|won't|unable)\b[^.;]*\bin[\s-]*person\b", text, re.I)


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


def read_rsvps(csv_file: Path) -> list[dict]:
    return read_rows(SPEC, csv_file)


def verify_file(csv_file: Path | str, cursor: sqlite3.Cursor, output_dir: Path | str) -> list[dict]:
    """
    Read an RSVP export, verify it and write agm_verification.csv (private: names and emails) to output_dir, with
    the binding RSVP per person (see selection.select_current). Returns those RSVPs. Nothing is saved.
    """
    cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'uoft_data'")
    if not cursor.fetchone():
        raise ValueError("The database has no uoft_data table; import the UofT student data first")
    rsvps = read_rsvps(Path(csv_file))
    verified, unverified = verify_rsvps(rsvps, cursor)
    current = select_current(rsvps)[0]

    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    write_rows(out / "agm_verification.csv", REPORT_FIELDS, current)
    logger.info(f"AGM verification: {len(verified)} verified, {len(unverified)} unverified of {len(rsvps)} RSVPs "
                f"({len(current)} binding); report in {out / 'agm_verification.csv'}")
    return current


def attendee_list(verified: list[dict], in_person_only: bool = False) -> list[dict]:
    """Verified attendees sorted by last then first name, for check-in; optionally only those attending in person."""
    rows = [r for r in verified if not in_person_only or is_in_person(r.get("meeting_preference", ""))]
    return sorted(rows, key=lambda r: (r.get("last_name", "").lower(), r.get("first_name", "").lower(), r["uuid"]))


def with_roster_numbers(rows: list[dict], cursor: sqlite3.Cursor) -> list[dict]:
    """Copies of verified RSVPs whose student_number is the roster's truncated number, not what the form gave."""
    numbers = truncated_numbers(cursor, (r.get("uoft_uuid") for r in rows))
    return [{**r, "student_number": numbers.get(r.get("uoft_uuid"), "")} for r in rows]


def write_csv(rows: list[dict], fields: list[str], path: Path):
    """Write rows to a CSV, with the meeting preference shown simply as In-person / Virtual."""
    write_rows(path, fields, [{**r, "meeting_preference": attendance(r.get("meeting_preference", ""))} for r in rows])


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value or "(blank)"] = counts.get(value or "(blank)", 0) + 1
    return dict(sorted(counts.items()))


def fuzzy_review_rows(rsvps: list[dict]) -> list[dict]:
    """
    One row per RSVP whose name was not found exactly but has close roster students (see `match_students`), for a
    person to reconcile: the RSVP, how it was decided, and its closest students (up to `MAX_CANDIDATES`, best
    first) with `selected` marking the one it was verified as. An RSVP that could not be decided has none selected.
    The last two columns are left empty for the reviewer.

    >>> from utsu_core.verification import Candidate
    >>> c = Candidate("u1", "Ann Marie Lee", "xxxxx3161x", "UC", 1.0)
    >>> row, = fuzzy_review_rows([{"uuid": "a", "uoft_uuid": "u1", "name_candidates": (c,), "status": "verified"}])
    >>> row["match1_name"], row["match1_selected"], row["match2_name"], row["reviewer_decision"]
    ('Ann Marie Lee', 'SELECTED', '', '')
    """
    rows = []
    for rsvp in rsvps:
        if not rsvp.get("name_candidates"):
            continue
        row = {field: rsvp.get(field, "") for field in FUZZY_REVIEW_FIELDS}
        row["name_similarity"] = "" if rsvp.get("name_similarity") is None else f"{rsvp['name_similarity']:.2f}"
        for i, c in enumerate(rsvp["name_candidates"], start=1):
            row.update({f"match{i}_uoft_uuid": c.uoft_uuid, f"match{i}_name": c.full_name,
                        f"match{i}_student_number": c.student_number, f"match{i}_division": c.division,
                        f"match{i}_similarity": f"{c.similarity:.2f}",
                        f"match{i}_selected": "SELECTED" if c.uoft_uuid == rsvp.get("uoft_uuid") else ""})
        rows.append(row)
    return rows


def process_agm(csv_file: Path | str, conn: sqlite3.Connection, output_dir: Path | str = "secrets/output") -> dict:
    """
    Process an AGM voter RSVP export and write, under output_dir:
      agm_list_all.csv        every verified attendee (door list; no ids; meeting preference as In-person / Virtual;
                              the truncated student number is read from uoft_data)
      agm_list_in_person.csv  verified attendees whose meeting preference is in person
      agm_unverified.csv      RSVPs that could not be verified, with the reason
      agm_fuzzy_review.csv    RSVPs matched (or nearly) on a close rather than exact first name, with the closest
                              students, for a person to reconcile
      agm_accommodations.csv  dietary and accessibility needs of verified attendees, by uuid only (no names)
      alerts_agm.csv          what a person has to look at (duplicate, ambiguous, non-binding RSVPs)
      agm_run.json            counts
    The lists carry names and emails and are private. The agm_rsvp table is rebuilt from the export.

    Give the full export each time. Every RSVP in it is verified against the current student data (the status
    follows the student data, so it can change when the data is re-imported). Only a `@mail.utoronto.ca` email counts:
    an RSVP with another or no email is unverified. An edit or a re-submission is a newer
    RSVP: per person, the newest verified RSVP is the binding one; a newer unverified one never replaces it (it
    becomes non-binding). The lists always cover the whole export. Every output file is checked to be writable
    before anything is changed, and the agm_rsvp table is dropped and created again in one transaction.
    """
    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    files = {name: out / name for name in ("agm_list_all.csv", "agm_list_in_person.csv", "agm_unverified.csv",
                                            "agm_fuzzy_review.csv", "agm_accommodations.csv", "alerts_agm.csv",
                                            "agm_run.json")}
    check_writable(*files.values())  # before anything changes: a file open in Excel stops the run here

    run = Run.start(SPEC, conn, csv_file)
    run.verify(conn.cursor())
    run.select()
    unverified = [r for r in run.current if r["status"] == "unverified"]

    run.replace_all()
    run.settle()
    verified = [r for r in run.current if r["status"] == "verified"]
    everyone, in_person = attendee_list(verified), attendee_list(verified, in_person_only=True)
    # an answer such as "None" or "N/A" is not a need
    with_needs = [{**r, "dietary": real_need(r.get("dietary")), "accessibility": real_need(r.get("accessibility"))}
                  for r in everyone]
    with_needs = [r for r in with_needs if r["dietary"] or r["accessibility"]]
    write_csv(with_roster_numbers(everyone, conn.cursor()), LIST_FIELDS, files["agm_list_all.csv"])
    write_csv(with_roster_numbers(in_person, conn.cursor()), LIST_FIELDS, files["agm_list_in_person.csv"])
    write_csv(unverified, UNVERIFIED_FIELDS, files["agm_unverified.csv"])
    fuzzy_rows = fuzzy_review_rows(run.current)
    write_rows(files["agm_fuzzy_review.csv"], FUZZY_REVIEW_FIELDS, fuzzy_rows)
    write_csv(with_needs, ACCOMMODATION_FIELDS, files["agm_accommodations.csv"])
    # an RSVP that more than one student fits cannot be verified: raise it for a person, every run until resolved
    ambiguous = [r for r in unverified if is_ambiguous(r.get("reason", ""))]
    alerts = run.alert(out, ambiguous)

    summary = {**run.summary(alerts),
               "binding_rsvps": len(run.current),
               "verified_rsvps": len(verified), "verified_in_person": len(in_person),
               "unverified_rsvps": len(unverified), "ambiguous_matches": len(ambiguous),
               "attendance": _tally(attendance(r.get("meeting_preference", "")) for r in verified),
               "match_method": _tally(r["match_method"] for r in verified),
               "unverified_reasons": _tally(r["reason"] for r in unverified),
               "dietary_requests": sum(bool(r["dietary"]) for r in with_needs),
               "accessibility_requests": sum(bool(r["accessibility"]) for r in with_needs),
               "fuzzy_name_reviews": len(fuzzy_rows),
               "flags": _tally(f.partition("=")[0] for r in run.current for f in (r.get("flags") or "").split(";") if f)}
    files["agm_run.json"].write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"AGM: {len(verified)} verified ({len(in_person)} in person), {len(unverified)} unverified")
    return {"verified": verified, "in_person": in_person, "unverified": unverified, "alerts": alerts,
            "ambiguous": ambiguous, "summary": summary}
