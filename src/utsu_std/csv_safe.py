"""
CSV output that is safe to open in a spreadsheet.

Applicants type free text into the forms (names, statements, ...). A cell that starts with = + - @ (or a tab or
carriage return) is run as a formula by Excel and Google Sheets, so every text cell written to a CSV that people
open gets a leading apostrophe, which makes the spreadsheet show it as plain text. Plain numbers are left alone.
`read_cell` undoes it for the files this program reads back.
"""
import csv
import re

TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")


def safe_cell(value):
    """
    >>> safe_cell('=HYPERLINK("http://x")'), safe_cell("-3"), safe_cell(-3), safe_cell("Ann"), safe_cell(None)
    ('\\'=HYPERLINK("http://x")', '-3', -3, 'Ann', None)
    >>> safe_cell("+1 416 555 0100"), safe_cell("\\t=1+1")
    ("'+1 416 555 0100", "'\\t=1+1")
    """
    if isinstance(value, str) and value.startswith(TRIGGERS) and not _NUMBER.match(value):
        return "'" + value
    return value


def read_cell(value: str) -> str:
    """
    Reverse `safe_cell` for a cell this program wrote.

    >>> read_cell("'=1+1"), read_cell("'Ann"), read_cell("Ann")
    ('=1+1', "'Ann", 'Ann')
    """
    return value[1:] if value.startswith("'") and value[1:].startswith(TRIGGERS) else value


class SafeDictWriter(csv.DictWriter):
    def writerow(self, rowdict):
        return super().writerow({k: safe_cell(v) for k, v in rowdict.items()})

    def writerows(self, rowdicts):
        for rowdict in rowdicts:
            self.writerow(rowdict)


class SafeWriter:
    """`csv.writer` that makes every cell safe."""

    def __init__(self, f, **kwargs):
        self._writer = csv.writer(f, **kwargs)

    def writerow(self, row):
        return self._writer.writerow([safe_cell(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)
