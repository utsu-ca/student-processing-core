"""The `utsu-senate` command: verify Senate applications against the student data, then assign the seats."""
import logging
import sqlite3
from pathlib import Path

from utsu_core.cli import TESTING_OUT, ProgramCli, cli_main
from utsu_programs.senate.program import verify_senators
from utsu_programs.senate.seating import run_seating
from utsu_std.utils import absfile

logger = logging.getLogger(__name__)


def add_arguments(parser) -> None:
    parser.add_argument("--verify_student", default="secrets/verify.csv",
                        help="Verify that the provided path to a CSV file matches the stored student data")
    parser.add_argument("--seats_file", default="data/seats_senate.csv",
                        help="CSV of seats per constituency (columns: constituency,division,seats)")
    parser.add_argument("--seed", default=2026, type=int,
                        help="Seed for tie-breaking in seat assignment; the same seed reproduces the same result")


def run(cfg: dict, db: sqlite3.Connection, standalone: bool = True) -> None:
    verify_path = Path(absfile(cfg['verify_student']))
    if not standalone and not verify_path.is_file():
        logger.warning(f"No Senate export at {verify_path}; skipping Senate verification and seating")
        return
    output_dir = cfg.get('output_dir', 'secrets/output')
    verify_senators(cfg['verify_student'], db.cursor(), output_dir=output_dir)

    seats_file = absfile(cfg.get('seats_file', 'data/seats_senate.csv'))
    if seats_file.exists():
        run_seating(Path(absfile(output_dir)) / "verified.csv", seats_file, output_dir, cfg.get('seed', 2026))
    else:
        logger.warning(f"Seats file not found, skipping seating: {seats_file}")


PROGRAM = ProgramCli(
    name="senate", add_arguments=add_arguments, run=run, path_keys=("verify_student", "seats_file"),
    testing_defaults={"verify_student": f"{TESTING_OUT}/fake_senator.csv"})


def main(argv: list[str] | None = None) -> None:
    cli_main([PROGRAM], "Verify Senate applications against the student data and assign the seats", argv)
