from utsu_std.utils import clean_text


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


def get_trunc_id(id_txt: str) -> int | None:
    """
    The 4 digits of a student number that the roster keeps, or None when the number is blank or cannot be read.
    Fullwidth and superscript digits and invisible characters are cleaned first.

    >>> get_trunc_id("1006639715"), get_trunc_id(" 1006639715 "), get_trunc_id("xxxxx3971x"), get_trunc_id("3971")
    (3971, 3971, 3971, 3971)
    >>> get_trunc_id("1006639715\\u200b"), get_trunc_id("\\uff11\\uff10\\uff10\\uff16\\uff16\\uff13\\uff19\\uff17\\uff11\\uff15")
    (3971, 3971)
    >>> get_trunc_id(""), get_trunc_id(None), get_trunc_id("12345"), get_trunc_id("12ab")
    (None, None, None, None)
    """
    id_txt = clean_text(id_txt or "").replace(" ", "")
    if len(id_txt) == 4:
        trunc = id_txt
    elif id_txt.endswith("x"):  # the roster's own form, xxxxx3971x
        trunc = id_txt.strip("x")
    elif len(id_txt) > 8:  # a full student number
        trunc = id_txt[-5:-1]
    else:
        return None
    return int(trunc) if len(trunc) == 4 and trunc.isdigit() else None

