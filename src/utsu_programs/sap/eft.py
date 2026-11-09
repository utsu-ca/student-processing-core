"""Generate credit-only Payments Canada Standard 005 files.

The fixed-width layout follows Standard 005. Bank-specific acceptance and submission
requirements must be confirmed with the financial institution.
"""
import re
from datetime import date
from decimal import Decimal

CONFIG_FIELDS = {
    "originator_id", "file_creation_number", "creation_date", "payment_date",
    "destination_data_center", "originating_data_center", "transaction_code",
    "originator_short_name", "originator_long_name", "return_institution_id",
    "return_account_number", "record_separator",
}
_DIGITS = re.compile(r"[0-9]+\Z")
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?\Z")
_TEXT = re.compile(r"[ -~]+\Z")


def _text(value: object, field: str, width: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > width or not _TEXT.fullmatch(value):
        raise ValueError(f"{field} must be 1-{width} printable ASCII characters")
    return value


def _number(value: object, field: str, width: int) -> str:
    if not isinstance(value, str) or len(value) != width or not _DIGITS.fullmatch(value):
        raise ValueError(f"{field} must be exactly {width} digits")
    return value


def _date(value: object, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid date") from exc
    if not 2000 <= result.year <= 2099:
        raise ValueError(f"{field} must be in the range 2000-2099")
    return result


def _eft_date(value: date) -> str:
    return f"0{value.year % 100:02d}{value.timetuple().tm_yday:03d}"


def _cents(value: str, record: str) -> int:
    value = value.strip()
    if not _AMOUNT.fullmatch(value):
        raise ValueError(f"{record}: approved_amount must be a positive dollar amount with at most two decimals")
    cents = int(Decimal(value) * 100)
    if not 1 <= cents <= 9999999999:
        raise ValueError(f"{record}: approved_amount is outside the Standard 005 range")
    return cents


def create_eft(rows: list[dict[str, str]], config: dict) -> bytes:
    """Return a Standard 005 credit file; reject any incomplete or ambiguous payment."""
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ValueError("EFT config must contain exactly: " + ", ".join(sorted(CONFIG_FIELDS)))
    originator = _text(config["originator_id"], "originator_id", 10)
    if len(originator) != 10:
        raise ValueError("originator_id must be exactly 10 characters")
    sequence = config["file_creation_number"]
    if type(sequence) is not int or not 1 <= sequence <= 9999:
        raise ValueError("file_creation_number must be an integer from 1 to 9999")
    created = _date(config["creation_date"], "creation_date")
    payment_date = _date(config["payment_date"], "payment_date")
    if payment_date < created:
        raise ValueError("payment_date must not precede creation_date")
    destination = _number(config["destination_data_center"], "destination_data_center", 5)
    originating = _number(config["originating_data_center"], "originating_data_center", 5)
    code = _number(config["transaction_code"], "transaction_code", 3)
    short_name = _text(config["originator_short_name"], "originator_short_name", 15)
    long_name = _text(config["originator_long_name"], "originator_long_name", 30)
    return_institution = _number(config["return_institution_id"], "return_institution_id", 9)
    if not return_institution.startswith("0"):
        raise ValueError("return_institution_id must start with 0")
    return_account = _text(config["return_account_number"], "return_account_number", 12)
    separators = {"none": "", "lf": "\n", "crlf": "\r\n"}
    separator = config["record_separator"]
    if not isinstance(separator, str) or separator not in separators:
        raise ValueError("record_separator must be none, lf, or crlf")
    if not rows or len(rows) > 99999999:
        raise ValueError("EFT must contain 1 to 99999999 payments")

    def prefix(kind: str, number: int) -> str:
        return f"{kind}{number:09d}{originator}{sequence:04d}"

    header = (prefix("A", 1) + _eft_date(created) + destination + " " * 20 + "CAD").ljust(1464)
    segments = []
    total = 0
    for index, row in enumerate(rows, 1):
        record = row["record_id"].strip().upper()
        _text(record, "record_id", 19)
        amount = _cents(row.get("approved_amount", ""), record)
        institution = _number(row["institution_number"], f"{record}: institution_number", 3)
        transit = _number(row["transit_number"], f"{record}: transit_number", 5)
        account = row["account_number"]
        if not 1 <= len(account) <= 12 or not _DIGITS.fullmatch(account):
            raise ValueError(f"{record}: account_number must contain 1-12 digits")
        name = _text(f"{row['first_name']} {row['last_name']}".strip(), f"{record}: payee_name", 30)
        trace = f"{destination[:4]}{originating}{sequence:04d}{index:09d}"
        segment = (
            f"{code}{amount:010d}{_eft_date(payment_date)}0{institution}{transit}"
            f"{account:<12}{trace}000{short_name:<15}{name:<30}{long_name:<30}"
            f"{'':10}{record:<19}{return_institution}{return_account:<12}"
            f"{'':15}{'':22}{'':2}{'0' * 11}"
        )
        if len(segment) != 240:
            raise ValueError(f"{record}: invalid EFT transaction length")
        segments.append(segment)
        total += amount
    if total > 99999999999999:
        raise ValueError("EFT total exceeds the 14-digit trailer limit")
    records = [header]
    for start in range(0, len(segments), 6):
        records.append((prefix("C", len(records) + 1) + "".join(segments[start:start + 6])).ljust(1464))
    trailer = (prefix("Z", len(records) + 1) + "0" * 22 + f"{total:014d}{len(rows):08d}" + "0" * 44).ljust(1464)
    records.append(trailer)
    if any(len(record) != 1464 for record in records):
        raise ValueError("invalid EFT record length")
    return separators[separator].join(records).encode("ascii")
