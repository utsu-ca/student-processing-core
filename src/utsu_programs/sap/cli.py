"""The `utsu-sap` command: process Student Aid Bursary (SABP) applications, and the committee's one-off actions."""
import logging
import shutil
import sqlite3
from pathlib import Path

from utsu_core.cli import TESTING_OUT, ProgramCli, cli_main
from utsu_programs.sap.program import issue_batches, merge_reviews, process_sap, record_decisions

logger = logging.getLogger(__name__)


def add_arguments(parser) -> None:
    parser.add_argument("--sap_file", default="",
                        help="SABP form export (CSV) to verify, score and cut into review batches")
    parser.add_argument("--batch_size", default=50, type=int,
                        help="Applications per anonymized review batch (batches already handed out never change)")
    parser.add_argument("--reviewers", default="",
                        help="Comma separated committee members (two or more); each SABP batch is assigned to two")
    parser.add_argument("--merge_reviews", default=False, action="store_true",
                        help="Merge the completed reviewer sheets into sap_merged_ratings.csv instead of processing")
    parser.add_argument("--issue_batches", default="",
                        help="Record that SABP batch sheets were handed to reviewers outside the program: 'all' or "
                             "batch numbers like 1,2. Those batches are then never changed or added to")
    parser.add_argument("--record_decisions", default="",
                        help="CSV with uuid, decision and awarded_amount columns: record the final SABP decisions")


def actions(cfg: dict, db: sqlite3.Connection) -> bool:
    """The one-off actions on the committee's work; each one replaces the normal run."""
    if cfg.get('merge_reviews'):
        result = merge_reviews(db, output_dir=cfg.get('output_dir', 'secrets/output'))
        print(f"Merged SABP ratings: {result['counts']}")
        return True
    if cfg.get('issue_batches'):
        issued = issue_batches(db, cfg['issue_batches'])
        print(f"Marked as handed to reviewers: batches {issued or 'none'}")
        return True
    if cfg.get('record_decisions'):
        result = record_decisions(db, cfg['record_decisions'])
        print(f"Recorded {result['recorded']} decision(s); {len(result['problems'])} row(s) skipped")
        for problem in result['problems']:
            print(f"  {problem}")
        return True
    return False


def run(cfg: dict, db: sqlite3.Connection, standalone: bool = True) -> None:
    if cfg.get('sap_file'):
        process_sap(cfg['sap_file'], db, output_dir=cfg.get('output_dir', 'secrets/output'),
                    batch_size=cfg.get('batch_size', 50), reviewers=cfg.get('reviewers', ''))
    elif standalone:
        raise ValueError("No SABP form export given; set --sap_file")


def reset_testing(cfg: dict) -> None:
    shutil.rmtree(Path(cfg["output_dir"]) / "sap_batches", ignore_errors=True)  # sheets of the old database


PROGRAM = ProgramCli(
    name="sap", add_arguments=add_arguments, run=run, actions=actions,
    path_keys=("sap_file", "record_decisions"),
    action_flags=("merge_reviews", "issue_batches", "record_decisions"),
    testing_defaults={"sap_file": f"{TESTING_OUT}/fake_sabp.csv", "reviewers": "Reviewer_A,Reviewer_B,Reviewer_C"},
    reset_testing=reset_testing)


def main(argv: list[str] | None = None) -> None:
    cli_main([PROGRAM], "Process Student Aid Bursary (SABP) applications", argv)
