"""
Choosing the binding application per person from a full form export.

Every run reads the whole export. Google Forms stamps a new timestamp on an edited or re-submitted response, so an
edit is simply a newer submission: per person (by email, else student number, else name) the newest verified
application is the binding one and the older ones are superseded (see `select_current`).
"""
import logging
import re
import sqlite3
from datetime import datetime
from typing import Callable

from utsu_std.utils import identity_key

logger = logging.getLogger(__name__)

CHUNK = 500
# What is saved with each application about how it was verified (see verification.Match).
OUTCOME_COLUMNS = ("uoft_uuid", "verification_level", "match_method")

# Statuses: an application is verified or unverified. It can then be superseded (a newer one from the same student
# replaced it), non_binding (an unverified one that did not replace a verified application) or not_considered (the
# student already has a completed application, so this one is never looked at).
LIVE = ("verified", "unverified")


def add_flag(app: dict, flag: str) -> None:
    flags = [f for f in (app.get("flags") or "").split(";") if f]
    if flag not in flags:
        flags.append(flag)
    app["flags"] = ";".join(flags)


def dedupe_by_uuid(rows: list[dict]) -> list[dict]:
    """
    One row per uuid, the last one in the file winning (two rows with the same timestamp and student are the same
    submission), keeping the order of first appearance.

    >>> dedupe_by_uuid([{"uuid": "a", "n": 1}, {"uuid": "b"}, {"uuid": "a", "n": 2}])
    [{'uuid': 'a', 'n': 2}, {'uuid': 'b'}]
    """
    latest = {row["uuid"]: row for row in rows}
    return list(latest.values())


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
    try:
        return datetime.strptime(f"{date} {time} {meridiem.upper()}M", "%Y/%m/%d %I:%M:%S %p")
    except ValueError:  # an hour such as 0 or 13 with a.m./p.m., or an impossible date
        return None


def warn_unreadable_timestamps(rows: list[dict], label: str) -> int:
    """
    Warn when submission timestamps cannot be read. They decide which of a person's submissions is the newest; for an
    unreadable one the order in the file decides instead (the form appends new responses at the end). Returns how many.
    """
    bad = [row for row in rows if parse_submission_ts(row.get("submission_ts", "")) is None]
    if bad:
        examples = ", ".join(repr(row.get("submission_ts", "")) for row in bad[:3])
        logger.warning(f"{len(bad)} of {len(rows)} {label} submissions have a timestamp that could not be read "
                       f"(for example {examples}); for those, the order in the file decides which is the newest. "
                       f"Expected a form timestamp such as '2026/08/14 1:30:47 p.m. AST'.")
    return len(bad)


def submission_rank(row: dict) -> datetime:
    """When a row was submitted, for deciding which of a person's submissions is the newest (unreadable: oldest)."""
    return parse_submission_ts(row.get("submission_ts", "")) or datetime.min


def person_key(row: dict) -> str:
    """
    Whose submission a row is: their email, else student number (`id` or `student_number`), else name, so
    all of a person's submissions are grouped together (see utsu_std.utils.identity_key).

    >>> person_key({"email": "A@x.ca", "id": "1"}) == person_key({"email": "a@x.ca", "student_number": "2"})
    True
    """
    return identity_key(row.get("email", ""), row.get("id") or row.get("student_number") or "",
                        row.get("full_name", ""))


def select_current(rows: list[dict], key_fn: Callable[[dict], str] = person_key,
                   rank_fn: Callable[[dict], object] = submission_rank) -> tuple[list[dict], dict[str, str]]:
    """
    Choose the one binding application per person (`key_fn`, by default their email, else student number, else
    name; `rank_fn` orders submissions, by default by timestamp, oldest first) among the rows whose status is
    verified or unverified. Returns (current rows in their original order, {uuid: new status} for the rows that lost).

    The newest *verified* application wins, so an unverified submission can never replace a verified one: it becomes
    `non_binding` (and is logged, since someone may be impersonating the student). Without any verified application
    the newest one wins. Older rows than the winner become `superseded`.

    A row marked `final` (a completed application: see sap.COMPLETE_STAGE) always wins and is never replaced; every
    other row of that student becomes `not_considered`.
    """
    groups: dict[str, list[tuple]] = {}
    for position, row in enumerate(rows):
        if row.get("status") in LIVE:
            groups.setdefault(key_fn(row), []).append((rank_fn(row), position, row))

    winners, changes = set(), {}
    for members in groups.values():
        members.sort(key=lambda m: (m[0], m[1]))
        final = [m for m in members if m[2].get("final")]
        verified = [m for m in members if m[2]["status"] == "verified"]
        winner = final[-1] if final else (verified or members)[-1]
        winners.add(winner[2]["uuid"])
        for member in members:
            if member is winner:
                continue
            row = member[2]
            if final:
                changes[row["uuid"]] = "not_considered"
                logger.warning(f"Application {row['uuid']} is not considered: the student already has a completed "
                               f"application ({winner[2]['uuid']}).")
            elif (member[0], member[1]) < (winner[0], winner[1]):
                changes[row["uuid"]] = "superseded"
            else:
                changes[row["uuid"]] = "non_binding"
                logger.warning(f"Unverified submission {row['uuid']} does not replace the verified application "
                               f"{winner[2]['uuid']} from the same student; it is non-binding. Check it was not "
                               f"someone else using their details.")
    for row in rows:
        if row.get("uuid") in changes:
            row["status"] = changes[row["uuid"]]
    return [row for row in rows if row.get("uuid") in winners], changes


def set_statuses(conn: sqlite3.Connection, table: str, changes: dict[str, str]) -> None:
    """Record new statuses (see `select_current`) for applications already saved in `table`."""
    for status in sorted(set(changes.values())):
        uuids = sorted(u for u, s in changes.items() if s == status)
        for start in range(0, len(uuids), CHUNK):
            chunk = uuids[start:start + CHUNK]
            conn.execute(f"UPDATE {table} SET status = ? WHERE status != ? AND uuid IN ({','.join('?' * len(chunk))})",
                         [status, status, *chunk])
    conn.commit()
