import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

from utsu_core.importing_data import import_uoft_data, preprocess_uoft_csv
from utsu_core.integrity import resolve_invalid
from utsu_core.physical import record_physical
from utsu_core.senate import verify_senators
from utsu_core.agm import process_agm
from utsu_core.sap import issue_batches, merge_reviews, process_sap, record_decisions
from utsu_core.seating import run_seating
from utsu_core.verification import bulk_check_for_students
from utsu_std.database import backup_database, get_connection
from utsu_std.utils import absfile, normalize_case, setup_logging, load_and_merge_config
from utsu_std.parsing_utils import get_trunc_id, normalize_name

logger = logging.getLogger(__name__)

def main(cfg):
    # keep a timestamped copy of the database from before this run changes it
    if not cfg.get('no_backup'):
        backup_database(cfg['db'], cfg.get('backup_dir') or None, int(cfg.get('keep_backups') or 0))

    # connect database
    db = get_connection(cfg['db'])

    if cfg.get('merge_reviews'):
        result = merge_reviews(db, output_dir=cfg.get('output_dir', 'secrets/output'))
        print(f"Merged SABP ratings: {result['counts']}")
        return

    if cfg.get('issue_batches'):
        issued = issue_batches(db, cfg['issue_batches'])
        print(f"Marked as handed to reviewers: batches {issued or 'none'}")
        return

    if cfg.get('record_decisions'):
        result = record_decisions(db, cfg['record_decisions'])
        print(f"Recorded {result['recorded']} decision(s); {len(result['problems'])} row(s) skipped")
        for problem in result['problems']:
            print(f"  {problem}")
        return

    if cfg.get('resolve_invalid'):
        ids = [i.strip() for i in str(cfg['resolve_invalid']).split(",") if i.strip()]
        resolved = resolve_invalid(db, ids)
        print(f"Released {len(resolved)} application(s) from hold; the next run takes their current form row as the "
              f"baseline" + (f". Not held or unknown: {sorted(set(ids) - set(resolved))}" if len(resolved) < len(ids) else ""))
        return

    if cfg.get('record_physical'):
        result = record_physical(db, cfg['record_physical'])
        print(f"Recorded {result['recorded']} student(s) as checked in person "
              f"({result['updated']} application row(s) raised to level 3); {len(result['problems'])} row(s) skipped")
        for problem in result['problems']:
            print(f"  {problem}")
        return

    # The student data is imported once: only when the table is missing, or on request with --reimport_uoft_data.
    # Applications are never re-imported; each run verifies just the ones it has not seen before.
    cursor = db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uoft_data';")
    test = cursor.fetchone()
    reimport = bool(cfg.get('reimport_uoft_data'))
    if reimport or not test:
        if not test:
            logger.info("Table 'uoft_data' does not exist in the database.")
        if not cfg['import_uoft_data']:
            logger.error("Application is missing UofT student data but no import file is specified in the configuration.")
            raise ValueError("Application is missing UofT student data but no import file is specified in the configuration.")
        else:
            logger.info("Importing UofT student data from the specified file.")
            # Implement the import logic here, e.g., read the CSV and populate the database
            uoft_data_path = Path(cfg['import_uoft_data'])
            # check existence
            if uoft_data_path.exists():
                import_uoft_data(db, uoft_data_path)
            else:
                logger.error(f"File not found: {uoft_data_path}" )
                quit()
    else:
        logger.info("Using the UofT student data already in the database (--reimport_uoft_data replaces it).")

    # new student data can change earlier outcomes, so everything is verified again after a reimport
    reverify = reimport

    # Implement the main functionality here
    program = cfg.get('program', 'all')
    if program in ('senate', 'all'):
        verify_senators(cfg['verify_student'], db.cursor(),
                        output_dir=cfg.get('output_dir', 'secrets/output'), reverify=reverify)

        seats_file = absfile(cfg.get('seats_file', 'data/seats_senate.csv'))
        if seats_file.exists():
            run_seating(Path(absfile(cfg.get('output_dir', 'secrets/output'))) / "verified.csv", seats_file,
                        cfg.get('output_dir', 'secrets/output'), cfg.get('seed', 2026))
        else:
            logger.warning(f"Seats file not found, skipping seating: {seats_file}")

    if program in ('sap', 'all'):
        if cfg.get('sap_file'):
            process_sap(cfg['sap_file'], db, output_dir=cfg.get('output_dir', 'secrets/output'),
                        batch_size=cfg.get('batch_size', 50), reviewers=cfg.get('reviewers', ''),
                        reverify=reverify)
        elif program == 'sap':
            raise ValueError("No SABP form export given; set --sap_file")

    if program in ('agm', 'all'):
        if cfg.get('agm_file'):
            process_agm(cfg['agm_file'], db, output_dir=cfg.get('output_dir', 'secrets/output'),
                        reverify=reverify)
        elif program == 'agm':
            raise ValueError("No AGM RSVP export given; set --agm_file")
    pass


def run_verification_of_csv(csv_path: str, db_path: str):
    # Implement the verification logic here

    # read the CSV file containing student data

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    with open(csv_path, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        # check header
        expected_headers = {"Student Number", "First Name", "Last Name", "Email"}

        # normalize the header names to a set for comparison
        norm_expected_headers = {normalize_case(field) for field in expected_headers}
        actual_headers = {normalize_case(field) for field in reader.fieldnames} # type: ignore

        if not norm_expected_headers.issubset(actual_headers):
            raise ValueError(f"CSV header does not match expected headers: {norm_expected_headers}, difference: {norm_expected_headers - actual_headers}")
        
        # batch processing of 50 students at a time
        full_names = []
        trunc_ids = []
        for row in reader:
            full_names.append(normalize_name(row['First Name'], row['Last Name']))
            trunc_ids.append(get_trunc_id(row['Student ID']))

            if len(full_names) == 50:
                # Perform verification logic for each batch
                
                # query the database for exact matches
                results = bulk_check_for_students(full_names, trunc_ids, cursor)

                for row in results:
                    print(row)
                full_names = []
                trunc_ids = []
                quit(0)

        # Process any remaining students in the last batch
        if full_names:
            for full_name, trunc_id in zip(full_names, trunc_ids):
                results = bulk_check_for_students([full_name], [trunc_id], cursor)
                for row in results:
                    print(row)

    pass



TESTING_DIR = "testing"
PATH_KEYS = ("verify_student", "sap_file", "agm_file", "import_uoft_data", "db", "log_file", "output_dir", "seats_file",
             "record_decisions", "record_physical", "backup_dir")
# one-off actions that work on the last run's database: never rebuilt in testing mode, never saved to the config
ACTION_FLAGS = ("merge_reviews", "issue_batches", "record_decisions", "record_physical", "resolve_invalid")


def resolve_paths(cfg: dict) -> dict:
    """Make every path setting absolute. Relative paths are taken from the project root, not the working directory."""
    for key in PATH_KEYS:
        if cfg.get(key):
            cfg[key] = str(absfile(cfg[key]))
    return cfg


def build_fake_data() -> None:
    """Write the fake roster and form exports to testing/output/. Seeded, so every run produces the same files."""
    testing = str(absfile(TESTING_DIR))
    if testing not in sys.path:
        sys.path.insert(0, testing)
    from generate_fake_data import generate_all
    generate_all()


def apply_testing_mode(args, parser) -> dict:
    """
    Settings for --testing: generate the fake data, read it, and write the database, logs and results, all in
    testing/output/. config.json is neither read nor written, and a fresh database is built every run.
    Flags given explicitly on the command line still win over these defaults.
    """
    out = TESTING_DIR + "/output"
    defaults = {
        "verify_student": f"{out}/fake_senator.csv",
        "sap_file": f"{out}/fake_sabp.csv",
        "agm_file": f"{out}/fake_agm.csv",
        "reviewers": "Reviewer_A,Reviewer_B,Reviewer_C",
        "import_uoft_data": f"{out}/fake_uoft_data.csv",
        "db": f"{out}/test.sqlite",
        "log_file": f"{out}/test.log",
        "output_dir": out,
    }
    cfg = vars(args).copy()
    for key, value in defaults.items():
        if cfg[key] == parser.get_default(key):
            cfg[key] = value
    resolve_paths(cfg)
    absfile(out).mkdir(parents=True, exist_ok=True)
    if not any(cfg.get(flag) for flag in ACTION_FLAGS):  # these read the database the last run left behind
        build_fake_data()
        Path(cfg["db"]).unlink(missing_ok=True)
    return cfg


# =========================
# CLI Entrypoint
# =========================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="")
    parser.add_argument("--testing", default=False, action="store_true",
                        help="Generate the fake data, run on it, and write everything to testing/output/")
    parser.add_argument("--verify_student", default="secrets/verify.csv",
                        help="Verify that the provided path to a CSV file matches the stored student data")
    parser.add_argument("--program", default="all", choices=["senate", "sap", "agm", "all"],
                        help="Which program to process: Senate applications, Student Aid Bursary (SABP), AGM voter "
                             "RSVPs, or all of them")
    parser.add_argument("--agm_file", default="",
                        help="AGM voter RSVP export (CSV) to verify")
    parser.add_argument("--sap_file", default="",
                        help="SABP form export (CSV) to verify, score and cut into review batches")
    parser.add_argument("--batch_size", default=50, type=int,
                        help="Applications per anonymized SABP review batch")
    parser.add_argument("--reviewers", default="",
                        help="Comma separated committee members (two or more); each SABP batch is assigned to two")
    parser.add_argument("--merge_reviews", default=False, action="store_true",
                        help="Merge the completed reviewer sheets into sap_merged_ratings.csv instead of processing")
    parser.add_argument("--issue_batches", default="",
                        help="Record that SABP batch sheets were handed to reviewers outside the program: 'all' or "
                             "batch numbers like 1,2. Those batches are then never changed or added to")
    parser.add_argument("--record_decisions", default="",
                        help="CSV with uuid, decision and awarded_amount columns: record the final SABP decisions")
    parser.add_argument("--resolve_invalid", default="",
                        help="Comma separated ids of invalid (held) applications that a person has looked at and "
                             "accepts: the hold is lifted and the next run takes their current form row as the baseline")
    parser.add_argument("--record_physical", default="",
                        help="CSV of students checked in person (email, student_number or full_name; optional "
                             "checked_in column): raises their verified applications to verification level 3")
    
    parser.add_argument("--db", default="secrets/db.sqlite",
                            help="Path to the database file containing student data")

    parser.add_argument("--import_uoft_data", default="secrets/uoft_data.csv",
                        help="Path to the CSV file containing student data to be parsed (imported once, when the "
                             "database has no uoft_data table)")
    parser.add_argument("--reimport_uoft_data", default=False, action="store_true",
                        help="Replace the stored student data with --import_uoft_data and verify every application "
                             "again")

    parser.add_argument("--verbosity", default="TRACE",
                        help="Set the logging verbosity level")
    parser.add_argument("--log_file", default="secrets/app.log",
                        help="Path to the log file")
    parser.add_argument("--seats_file", default="data/seats_senate.csv",
                        help="CSV of seats per constituency (columns: constituency,division,seats)")
    parser.add_argument("--seed", default=2026, type=int,
                        help="Seed for tie-breaking in seat assignment; the same seed reproduces the same result")
    parser.add_argument("--output_dir", default="secrets/output",
                        help="Directory (relative to the project root) for verified.csv and unverified.csv")
    parser.add_argument("--backup_dir", default="",
                        help="Where the timestamped database backups go (default: a backups folder next to the database)")
    parser.add_argument("--keep_backups", default=0, type=int,
                        help="Keep only this many of the newest backups (0 keeps them all)")
    parser.add_argument("--no_backup", default=False, action="store_true",
                        help="Skip the database backup that is made before every run")

    parser.add_argument("--config", default="config.json",
                        help="Path to a JSON config file with settings (relative paths are from the project root)")

    args = parser.parse_args()
    if args.testing:
        args = apply_testing_mode(args, parser)
    else:
        # these flags are one-off actions: never saved to the config file
        args = resolve_paths(load_and_merge_config(
            args, parser, transient=(*ACTION_FLAGS, "reimport_uoft_data", "no_backup")))
    setup_logging(args)

    main(args)