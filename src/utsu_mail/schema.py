import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def schema_fields(schema_dirs) -> dict[str, list[str]]:
    """
    Column names per table, read from the `.sql` schema files in these folders (applied to a throwaway
    in-memory database), so templates can be checked against the real data model.
    """
    fields: dict[str, list[str]] = {}
    for sql_file in sorted(f for folder in schema_dirs for f in Path(folder).glob("*.sql")):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(sql_file.read_text(encoding="utf-8-sig"))
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                fields[table] = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        except sqlite3.Error as e:
            logger.warning(f"Could not read schema {sql_file.name}: {e}")
        finally:
            conn.close()
    return fields


def tables_providing(field: str, schema: dict[str, list[str]]) -> list[str]:
    return [table for table, columns in schema.items() if field in columns]
