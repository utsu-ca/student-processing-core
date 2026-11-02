"""
The `utsu-all` command: Senate, SABP and AGM in one run, as the original single program did. A program without an
export to read is skipped with a warning. Each program also has its own command (`utsu-senate`, `utsu-sap`,
`utsu-agm`), which needs only its own flags.
"""
from utsu_core.cli import cli_main, execute
from utsu_programs.agm.cli import PROGRAM as AGM
from utsu_programs.sap.cli import PROGRAM as SAP
from utsu_programs.senate.cli import PROGRAM as SENATE

PROGRAMS = (SENATE, SAP, AGM)


def run(cfg: dict) -> None:
    """Back up, import the student data if needed, and run every program (see `utsu_core.cli.execute`)."""
    execute(cfg, PROGRAMS)


def main(argv: list[str] | None = None) -> None:
    cli_main(PROGRAMS, "Process the Senate, Student Aid Bursary and AGM exports in one run", argv)
