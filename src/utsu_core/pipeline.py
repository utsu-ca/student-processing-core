"""
The steps Senate, SABP and AGM share when they process a form export, so that all three behave the same way. What a
program is (its form, table and rules) is a `ProgramSpec` (spec.py); this runs it.

Every run reads the full export (see selection.py): nothing is skipped because an earlier run saw it.

    run = Run.start(SPEC, conn, csv_file)       # reads the export and what an earlier run remembered
    run.verify(cursor)                          # status, uoft_uuid, verification_level, match_method on every row
    run.select()                                # the binding application per person
    run.replace_all()                           # or each program saves its own way
    run.settle()                                # saved statuses, duplicate check
    alerts = run.alert(out_dir)                 # alerts_<program>.csv
"""
import logging
import sqlite3
from pathlib import Path

from utsu_core.selection import (OUTCOME_COLUMNS, dedupe_by_uuid, select_current, set_statuses,
                                 warn_unreadable_timestamps)
from utsu_core.integrity import build_alerts, duplicate_verified, write_alerts
from utsu_core.spec import ProgramSpec
from utsu_core.verification import match_students, setup_reader
from utsu_std.database import replace_table
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)


def ensure_table(conn: sqlite3.Connection, spec: ProgramSpec) -> None:
    """Create a program's tables (its schema only creates what is missing)."""
    conn.executescript(spec.schema_text)


def read_rows(spec: ProgramSpec, csv_file: Path | str) -> list[dict]:
    """The rows of a form export: columns renamed by the spec's lookup table, one row per uuid (the last wins)."""
    reader = setup_reader(Path(csv_file), absfile(spec.mapping), optional=spec.optional, identity=spec.identity)
    return dedupe_by_uuid([row for row in map(spec.build_row, reader) if row is not None])


def verify_rows(spec: ProgramSpec, rows: list[dict], cursor: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
    """
    Check rows against uoft_data, by name, student number or both (whatever each one gives). Sets status,
    uoft_uuid, verification_level and match_method on each (and what the spec's `annotate` adds) and returns
    (verified, unverified). Nothing is saved.
    """
    verified, unverified = [], []
    for row, match in zip(rows, match_students(rows, cursor, id_key=spec.id_key)):
        if spec.veto and (override := spec.veto(row)):
            match = override
        row.update(uoft_uuid=match.uoft_uuid, verification_level=match.level, match_method=match.method,
                   status="verified" if match.verified else "unverified")
        spec.annotate(row, match)
        (verified if match.verified else unverified).append(row)
    return verified, unverified


class Run:
    """
    One pass over a full form export for one program.

    Which submission is the binding one is decided the same way everywhere: by the spec's `key` (default: the
    person, by email, else student number, else name) and `rank` (default: the submission timestamp), the newest
    verified one winning.

    `spec.sticky`: an application an earlier run verified is never downgraded. If it no longer matches the student
    data (the data was re-imported) it stays verified, and is raised as an alert (see `keep_verified`). Only
    unverified applications can become verified. Without it the status follows the student data on every run.

    `absent` are applications stored by an earlier run that are not in this export any more (see the SABP program).
    They are not processed, but they compete with the rows of the export for being the binding application.
    """

    def __init__(self, spec: ProgramSpec, conn: sqlite3.Connection, rows: list[dict], absent: list[dict] = ()):
        self.spec, self.conn, self.rows = spec, conn, rows
        self.table, self.label, self.slug = spec.table, spec.label, spec.slug
        self.key, self.rank, self.absent = spec.key, spec.rank, list(absent)
        self.remembered: dict[str, dict] = {}
        if spec.sticky:
            columns = ("uuid", *OUTCOME_COLUMNS)
            self.remembered = {row[0]: dict(zip(columns[1:], row[1:])) for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {self.table} WHERE uoft_uuid IS NOT NULL")}
        self.kept: list[dict] = []
        self.current: list[dict] = []
        self.statuses: dict[str, str] = {}
        self.duplicates: list[tuple] = []

    @classmethod
    def start(cls, spec: ProgramSpec, conn: sqlite3.Connection, csv_file: Path | str) -> "Run":
        """Read the export, make sure the table exists and (for the spec's `prepare`) find the stored rows."""
        rows = read_rows(spec, csv_file)
        if spec.sticky:
            ensure_table(conn, spec)
        absent = spec.prepare(conn, rows) if spec.prepare else []
        return cls(spec, conn, rows, absent)

    def verify(self, cursor: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
        """Verify every row (see `verify_rows`), then keep what an earlier run verified, and log the outcomes."""
        outcome = verify_rows(self.spec, self.rows, cursor)
        if self.spec.sticky:
            self.keep_verified()
        if self.spec.log_outcomes:
            for row in self.rows:
                logger.log(logging.INFO if row["status"] == "verified" else logging.WARNING,
                           f"{row['status'].upper()} {self.label} application {row['uuid']}")
        return outcome

    def keep_verified(self) -> None:
        """Call after verifying: an application that was verified before and now is not stays verified."""
        for app in self.rows:
            stored = self.remembered.get(app["uuid"])
            if stored and app.get("status") == "unverified":
                app.update(stored, status="verified", reason="")
                self.kept.append(app)
                logger.warning(f"{self.label} application {app['uuid']} no longer matches the student data; it was "
                               f"verified before and stays verified.")

    def select(self) -> None:
        """Pick the binding application per key (see selection.select_current)."""
        warn_unreadable_timestamps(self.rows, self.label)
        current, self.statuses = select_current(self.rows + self.absent, self.key, self.rank)
        present = {a["uuid"] for a in self.rows}
        self.current = [a for a in current if a["uuid"] in present]
        mine = [s for u, s in self.statuses.items() if u in present]
        count = lambda status: sum(s == status for s in mine)
        logger.info(f"{len(self.rows)} {self.label} submissions read; {len(self.current)} binding after "
                    f"{count('superseded')} superseded, {count('non_binding')} non-binding and "
                    f"{count('not_considered')} not considered")

    def replace_all(self) -> None:
        """
        Make the table hold exactly the export: it is dropped and created again from its schema (so rows that are no
        longer in the export, and an older table shape, are gone), in one transaction, saving rows with the spec's
        `insert`.
        """
        replace_table(self.conn, self.table, self.spec.schema_text, self.spec.insert, self.rows)

    def settle(self, group_columns: tuple[str, ...] = ("student_uuid",)) -> None:
        """After the rows are saved: record the statuses of stored rows that lost, and check for duplicates."""
        stored_only = {u: s for u, s in self.statuses.items() if u not in {a["uuid"] for a in self.rows}}
        set_statuses(self.conn, self.table, stored_only)
        self.duplicates = duplicate_verified(self.conn, self.table, self.label, group_columns)

    def alert(self, out: Path, ambiguous: list[dict] = (), extra: list[dict] = ()) -> list[dict]:
        """Write alerts_<program>.csv: what a person has to look at. Returns the alerts."""
        kept = [{"severity": "WARNING", "kind": "roster_mismatch", "program": self.label, "uuid": app["uuid"],
                 "student_uuid": app.get("student_uuid", ""),
                 "detail": "no longer matches the student data; it was verified before, so it stays verified"}
                for app in self.kept]
        alerts = build_alerts(self.label, self.rows, self.statuses, self.duplicates, ambiguous, [*kept, *extra])
        write_alerts(out, self.slug, alerts)
        return alerts

    def summary(self, alerts: list[dict]) -> dict:
        """The counts every program reports in its run summary."""
        return {"submissions_in_export": len(self.rows),
                "kept_verified_despite_roster": len(self.kept),
                "alerts": len(alerts),
                "integrity_duplicates": len(self.duplicates),
                "non_binding": sum(s == "non_binding" for s in self.statuses.values())}
