import csv
import re
import sqlite3
import unicodedata
from pathlib import Path

from utsu_std.csv_safe import open_csv, read_cell

_EMAIL = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


def valid_email(address: str) -> bool:
    """
    >>> valid_email("a.b@mail.utoronto.ca")
    True
    >>> valid_email("a@b")
    False
    >>> valid_email("a@b.ca\\nBcc: x@y.ca")
    False
    >>> valid_email("a\\u200b@b.ca")
    False
    >>> valid_email("jos\\u00e9@b.ca")
    True
    """
    address = address or ""
    # an invisible or control character (a zero-width space copied in with the address) is never a real address
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in address):
        return False
    return bool(_EMAIL.match(address))


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a recipient CSV; cells that this program made spreadsheet-safe (see utsu_std.csv_safe) are restored."""
    with open_csv(path) as f:
        return [{k: read_cell((v or "").strip()) for k, v in row.items() if k} for row in csv.DictReader(f)]


def read_sql(db_path: Path, query: str) -> list[dict[str, str]]:
    """
    Run a read-only SELECT against the SQLite database; each column becomes a template field
    (so a query can alias columns to the names a template uses).
    """
    if not re.match(r"^\s*(select|with)\b", query, re.I):
        raise ValueError("Recipient query must be a SELECT statement")
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        cursor = conn.execute(query)
        columns = [d[0] for d in cursor.description]
        return [{c: ("" if v is None else str(v)).strip() for c, v in zip(columns, row)}
                for row in cursor.fetchall()]
    finally:
        conn.close()


def split_by_validity(rows: list[dict[str, str]], email_field: str) -> tuple[list[dict[str, str]], list[tuple[int, str]]]:
    """
    Keep rows with a valid, first-seen email address. Returns (kept, [(row_number, reason), ...]),
    where row_number is 1-based within the source.
    """
    kept, skipped, seen = [], [], set()
    for number, row in enumerate(rows, start=1):
        address = row.get(email_field, "")
        if not valid_email(address):
            skipped.append((number, f"invalid or missing '{email_field}': {address!r}"))
        elif address.lower() in seen:
            skipped.append((number, f"duplicate address {address}"))
        else:
            seen.add(address.lower())
            kept.append(row)
    return kept, skipped
