import csv
from pathlib import Path

from utsu_std.utils import clean_text, normalize_case


def split_multi(value: str) -> list[str]:
    """
    Split a ';' separated multi-select answer.

    >>> split_multi("A; B;;C ")
    ['A', 'B', 'C']
    """
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def division_code(division: str) -> str:
    """
    The code in a form division such as 'TRIN - Trinity College'.

    >>> division_code("TRIN - Trinity College")
    'TRIN'
    >>> division_code(" uc ")
    'UC'
    """
    return division.split(" - ")[0].strip().upper()


def get_uoft_trunc_format(student_number: str) -> str:
    """
    Get the truncated student number in the format 'xxxxx####x'.

    Parameters
    ----------
    student_number : str
        The original student number.

    Returns
    -------
    str
        The truncated student number in UofT format.

    >>> get_uoft_trunc_format("1234567890")
    'xxxxx6789x'
    """
    return "xxxxx" + student_number[-5:-1] + "x"


def get_trunc_id(id_txt: str):
    """
    The 4 digits of a student number that the roster keeps, or None when the number cannot be read. Fullwidth and
    superscript digits and invisible characters are cleaned first.

    >>> get_trunc_id("1006639715"), get_trunc_id(" 1006639715 "), get_trunc_id("xxxxx3971x"), get_trunc_id("3971")
    ('3971', '3971', '3971', '3971')
    >>> get_trunc_id("1006639715\\u200b"), get_trunc_id("\\uff11\\uff10\\uff10\\uff16\\uff16\\uff13\\uff19\\uff17\\uff11\\uff15")
    ('3971', '3971')
    >>> get_trunc_id("") is None, get_trunc_id("12345") is None
    (True, True)
    """
    id_txt = clean_text(id_txt).replace(" ", "")
    if len(id_txt) == 4:
        # string is likely already in the correct format
        return id_txt

    if id_txt[-1:] == "x":
        # str likely needs to be stripped
        return id_txt.strip("x")

    if len(id_txt) > 8:
        trunc = id_txt[-5:-1]
        return trunc

    return None


def strip_uoft_trunc_placeholders(csv_file: Path):
    """
    Strip the placeholder x characters from the truncated student number in the CSV file and save it to a temporary file.

    Parameters
    ----------
    csv_file : Path
        The path to the CSV file.

    Returns
    -------
    Path
        The path to the temporary CSV file with stripped truncated student numbers.

    """
    temp_file = csv_file.with_suffix(".stripped.csv")
    with open(csv_file, newline='') as f_in, open(temp_file, 'w', newline='') as f_out:
        reader = csv.reader(f_in)
        headers = next(reader)

        if "Truncated Student Number" not in headers:
            raise ValueError("CSV file does not contain 'Truncated Student Number' column.")

        trunc_col_indx = headers.index("Truncated Student Number")
        writer = csv.writer(f_out)
        writer.writerow(headers)
        for row in reader:
            row[trunc_col_indx] = row[trunc_col_indx].strip("x")  # Strip the Truncated Student Number column
            writer.writerow(row)
    return temp_file


def normalize_name(first_name: str, last_name: str) -> str:
    """
    Normalize the full name by normalizing the first and last names and combining them.

    Parameters
    ----------
    first_name : str
        The first name of the person.
    last_name : str
        The last name of the person.

    Returns
    -------
    str
        The normalized full name in the format "First Last".

    Example
    -------
    >>> normalize_name("  john  ", "  doe  ")
    'John Doe'
    """
    return f"{normalize_case(first_name)} {normalize_case(last_name)}"
