import logging
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3

from utsu_std.utils import absfile

logger = logging.getLogger(__name__)

def get_connection(filepath : Path):
    path = absfile(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def split_statements(script: str) -> list[str]:
    """The SQL statements of a script, so they can run inside one transaction (PRAGMAs are left out)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if not buffer.strip().upper().startswith("PRAGMA"):
                statements.append(buffer.strip())
            buffer = ""
    return statements


def replace_table(conn: sqlite3.Connection, table: str, schema_script: str, insert: str, rows: list[dict]) -> None:
    """
    Drop a table, create it again from its schema and insert `rows`, as one transaction: if anything fails the
    previous table is as it was. A table that is rebuilt on every run is never left in the shape of an older version.
    """
    conn.commit()
    try:
        conn.execute("BEGIN")
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in split_statements(schema_script):
            conn.execute(statement)
        conn.executemany(insert, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def backup_database(db_path: Path | str, backup_dir: Path | str | None = None, keep: int = 0) -> Path | None:
    """
    Copy the database to `<backup_dir>/<name>_<YYYYmmdd-HHMMSS>.sqlite` (default folder: `backups/` next to it),
    using SQLite's backup, so the copy is consistent. Returns the copy, or None when there is no database yet.
    With `keep` > 0, only that many of the newest backups of this database are kept.
    """
    source = absfile(db_path)
    if not source.is_file() or source.stat().st_size == 0:
        return None
    folder = absfile(backup_dir) if backup_dir else source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = folder / f"{source.stem}_{stamp}.sqlite", 1
    while target.exists():  # two runs in the same second
        target, n = folder / f"{source.stem}_{stamp}_{n}.sqlite", n + 1
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    logger.info(f"Backed up the database to {target}")

    if keep > 0:
        pattern = re.compile(rf"^{re.escape(source.stem)}_\d{{8}}-\d{{6}}(_\d+)?\.sqlite$")
        old = sorted((p for p in folder.iterdir() if pattern.match(p.name)), key=lambda p: p.name)
        for stale in old[:-keep]:
            stale.unlink()
            logger.info(f"Removed old backup {stale}")
    return target

