"""
The steps Senate, SABP and AGM share when they process a form export, so that all three behave the same way.

    run = Run(conn, "agm_rsvp", "AGM", "agm", rows, reverify)    # what is new, edited, invalid (nothing is changed yet)
    ... verify run.to_check ...
    run.select(person_key, submission_rank)                       # the binding application per person
    conn.executemany(UPSERT, run.to_save())                       # each program saves its own columns
    run.settle()                                                  # statuses, invalid holds, duplicate check
    verified, held = hold([r for r in run.current if r["status"] == "verified"])
    alerts = run.alert(out_dir)                                   # alerts_<program>.csv

See incremental.py (what to process) and integrity.py (what to hold and raise) for the rules themselves.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Callable

from utsu_core.incremental import (OUTCOME_SQL_TYPES, TRACKING_SQL_TYPES, add_missing_columns, apply_known,
                                   count_new, dedupe_by_uuid, known_rows, person_key, select_current, set_statuses,
                                   submission_rank)
from utsu_core.integrity import (build_alerts, changed_after_processing, duplicate_verified, record_invalid,
                                 restored_after_processing, write_alerts)
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)


def ensure_tracked_table(conn: sqlite3.Connection, schema_file: str, table: str) -> None:
    """Create a program's tables and add the tracking columns a database from an older version lacks."""
    conn.executescript(Path(absfile(schema_file)).read_text(encoding="utf-8-sig"))
    add_missing_columns(conn, table, {**TRACKING_SQL_TYPES, **OUTCOME_SQL_TYPES})


class Run:
    """
    One pass over a full form export for one program (`label` for people, `slug` for file names).

    Which submission is the binding one is decided the same way everywhere: by `key` (default: the person, by
    email, else student number, else name) and `rank` (default: the submission timestamp), the newest verified
    one winning. A program that needs more passes a callback:

    `set_aside(rows)` is called once the stored outcomes are applied and before anything is verified. It returns
    the rows that must not be considered at all (they become `not_considered` and are not verified), and may mark
    a row `final` so that it always wins (SABP uses it for applications that are already complete).
    """

    def __init__(self, conn: sqlite3.Connection, table: str, label: str, slug: str, rows: list[dict],
                 reverify: bool = False, stored_columns: tuple[str, ...] = (), key: Callable[[dict], str] = person_key,
                 rank: Callable[[dict], object] = submission_rank,
                 set_aside: Callable[[list[dict]], list[dict]] | None = None):
        self.conn, self.table, self.label, self.slug, self.rows = conn, table, label, slug, rows
        self.key, self.rank = key, rank
        self.known = known_rows(conn, table, stored_columns)
        self.new = count_new(rows, self.known)
        self.to_check = apply_known(rows, self.known, reverify)
        self.changed = changed_after_processing(rows, self.known, label)
        self.released = restored_after_processing(rows, self.known)
        self.ignored = set_aside(rows) if set_aside else []
        self.to_check = [a for a in self.to_check if a.get("status") != "not_considered"]
        self.current: list[dict] = []
        self.statuses: dict[str, str] = {}
        self.duplicates: list[tuple] = []

    def select(self) -> None:
        """Pick the binding application per key (see incremental.select_current)."""
        self.current, self.statuses = select_current(self.rows, self.key, self.rank)
        self.statuses.update({a["uuid"]: "not_considered" for a in self.ignored})
        count = lambda status: sum(s == status for s in self.statuses.values())
        logger.info(f"{len(self.rows)} {self.label} submissions read; {self.new} new, {len(self.to_check)} verified "
                    f"now, {len(self.changed)} changed since processed; {len(self.current)} binding after "
                    f"{count('superseded')} superseded, {count('non_binding')} non-binding and "
                    f"{count('not_considered')} not considered")

    def to_save(self) -> list[dict]:
        """The rows to write to the table: what was processed or is binding, never a changed (invalid) locked row."""
        return [a for a in dedupe_by_uuid(self.to_check + self.ignored + self.current)
                if a["uuid"] not in self.changed]

    def settle(self, group_columns: tuple[str, ...] = ("student_uuid",)) -> None:
        """After the rows are saved: record the new statuses and holds, and check for duplicate verified ones."""
        set_statuses(self.conn, self.table, self.statuses)
        record_invalid(self.conn, self.table, self.changed, self.released)
        self.duplicates = duplicate_verified(self.conn, self.table, self.label, group_columns)

    def alert(self, out: Path, ambiguous: list[dict] = ()) -> list[dict]:
        """Write alerts_<program>.csv: what a person has to look at. Returns the alerts."""
        alerts = build_alerts(self.conn, self.table, self.label, self.rows, self.statuses, self.duplicates, ambiguous)
        write_alerts(out, self.slug, alerts)
        return alerts

    def summary(self, held: list[dict], alerts: list[dict]) -> dict:
        """The counts every program reports in its run summary."""
        return {"submissions_in_export": len(self.rows),
                "new_submissions_this_run": self.new,
                "processed_this_run": len(self.to_check),
                "changed_after_processing": len(self.changed),
                "held_invalid": len(held), "alerts": len(alerts),
                "integrity_duplicates": len(self.duplicates),
                "non_binding": sum(s == "non_binding" for s in self.statuses.values())}
