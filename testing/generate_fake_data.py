"""
Generate all the fake test data. This is the single entry point; the per-form logic lives in the sibling modules.

    python testing/generate_fake_data.py                          # everything, with the default seeds and sizes
    python testing/generate_fake_data.py --only senate sabp       # just some of them
    python testing/generate_fake_data.py --seed 7 --students 5000 --sabp-rows 4000

The files are written to testing/output/ (not committed). `utsu-all --testing` calls generate_all()
itself, so it only needs running by hand to regenerate with other seeds or sizes.

    students  generate_fake_student_data.py  testing/output/fake_uoft_data.csv (from data/seats_senate.csv)
    senate    generate_fake_senate.py        testing/output/fake_senator.csv
    sabp      generate_fake_sabp.py          testing/output/fake_sabp.csv
    agm       generate_fake_agm.py           testing/output/fake_agm.csv

students runs first because it writes the UofT student file that senate, sabp and agm draw their applicants from;
after that the form generators are independent of each other. The same seed always produces the same files. This
module also holds the code the generators share (names, divisions, timestamps, emails, weighted picks, reading and
writing the student file and CSVs).
"""
import argparse
import csv
import math
import random
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTING = ROOT / "testing"
OUTPUT_DIR = TESTING / "output"
UOFT_DATA = OUTPUT_DIR / "fake_uoft_data.csv"
DEFAULT_SEED = "UTSU-482"
DEFAULT_STUDENTS = 500  # size of the fake_uoft_data.csv roster the forms draw their applicants from
DEFAULT_SABP_ROWS = 500
DEFAULT_ENCODING = "utf-8-sig"

FIRST = ["Amara", "Priya", "Jonas", "Fatima", "Wei", "Sofia", "Tobias", "Nadia", "Kwame", "Hannah", "Maya", "Yuki",
         "Olivia", "Ahmed", "Chloe", "Rohan", "Ines", "Zainab", "Ethan", "Grace", "Lucas", "Aisha", "Mateo", "Elena",
         "Hiro", "Leila", "Samuel", "Anika", "Omar", "Freya", "Diego", "Mei", "Kofi", "Ingrid", "Tariq", "Camille",
         "Nikolai", "Sana", "Isaac", "Layla", "Ravi", "Beatriz", "Jun", "Thandiwe", "Felix", "Noor", "Callum", "Esra",
         "Dmitri", "Bianca", "Arjun", "Selin", "Mohammed", "Katarzyna", "Joon", "Amina", "Pedro", "Ayesha", "Liam",
         "Valentina"]
MIDDLE = ["Marie", "James", "Anne", "Lee", "Rose", "Alexander", "Grace", "Kai", "Elizabeth", "Jin", "Cathrine"]
LAST = ["Okafor", "Nair", "Lindqvist", "Al-Hashimi", "Zhang", "Marquez", "Eriksen", "Rahman", "Mensah", "Cohen",
        "Patel", "Tanaka", "Brennan", "Youssef", "Dubois", "Mehta", "Carvalho", "Ali", "Wright", "Liu", "Santos",
        "Kowalski", "Nguyen", "Haddad", "Singh", "Ivanov", "Abdi", "Park", "Rossi", "Mbeki", "Larsen", "Khan",
        "Fernandez", "Oyelaran", "Petrov", "Sato", "Boucher", "Chaudhry", "Moreau", "Adeyemi", "Kim", "Silva",
        "Hoang", "Reyes", "Novak", "Gunawan", "Baptiste", "Yilmaz", "Ahmadi", "Campbell", "Dhillon", "Papadopoulos",
        "Tremblay", "Vasquez", "Lopez", "Osei", "Hassan", "Fraser", "Choi"]

STUDENT_HEADER = "Last Name,First Name,Truncated Student Number,Faculty,Organization,Division"

DIVISION_NAMES = {
    "INNIS": "Innis College",
    "NEW": "New College",
    "SMC": "St. Michaels College",
    "TRIN": "Trinity College",
    "UC": "University College",
    "VIC": "Victoria College",
    "WDW": "Woodsworth College",
    "ARCLA": "John H. Daniels Faculty of Architecture, Landscape, and Design",
    "FPEH": "Faculty of Kinesiology and Physical Education",
    "MUSIC": "Faculty of Music",
    "APSC": "Faculty of Applied Science and Engineering",
    "DENT": "Faculty of Dentistry",
    "LAW": "Faculty of Law",
    "MED": "Temerty Faculty of Medicine",
    "NURS": "Lawrence S. Bloomberg Faculty of Nursing",
    "PHM": "Leslie Dan Faculty of Pharmacy",
    "TST": "Toronto School of Theology",
    "TYP": "Transitional Year Programme",
    "FIS": "Faculty of Information",
}
COLLEGES = {"INNIS", "NEW", "SMC", "TRIN", "UC", "VIC", "WDW"}  # these divisions sit under ARTSC

# (division, weight): faculty counts from an anonymized export of the real SABP form, with ARTSC split evenly over
# its seven colleges.
DIVISION_WEIGHTS = [("INNIS", 50), ("NEW", 50), ("SMC", 50), ("TRIN", 50), ("UC", 51), ("VIC", 51), ("WDW", 51),
                    ("APSC", 67), ("PHM", 46), ("NURS", 30), ("MED", 29), ("DENT", 20), ("FPEH", 19), ("TST", 17),
                    ("ARCLA", 6), ("FIS", 6), ("TYP", 5), ("MUSIC", 3), ("LAW", 2)]

# Share of Senate and SABP applicants who are not in the student data at all, so they can never verify.
NOT_IN_ROSTER_PERCENT = 2

# Students that already existed in the original fake_uoft_data.csv; the student data keeps them as they were.
ORIGINAL_STUDENTS = [dict(zip(("last", "first", "trunc", "faculty", "organization", "division"), row)) for row in [
    ("Marchegiano", "Erekle Cathrine", "1290", "ARTSC", "", "WDW"),
    ("Heidrich", "Devrim Ségdae", "6416", "PHM", "PHM", ""),
    ("Ó Séaghdha", "Viktoria", "6824", "FPEH", "FPEH", ""),
    ("Černá", "Asako Lea", "1995", "ARTSC", "", "VIC"),
    ("Nardo", "Satu Rayno", "4323", "ARTSC", "", "NEW"),
    ("Jameseson", "Rita", "4123", "ARTSC", "", "UC"),
    ("Florky", "Mykel James The Fourth", "1323", "ARTSC", "", "SMC"),
]]

# Truncated numbers generate_student hands out (all 4-digit numbers but the original students'), and how many
# (first, last) pairs it can make before it has to add a middle name: the first and last names repeat together
# every lcm(len(FIRST), len(LAST)) students.
_FREE_TRUNC = [f"{i:04d}" for i in range(10000) if f"{i:04d}" not in {s["trunc"] for s in ORIGINAL_STUDENTS}]
_NAME_PAIRS = math.lcm(len(FIRST), len(LAST))


class Gen:
    """A seeded random source with weighted helpers."""

    def __init__(self, seed: str | int):
        self.rng = random.Random(seed)

    def pick(self, options):
        """Return one option from (option, relative_weight) pairs."""
        return self.rng.choices(
            [opt    for opt,    _       in options], 
            [rweigh for _,      rweigh  in options]
            )[0]

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def multi(self, options, number: int) -> list[str]:
        """Takes a weighted sample of `number` distinct choices from the provided options pool without replacement."""
        chosen: list[str] = []
        pool = list(options)
        for _ in range(min(number, len(pool))):
            choice = self.pick(pool)
            chosen.append(choice)
            # Remove the just-selected item from the remaining pool so each value is picked at most once.
            # Weights are automatically adjusted as the pool shrinks.
            pool = [(o, w) for o, w in pool if o != choice]
        return chosen


def fmt_ts(dt: datetime) -> str:
    """Timestamp in the form export's style, e.g. 2026/09/16 3:04:05 p.m. AST.

    >>> from datetime import datetime
    >>> fmt_ts(datetime(2026, 9, 16, 13, 4, 5))
    '2026/09/16 1:04:05 p.m. AST'
    """
    return f"{dt:%Y/%m/%d} {dt.hour % 12 or 12}:{dt:%M:%S} {'a' if dt.hour < 12 else 'p'}.m. AST"

SLUG_RE = re.compile(r"[^a-z-]") 
def slug(value: str) -> str:
    """First word of a name, lowercased, accents folded and anything but letters and hyphens dropped."""
    first_word = (value.split() or [""])[0]
    # Normalize to ASCII and lowercase, removing accents.
    first_word = unicodedata.normalize("NFKD", first_word).encode("ascii", "ignore").decode().lower()
    # Remove any characters that are not lowercase letters or hyphens.
    return SLUG_RE.sub("", first_word)


def email_for(first: str, last: str) -> str:
    return f"{slug(first)}.{slug(last)}@mail.utoronto.ca"


def load_students(path: Path = UOFT_DATA) -> list[dict]:
    """Students from a fake_uoft_data.csv-style file: last, first, 4-digit truncated number, faculty code."""
    if not path.exists():
        return []
    students = []
    with open(path, newline="", encoding=DEFAULT_ENCODING) as f:
        # Read each CSV row as a dictionary keyed by the file's header names.
        for row in csv.DictReader(f):
            # Normalize every field by collapsing repeated whitespace and dropping blank header keys.
            clean = {k: " ".join((v or "").split()) for k, v in row.items() if k}
            # Extract only the digits from the truncated student number, removing any non-numeric characters.
            trunc = re.sub(r"\D", "", clean.get("Truncated Student Number", ""))
            # Keep only rows that have both names and a valid 4-digit student number.
            if clean.get("First Name") and clean.get("Last Name") and len(trunc) == 4:
                # Store the student in a normalized dictionary format used elsewhere in the script.
                students.append({"first": clean["First Name"], 
                                 "last": clean["Last Name"], 
                                 "trunc": trunc,
                                 "faculty": clean.get("Faculty", "").upper(),
                                 "organization": clean.get("Organization", ""), 
                                 "division": clean.get("Division", "")})
    return students


def load_seats() -> list[dict]:
    """Rows of data/seats_senate.csv (constituency, division, seats, ...)."""
    with open(ROOT / "data" / "seats_senate.csv", encoding=DEFAULT_ENCODING) as f:
        # Read all non-comment lines from the CSV file and store them in memory.
        file_lines = [line_in for line_in in f if not line_in.startswith("#")]
        # Parse the lines as CSV rows and keep only non-empty rows.
        rows = [row for row in csv.reader(file_lines) if row]
        header = rows.pop(0)
    # Convert the list of rows into a list of dictionaries using the first row as the header.
    return [dict(zip(header, r)) for r in rows]


def division_of(student: dict) -> str:
    """The Senate division a student belongs to: their college, or else their faculty/organization."""
    return student["division"] or student["organization"]


def _middle_names(variant: int) -> list[str]:
    """No middle names for 0, then one for 1..len(MIDDLE), then two, and so on (bijective numeration, so every
    variant is a different sequence)."""
    names = []
    while variant:
        variant -= 1
        names.append(MIDDLE[variant % len(MIDDLE)])
        variant //= len(MIDDLE)
    return names[::-1]


def student_name(n: int) -> tuple[str, str]:
    """(first, last) of the nth generated student. Unique for every n (middle names are added as the first/last
    pairs run out), and the same every run."""
    first = " ".join([FIRST[n * 11 % len(FIRST)], *_middle_names(n // _NAME_PAIRS)])
    return first, LAST[n * 7 % len(LAST)]


def generate_student(n: int, division: str) -> dict:
    """The nth generated student, in `division`. The name is unique for every n, so (name, truncated number) is too
    (that pair is what the pipeline matches on). Numbers never clash with ORIGINAL_STUDENTS and are distinct for the
    first len(_FREE_TRUNC) students; after that they repeat, as real truncated numbers do. Callers only have to keep
    their indices apart (e.g. start after the students that exist)."""
    first, last = student_name(n)
    faculty, org, div = ("ARTSC", "", division) if division in COLLEGES else (division, division, "")
    return {"first": first, "last": last, "trunc": _FREE_TRUNC[n * 7919 % len(_FREE_TRUNC)], "faculty": faculty,
            "organization": org, "division": div}


def student_key(student: dict) -> tuple[str, str, str]:
    """What identifies a student: truncated numbers alone can repeat between students."""
    return student["first"], student["last"], student["trunc"]


def not_in_roster(g: Gen, applicants: int, roster_size: int) -> list[dict]:
    """About NOT_IN_ROSTER_PERCENT of `applicants` (at least one) students who are not in the student data, numbered
    after everyone who is so they never clash, in divisions weighted like the roster's."""
    count = max(1, round(applicants * NOT_IN_ROSTER_PERCENT / 100))
    return [generate_student(roster_size + i, g.pick(DIVISION_WEIGHTS)) for i in range(count)]


def write_students(path: Path, students: list[dict]) -> None:
    """Write students (dicts shaped like load_students returns) as a fake_uoft_data.csv-style file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Manually construct the CSV content rather than using the csv module.
    with open(path, "w", newline="", encoding=DEFAULT_ENCODING) as f:
        f.write(STUDENT_HEADER + "\r\n")
        for s in students:
            f.write(f"{s['last'] + ',':<20} {s['first'] + ',':<28} xxxxx{s['trunc']}x,{s['faculty'] or 'ARTSC'},"
                    f"{s['organization']},{s['division']}\r\n")


def write_csv(path: Path, headers: list[str], rows: list[list], **writer_options) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding=DEFAULT_ENCODING) as f:
        writer = csv.writer(f, **writer_options)
        writer.writerow(headers)
        writer.writerows(rows)


def generate_all(only=None, seed=DEFAULT_SEED, students=DEFAULT_STUDENTS, sabp_rows=DEFAULT_SABP_ROWS,
                 sabp_unmatched=3, sabp_start="2026-09-16", sabp_end="2026-10-09", agm_rows=200) -> None:
    """Write the fake data files to OUTPUT_DIR: all of them, or just those named in `only`."""
    selected = only or ["students", "senate", "sabp", "agm"]

    # The generators import this module for the shared code, and this file's folder is not otherwise importable
    # when called from elsewhere (e.g. `utsu-all --testing`).
    if str(TESTING) not in sys.path:
        sys.path.insert(0, str(TESTING))
    if "students" in selected:
        import generate_fake_student_data
        generate_fake_student_data.generate(size=students)
    if "senate" in selected:
        import generate_fake_senate
        generate_fake_senate.generate()
    if "sabp" in selected:
        import generate_fake_sabp
        generate_fake_sabp.generate(rows=sabp_rows, seed=seed, unmatched=sabp_unmatched,
                                    start=sabp_start, end=sabp_end)
    if "agm" in selected:
        import generate_fake_agm
        generate_fake_agm.generate(rows=agm_rows, seed=seed)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=["students", "senate", "sabp", "agm"],
                        help="Generate only these (default: all). senate, sabp and agm need "
                             "testing/output/fake_uoft_data.csv to draw applicants from, which students writes")
    parser.add_argument("--seed", type=int,
                        help=f"Seed for sabp and agm (default {DEFAULT_SEED}); students and senate are fixed")
    parser.add_argument("--students", type=int, default=DEFAULT_STUDENTS,
                        help=f"Size of the UofT student roster the forms draw applicants from (default "
                             f"{DEFAULT_STUDENTS}); SABP applicants are all drawn from it, so it must be at least "
                             f"--sabp-rows")
    parser.add_argument("--sabp-rows", type=int, default=DEFAULT_SABP_ROWS,
                        help=f"Number of SABP applications (default {DEFAULT_SABP_ROWS})")
    parser.add_argument("--sabp-unmatched", type=float, default=3,
                        help="Percent of SABP applications whose last name is misspelled, so they fail verification "
                             "(default 3)")
    parser.add_argument("--sabp-start", default="2026-09-16", help="First day of the SABP application window")
    parser.add_argument("--sabp-end", default="2026-10-09", help="Last day of the SABP application window")
    parser.add_argument("--agm-rows", type=int, default=200, help="Number of AGM RSVPs, before duplicates (default 200)")
    args = parser.parse_args()

    generate_all(only=args.only, seed=DEFAULT_SEED if args.seed is None else args.seed, students=args.students,
                 sabp_rows=args.sabp_rows, sabp_unmatched=args.sabp_unmatched, sabp_start=args.sabp_start,
                 sabp_end=args.sabp_end, agm_rows=args.agm_rows)


if __name__ == "__main__":
    main()
