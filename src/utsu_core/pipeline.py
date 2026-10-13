"""
The steps Senate, SABP and AGM share when they process a form export, so that all three behave the same way.

Every run reads the full export (see selection.py): nothing is skipped because an earlier run saw it.

    run = Run(conn, "senate_application", "Senate", "senate", rows)   # reads what an earlier run remembered
    ... verify every row (set status, uoft_uuid, verification_level, match_method) ...
    run.keep_verified()                                               # a verified application is never downgraded
    run.select()                                                      # the binding application per person
    run.replace_all(UPSERT)                                           # or each program saves its own way
    run.settle()                                                      # saved statuses, duplicate check
    alerts = run.alert(out_dir)                                       # alerts_<program>.csv
"""
import logging
import sqlite3
from pathlib import Path
from typing import Callable

from utsu_core.selection import OUTCOME_COLUMNS, person_key, select_current, set_statuses, submission_rank
from utsu_core.integrity import build_alerts, duplicate_verified, write_alerts
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)


def ensure_table(conn: sqlite3.Connection, schema_file: str) -> None:
    """Create a program's tables (the schema file only creates what is missing)."""
    conn.executescript(Path(absfile(schema_file)).read_text(encoding="utf-8-sig"))


class Run:
    """
    One pass over a full form export for one program (`label` for people, `slug` for file names).

    Which submission is the binding one is decided the same way everywhere: by `key` (default: the person, by
    email, else student number, else name) and `rank` (default: the submission timestamp), the newest verified
    one winning.

    `sticky`: an application an earlier run verified is never downgraded. If it no longer matches the student data
    (the data was re-imported) it stays verified, and is raised as an alert (see `keep_verified`). Only unverified
    applications can become verified. Without `sticky` the status follows the student data on every run.

    `absent` are applications stored by an earlier run that are not in this export any more (see sap.py). They are
    not processed, but they compete with the rows of the export for being the binding application.
    """

    def __init__(self, conn: sqlite3.Connection, table: str, label: str, slug: str, rows: list[dict],
                 key: Callable[[dict], str] = person_key, rank: Callable[[dict], object] = submission_rank,
                 sticky: bool = True, absent: list[dict] = ()):
        self.conn, self.table, self.label, self.slug, self.rows = conn, table, label, slug, rows
        self.key, self.rank, self.absent = key, rank, list(absent)
        self.remembered: dict[str, dict] = {}
        if sticky:
            columns = ("uuid", *OUTCOME_COLUMNS)
            self.remembered = {row[0]: dict(zip(columns[1:], row[1:])) for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE uoft_uuid IS NOT NULL")}
        self.kept: list[dict] = []
        self.current: list[dict] = []
        self.statuses: dict[str, str] = {}
        self.duplicates: list[tuple] = []

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
        current, self.statuses = select_current(self.rows + self.absent, self.key, self.rank)
        present = {a["uuid"] for a in self.rows}
        self.current = [a for a in current if a["uuid"] in present]
        mine = [s for u, s in self.statuses.items() if u in present]
        count = lambda status: sum(s == status for s in mine)
        logger.info(f"{len(self.rows)} {self.label} submissions read; {len(self.current)} binding after "
                    f"{count('superseded')} superseded, {count('non_binding')} non-binding and "
                    f"{count('not_considered')} not considered")

    def replace_all(self, upsert: str) -> None:
        """Make the table hold exactly the export: rows that are no longer in it (edited or deleted) are removed."""
        self.conn.execute(f"DELETE FROM {self.table}")
        self.conn.executemany(upsert, self.rows)
        self.conn.commit()

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
