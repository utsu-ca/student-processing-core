"""
The command-line plumbing every program shares: the common flags, the config file, the testing mode, the database
backup and the student data (roster) import. A program describes itself with a `ProgramCli` (its own flags, what it
runs, and its testing defaults); `cli_main` / `execute` then run one or several of them:

    cli_main([AGM], "AGM voter RSVPs")                # the `utsu-agm` command
    cli_main([SENATE, SAP, AGM], "all programs")      # the `utsu-all` command
"""
import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from utsu_core.importing_data import import_uoft_data
from utsu_std.database import backup_database, get_connection
from utsu_std.utils import ConfigError, absfile, explicit_flags, load_and_merge_config, setup_logging

logger = logging.getLogger(__name__)

TESTING_DIR = "testing"
TESTING_OUT = TESTING_DIR + "/output"
COMMON_PATH_KEYS = ("import_uoft_data", "db", "log_file", "output_dir", "backup_dir")


@dataclass(frozen=True)
class ProgramCli:
    """
    name              the command and the program's name in messages
    add_arguments     adds the program's own flags to a parser
    run               (cfg, db, standalone) -> processes the program's export; `standalone` is False when it runs
                      together with other programs, which then skip what has no input instead of failing
    testing_defaults  settings `--testing` uses (the fake data's files), unless given on the command line
    path_keys         the program's settings that are paths (relative ones are taken from the project root)
    actions           (cfg, db) -> True when it carried out a one-off action (and nothing else is to run)
    action_flags      the flags that choose such an action: never saved to the config, never run in testing mode
    reset_testing     cfg -> removes what an earlier testing run left that belongs to the old database
    """
    name: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[dict, sqlite3.Connection, bool], None]
    testing_defaults: Mapping[str, str] = field(default_factory=dict)
    path_keys: tuple[str, ...] = ()
    actions: Callable[[dict, sqlite3.Connection], bool] | None = None
    action_flags: tuple[str, ...] = ()
    reset_testing: Callable[[dict], None] | None = None


def ensure_roster(db: sqlite3.Connection, cfg: dict) -> None:
    """
    The student data is imported once: only when the table is missing or empty, or on request with
    --reimport_uoft_data. Every run verifies the whole export against it, so new data takes effect on the next run.
    """
    cursor = db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uoft_data';")
    # a missing or empty table counts as missing: verifying against no students would leave everybody unverified
    present = cursor.fetchone() and cursor.execute("SELECT 1 FROM uoft_data LIMIT 1").fetchone()
    if not (cfg.get('reimport_uoft_data') or not present):
        logger.info("Using the UofT student data already in the database (--reimport_uoft_data replaces it).")
        return
    if not present:
        logger.info("The UofT student data is missing or empty in the database.")
    if not cfg['import_uoft_data']:
        logger.error("Application is missing UofT student data but no import file is specified in the configuration.")
        raise ValueError("Application is missing UofT student data but no import file is specified in the configuration.")
    logger.info("Importing UofT student data from the specified file.")
    uoft_data_path = Path(cfg['import_uoft_data'])
    if not uoft_data_path.exists():
        logger.error(f"File not found: {uoft_data_path}")
        raise FileNotFoundError(f"UofT student data file not found: {uoft_data_path}")
    import_uoft_data(db, uoft_data_path)


def execute(cfg: dict, programs: Sequence[ProgramCli]) -> None:
    """Back up the database, run any one-off action, make sure the roster is there, then run each program."""
    # keep a timestamped copy of the database from before this run changes it
    if not cfg.get('no_backup'):
        backup_database(cfg['db'], cfg.get('backup_dir') or None, int(cfg.get('keep_backups') or 0))
    db = get_connection(cfg['db'])

    for program in programs:
        if program.actions and program.actions(cfg, db):
            return
    ensure_roster(db, cfg)
    for program in programs:
        program.run(cfg, db, len(programs) == 1)


def resolve_paths(cfg: dict, programs: Sequence[ProgramCli]) -> dict:
    """Make every path setting absolute. Relative paths are taken from the project root, not the working directory."""
    for key in (*COMMON_PATH_KEYS, *(k for p in programs for k in p.path_keys)):
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


def apply_testing_mode(args: argparse.Namespace, parser: argparse.ArgumentParser,
                       programs: Sequence[ProgramCli]) -> dict:
    """
    Settings for --testing: generate the fake data, read it, and write the database, logs and results, all in
    testing/output/. config.json is neither read nor written, and a fresh database is built every run.
    Flags given explicitly on the command line still win over these defaults.
    """
    out = TESTING_OUT
    defaults = {"import_uoft_data": f"{out}/fake_uoft_data.csv", "db": f"{out}/test.sqlite",
                "log_file": f"{out}/test.log", "output_dir": out}
    for program in programs:
        defaults.update(program.testing_defaults)
    cfg = vars(args).copy()
    for key, value in defaults.items():
        if cfg[key] == parser.get_default(key):
            cfg[key] = value
    resolve_paths(cfg, programs)
    absfile(out).mkdir(parents=True, exist_ok=True)
    if not any(cfg.get(flag) for p in programs for flag in p.action_flags):  # these read the last run's database
        build_fake_data()
        Path(cfg["db"]).unlink(missing_ok=True)
        for program in programs:
            if program.reset_testing:
                program.reset_testing(cfg)
    return cfg


def build_parser(programs: Sequence[ProgramCli], description: str = "") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--testing", default=False, action="store_true",
                        help="Generate the fake data, run on it, and write everything to testing/output/")
    for program in programs:
        program.add_arguments(parser)
    parser.add_argument("--db", default="secrets/db.sqlite",
                        help="Path to the database file containing student data")
    parser.add_argument("--import_uoft_data", default="secrets/uoft_data.csv",
                        help="Path to the CSV file containing student data to be parsed (imported once, when the "
                             "database has no uoft_data table)")
    parser.add_argument("--reimport_uoft_data", default=False, action="store_true",
                        help="Replace the stored student data with --import_uoft_data (every run verifies the whole "
                             "export against the stored data, so the next run uses the new data)")
    parser.add_argument("--verbosity", default="TRACE", help="Set the logging verbosity level")
    parser.add_argument("--log_file", default="secrets/app.log", help="Path to the log file")
    parser.add_argument("--output_dir", default="secrets/output",
                        help="Directory (relative to the project root) for the results")
    parser.add_argument("--backup_dir", default="",
                        help="Where the timestamped database backups go (default: a backups folder next to the database)")
    parser.add_argument("--keep_backups", default=0, type=int,
                        help="Keep only this many of the newest backups (0 keeps them all)")
    parser.add_argument("--no_backup", default=False, action="store_true",
                        help="Skip the database backup that is made before every run")
    parser.add_argument("--config", default="config.json",
                        help="Path to a JSON config file with settings (relative paths are from the project root)")
    return parser


def cli_main(programs: Sequence[ProgramCli], description: str = "", argv: list[str] | None = None) -> None:
    """The entry point of a command that runs these programs: parse the flags and config, then `execute`."""
    parser = build_parser(programs, description)
    args = parser.parse_args(argv)
    if args.testing:
        cfg = apply_testing_mode(args, parser, programs)
    else:
        # these flags are one-off actions: never saved to the config file
        transient = (*(f for p in programs for f in p.action_flags), "reimport_uoft_data", "no_backup")
        try:
            cfg = resolve_paths(load_and_merge_config(args, parser, transient=transient,
                                                      explicit=explicit_flags(parser, argv)), programs)
        except ConfigError as e:
            sys.exit(f"ERROR: {e}")
    setup_logging(cfg)
    execute(cfg, programs)
