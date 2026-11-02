"""The `utsu-agm` command: verify AGM voter RSVPs against the student data and write the attendee lists."""
import sqlite3

from utsu_core.cli import TESTING_OUT, ProgramCli, cli_main
from utsu_programs.agm.program import process_agm, verify_file
from utsu_std.utils import absfile


def add_arguments(parser) -> None:
    parser.add_argument("--agm_file", default="", help="AGM voter RSVP export (CSV) to verify")
    parser.add_argument("--verify_only", default=False, action="store_true",
                        help="Only report the verification (agm_verification.csv); write no lists and no database rows")


def actions(cfg: dict, db: sqlite3.Connection) -> bool:
    if not cfg.get('verify_only'):
        return False
    if not cfg.get('agm_file'):
        raise ValueError("No AGM RSVP export given; set --agm_file")
    rows = verify_file(absfile(cfg['agm_file']), db.cursor(), cfg.get('output_dir', 'secrets/output'))
    verified = sum(r["status"] == "verified" for r in rows)
    print(f"{verified} verified, {len(rows) - verified} unverified of {len(rows)} RSVPs")
    return True


def run(cfg: dict, db: sqlite3.Connection, standalone: bool = True) -> None:
    if cfg.get('agm_file'):
        process_agm(cfg['agm_file'], db, output_dir=cfg.get('output_dir', 'secrets/output'))
    elif standalone:
        raise ValueError("No AGM RSVP export given; set --agm_file")


PROGRAM = ProgramCli(
    name="agm", add_arguments=add_arguments, run=run, actions=actions, path_keys=("agm_file",),
    action_flags=("verify_only",), testing_defaults={"agm_file": f"{TESTING_OUT}/fake_agm.csv"})


def main(argv: list[str] | None = None) -> None:
    cli_main([PROGRAM], "Verify AGM voter RSVPs against the student data and write the attendee lists", argv)
