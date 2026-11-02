import ast
import sqlite3
from pathlib import Path

import pytest

from utsu_core.cli import build_parser
from utsu_core.spec import ProgramSpec
from utsu_programs.agm.cli import PROGRAM as AGM
from utsu_programs.agm.program import SPEC as AGM_SPEC
from utsu_programs.runner import PROGRAMS
from utsu_programs.sap.cli import PROGRAM as SAP
from utsu_programs.sap.program import SPEC as SAP_SPEC
from utsu_programs.senate.cli import PROGRAM as SENATE
from utsu_programs.senate.program import SPEC as SENATE_SPEC

SRC = Path(__file__).resolve().parents[1] / "src"
SPECS = [SENATE_SPEC, SAP_SPEC, AGM_SPEC]


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.slug)
def test_a_spec_describes_its_table(spec: ProgramSpec):
    conn = sqlite3.connect(":memory:")
    conn.executescript(spec.schema_text)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (spec.table,)).fetchone()
    if spec.insert:  # what a rebuild saves must fit the table
        row = {c: None for c in ("uuid", "student_uuid", "uoft_uuid", "verification_level", "match_method", "reason",
                                 "submission_ts", "meeting_preference", "contact_ok", "flags", "delegated")}
        conn.execute(spec.insert, {**row, "status": "unverified"})


def test_each_program_has_its_own_flags_and_all_programs_have_them_together():
    flags = lambda programs: {a.dest for a in build_parser(programs)._actions}
    assert {"verify_student", "seats_file", "seed"} <= flags([SENATE]) and "sap_file" not in flags([SENATE])
    assert {"sap_file", "merge_reviews"} <= flags([SAP]) and "agm_file" not in flags([SAP])
    assert {"agm_file", "verify_only"} <= flags([AGM]) and "verify_student" not in flags([AGM])
    assert flags(PROGRAMS) >= flags([SENATE]) | flags([SAP]) | flags([AGM])
    assert {"db", "output_dir", "config", "testing"} <= flags([AGM])  # the common flags come from the core


def imported_packages(package: str) -> set[str]:
    found = set()
    for path in (SRC / package).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("program", ["senate", "sap", "agm"])
def test_a_program_depends_on_the_core_and_not_on_the_other_programs(program):
    others = {"senate", "sap", "agm"} - {program}
    for module in imported_packages(f"utsu_programs/{program}"):
        assert not any(module.startswith(f"utsu_programs.{other}") for other in others), module
        assert not module.startswith("utsu_mail"), module


def test_the_core_does_not_know_the_programs():
    for module in imported_packages("utsu_core"):
        assert not module.startswith(("utsu_programs", "utsu_mail")), module
