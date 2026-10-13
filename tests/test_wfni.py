import json
import shutil
from pathlib import Path

import pytest

from test_sap import LC_ROW, form_csv, students_db
from utsu_core import sap, wfni
from utsu_std.utils import absfile

DEFAULT = Path(absfile(wfni.WEIGHTS_FILE))


def test_default_weights_match_the_policy_scale():
    w = wfni.current_weights()
    assert (w.max_score, w.high_threshold) == (33, 17)  # the values that used to be hard-coded


# Every option as the form words it, with the points the policy table gives it.
FORM_OPTIONS = [
    ("disability_mobility", "Yes", 2), ("disability_mobility", "No", 0),
    ("disability_invisible", "Yes", 2), ("disability_invisible", "No", 0),
    ("living_situation", "Stably Housed", 0), ("living_situation", "Precariously Housed", 1),
    ("living_situation", "Imminent risk of losing housing", 4), ("living_situation", "Emergency sheltered", 4),
    ("living_situation", "Unsheltered", 6),
    ("disposable_income", "A negative amount (taking on short-term debt to satisfy necessities)", 5),
    ("disposable_income", "Less than $100", 4), ("disposable_income", "$100-200", 3),
    ("disposable_income", "$200-500", 2), ("disposable_income", "$500-1000", 0), ("disposable_income", "$1000+", -3),
    ("liquidity", "$0 to $500", 3), ("liquidity", "$500 to $1000", 1), ("liquidity", "$1000 to $2500", 0),
    ("liquidity", "$2500 to $5000", -1), ("liquidity", "$5000 to $7500", -2),
    ("liquidity", "$7500 to $10,000", -3), ("liquidity", "$10,000+", -5),
    ("dependents", "0", 0), ("dependents", "1", 1), ("dependents", "2", 2), ("dependents", "3", 3),
    ("dependents", "4+", 4), ("external_supports", "Yes", 0), ("external_supports", "No", 1),
]


@pytest.mark.parametrize("field, answer, points", FORM_OPTIONS)
def test_every_form_option_scores_as_the_policy_table_says(field, answer, points):
    assert wfni.points_for(wfni.current_weights().tables[field], answer) == points


@pytest.mark.parametrize("edit, message", [
    (lambda t: t.replace("liquidity,$0 to $500,3", "liquidity,$0 to $500,three"), "whole number"),
    (lambda t: t.replace("dependents,", "kids,"), "unknown field"),
    (lambda t: t.replace("living_situation,Unsheltered,6", "living_situation,Stably Housed,6"), "duplicate"),
    (lambda t: t.replace("circumstances,cap,10\n", ""), "circumstances"),
    (lambda t: t.replace("field,answer,points", "a,b,c"), "header"),
])
def test_bad_weights_are_rejected_with_a_clear_message(edit, message):
    with pytest.raises(wfni.WeightsError, match=message):
        wfni.parse_weights(edit(DEFAULT.read_text(encoding="utf-8")))


def test_thresholds_follow_the_weights():
    text = DEFAULT.read_text(encoding="utf-8").replace("living_situation,Unsheltered,6", "living_situation,Unsheltered,16")
    w = wfni.parse_weights(text)
    assert (w.max_score, w.high_threshold) == (43, 22)


@pytest.fixture
def weights_file(tmp_path, monkeypatch):
    path = tmp_path / "weights.csv"
    shutil.copy(DEFAULT, path)
    monkeypatch.setattr(wfni, "WEIGHTS_FILE", str(path))
    return path


def test_editing_the_weights_file_changes_the_next_run(tmp_path, weights_file):
    conn = students_db(("Ann", "Lee", 3161))
    form = form_csv(tmp_path / "sabp.csv", [LC_ROW])
    out = tmp_path / "out"

    first = sap.process_sap(form, conn, output_dir=out)
    assert first["verified"][0]["wfni"] == 17
    sha = json.loads((out / "sap_run.json").read_text())["wfni_weights_sha256"]

    weights_file.write_text(weights_file.read_text(encoding="utf-8").replace(
        "liquidity,$0 to $500,3", "liquidity,$0 to $500,10"), encoding="utf-8")
    second = sap.process_sap(form, conn, output_dir=out)  # same export: the weights are applied again
    assert second["verified"][0]["wfni"] == 24
    assert conn.execute("SELECT wfni FROM student_aid").fetchone() == (24,)
    assert json.loads((out / "sap_run.json").read_text())["wfni_weights_sha256"] != sha


def test_reworded_option_is_flagged_not_scored_silently(tmp_path, weights_file):
    weights_file.write_text(weights_file.read_text(encoding="utf-8").replace(
        "external_supports,No,1", "external_supports,Nope,1"), encoding="utf-8")
    app = sap.build_application({k.lower(): v for k, v in {
        "living_situation": "Stably Housed", "external_supports": "No"}.items()})
    assert "wfni_incomplete" in app["flags"]
