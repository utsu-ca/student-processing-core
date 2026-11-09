import csv
import sys
import zipfile
from pathlib import Path

import pytest

from utsu_programs.sap import merge_docx
from utsu_programs.sap import program as sap

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))
import generate_fake_reviews as fake  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def write_docx(path, header, records):
    """records: (record_id, level text, notes) in the layout of a batch_NNN.txt opened in a word processor."""
    lines = [f"BATCH 001  |  reviewer: {header}  |  {len(records)} applications", ""]
    for rid, level, notes in records:
        lines += [f"[{rid}]  Stream | requested $100", "Statement:", "text", f"Need level (0-5): {level}    Notes: {notes}",
                  "-" * 78]
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{line}</w:t></w:r></w:p>" for line in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document xmlns:w=\"{W}\"><w:body>{body}</w:body></w:document>")


@pytest.mark.parametrize("raw, expected", [("2-3", 2.5), ("4", 4), (" 1 \u2013 2 ", 1.5), ("2.5", 2.5), ("", None),
                                           ("0", 0), ("____", None)])
def test_parse_score(raw, expected):
    assert merge_docx.parse_score(raw.replace("_", "")) == expected


@pytest.mark.parametrize("raw", ["6", "3-2", "high", "2-7"])
def test_parse_score_rejects(raw):
    with pytest.raises(ValueError):
        merge_docx.parse_score(raw)


def test_folders_are_the_reviewers_and_scores_are_merged(tmp_path):
    root = tmp_path / "batches"
    write_docx(root / "Justin" / "batch1.docx", "Somebody Else", [("B001-01", "2-3", "close"), ("B001-02", "1", "")])
    write_docx(root / "David" / "batch3.docx", "David", [("B003-01", "5", "")])
    write_docx(root / "Mike" / "batch1.docx", "Justin", [("B001-01", "3", "ok"), ("B001-02", "4", "")])
    write_docx(root / "Mike" / "batch3.docx", "Mike", [("B003-01", "2", ""), ("B003-02", "x", "")])

    out = tmp_path / "out" / "merged.csv"
    result = merge_docx.merge_folder(root, out)
    with open(out, newline="", encoding="utf-8-sig") as f:
        rows = {r["record_id"]: r for r in csv.DictReader(f)}

    assert result["reviewers"] == ["David", "Justin", "Mike"]
    one = rows["B001-01"]
    assert (one["Justin"], one["Mike"], one["mean_score"], one["status"]) == ("2.5", "3", "2.75", "agreed")
    assert one["notes"] == "Justin: close | Mike: ok" and one["n_reviewers"] == "2"
    assert rows["B001-02"]["status"] == "discuss"
    assert one["suggested_amount"] == str(sap.suggested_amount(2.75))
    assert one["rubric_amount"] == "300"
    assert (rows["B003-01"]["David"], rows["B003-01"]["Mike"], rows["B003-01"]["status"]) == ("5", "2", "discuss")
    assert "B003-02" not in rows
    assert any("B003-02" in p and "invalid" in p for p in result["problems"])


@pytest.mark.parametrize("seed", range(8))
def test_random_reviews_are_merged_as_generated(tmp_path, seed):
    made = fake.generate(tmp_path / "root", seed=seed, batches=5, size=12)
    result = merge_docx.merge_folder(tmp_path / "root", tmp_path / "merged.csv")
    rows = {r["record_id"]: r for r in result["rows"]}

    assert set(rows) == set(made["expected"])
    for rid, given in made["expected"].items():
        row, scores = rows[rid], list(given.values())
        assert row["n_reviewers"] == len(given) and row["reviewers"] == "; ".join(sorted(given))
        assert {r: float(row[r]) for r in result["reviewers"] if row[r]} == given
        assert float(row["mean_score"]) == pytest.approx(sum(scores) / len(scores), abs=1e-4)
        spread = max(scores) - min(scores)
        assert float(row["spread"]) == spread
        mean = sum(scores) / len(scores)
        assert row["suggested_amount"] == sap.suggested_amount(mean)
        assert row["rubric_amount"] == sap.rubric_amount(mean) <= row["suggested_amount"]
        assert row["status"] == ("single" if len(given) < 2 else "discuss" if spread >= 2 else "agreed")
    assert sum("invalid need level" in p for p in result["problems"]) == len(made["invalid"])


def test_random_reviews_cover_the_messy_cases(tmp_path):
    made = fake.generate(tmp_path / "root", seed=3, batches=8, size=20)
    given = made["expected"].values()
    assert any(s % 1 == 0.5 for g in given for s in g.values())  # ranges
    assert made["invalid"]
    assert {len(g) for g in given} == {1, 2}


def test_suggested_amount_follows_the_rubric():
    assert sap.suggested_amount(0) == 0
    for level, maximum in sap.RUBRIC_AMOUNT.items():
        assert sap.rubric_amount(level) == maximum
        assert abs(sap.curve_amount(level) - maximum) <= 70
        assert sap.suggested_amount(level) >= maximum  # the rubric amount is the minimum
    assert sap.suggested_amount(3) == 650 and sap.suggested_amount(4) == 960  # raised by the curve, rounded to $10
    for x in range(21):
        level = x / 4
        suggested = sap.suggested_amount(level)
        assert suggested >= sap.rubric_amount(level)
        assert suggested % 10 == 0 or suggested == sap.rubric_amount(level)
    steps = [sap.suggested_amount(x / 4) for x in range(21)]
    assert steps == sorted(steps)  # more need never means less money
    assert sap.suggested_amount(-1) == 0 and sap.suggested_amount(9) == sap.suggested_amount(5)


def test_generated_reviews_are_reproducible(tmp_path):
    assert fake.generate(tmp_path / "a", seed=5) == fake.generate(tmp_path / "b", seed=5)


def test_fake_reviews_fill_real_batch_sheets(tmp_path):
    rows = [{"record_id": f"B001-{i:02d}", "uuid": f"u{i}", "stream": "LC", "subsidies": "", "requested_amount": "500",
             "wfni": "3", "emergency": "", "general_statement": "text"} for i in range(1, 5)]
    for reviewer in ("Ann", "Bo"):
        sap._write_sheet(rows, 1, tmp_path / "by_reviewer" / reviewer, reviewer)
    made = fake.generate(tmp_path / "root", seed=2, from_sheets=tmp_path / "by_reviewer")
    assert made["assignments"] == {1: ["Ann", "Bo"]}
    result = merge_docx.merge_folder(tmp_path / "root", tmp_path / "merged.csv")
    assert {r["record_id"] for r in result["rows"]} == set(made["expected"])
