"""
The declarative description of a program (Senate, SABP, AGM, ...): what its form looks like, what table it fills and
how its rows are decided between. The shared engine (pipeline.py) runs any such spec, so a program only supplies the
parts that are really its own (how a form row becomes a row, and what it writes afterwards).
"""
import sqlite3
from dataclasses import dataclass
from typing import Callable

from utsu_core.resources import package_text
from utsu_core.selection import add_flag, person_key, submission_rank
from utsu_core.verification import Match


def flag_fuzzy_match(row: dict, match: Match) -> None:
    """The default `annotate`: a verification on a close rather than exact first name is flagged for review."""
    if match.fuzzy_flag:
        add_flag(row, match.fuzzy_flag)


@dataclass(frozen=True)
class ProgramSpec:
    """
    slug, label        file names (`alerts_<slug>.csv`) and the name people read in logs and alerts
    table              the table the program fills; its schema is `schema_file` in `package`
    mapping            lookup table (project-root relative) that renames the form's columns
    optional, identity form columns that may be missing, and the ones of which at least one must be there
    build_row          a mapped form row -> a row (with uuid and student_uuid), or None to skip it
    insert             the INSERT that saves a row when the table is rebuilt from the export (`Run.replace_all`)
    id_key             the row key that holds the student number the person gave
    sticky             an application verified by an earlier run is never downgraded (see `Run`)
    key, rank          who a row belongs to and which of a person's rows is newest (see `selection.select_current`)
    group_columns      what must be unique among verified rows (see `integrity.duplicate_verified`)
    veto               a row -> a Match that replaces what the roster says, or None (e.g. a bad email)
    annotate           (row, match) -> sets what else a program records about how a row was decided
    prepare            (conn, rows) -> stored rows that are not in the export but still compete (see `Run`)
    log_outcomes       log every row's outcome; a program that logs its own sets this to False
    """
    slug: str
    label: str
    table: str
    package: str
    mapping: str
    build_row: Callable[[dict], dict | None]
    optional: tuple[str, ...] = ()
    identity: tuple[str, ...] = ()
    insert: str = ""
    schema_file: str = "schema.sql"
    id_key: str = "id"
    sticky: bool = True
    key: Callable[[dict], str] = person_key
    rank: Callable[[dict], object] = submission_rank
    group_columns: tuple[str, ...] = ("student_uuid",)
    veto: Callable[[dict], Match | None] | None = None
    annotate: Callable[[dict, Match], None] = flag_fuzzy_match
    prepare: Callable[[sqlite3.Connection, list[dict]], list[dict]] | None = None
    log_outcomes: bool = True

    @property
    def schema_text(self) -> str:
        return package_text(self.package, self.schema_file)
