"""
Merge reviewers' batch .docx sheets into one table, without the database.

    python -m utsu_programs.sap.merge_docx <root> [-o secrets/merged_reviews.csv]

<root>/<reviewer>/**/*.docx: the folder name is the reviewer (a reviewer named in a sheet's header is ignored), and
each record is found by its [B012-01] tag. A range such as "2-3" is resolved to its midpoint (2.5). The suggested
amount is the mean score on the exponential curve fitted to the rubric maximums (program.RUBRIC_CURVE), rounded to
the nearest $10 and never below the rubric amount of the mean score's whole level (the rubric_amount column).
"""
import argparse
import logging
import re
import sys
from pathlib import Path

from utsu_programs.sap.program import DISCUSS_SPREAD, RATING_LEVELS, _read_text_sheet, rubric_amount, suggested_amount
from utsu_std.csv_safe import write_rows

logger = logging.getLogger(__name__)

_NUM = r"\d+(?:\.\d+)?"
_SCORE = re.compile(rf"({_NUM})(?:\s*(?:-|\u2010|\u2011|\u2012|\u2013|\u2014|to)\s*({_NUM}))?", re.I)


def parse_score(raw: str) -> float | None:
    """
    A need level, or the midpoint of a range; None when blank.

    >>> parse_score("2-3"), parse_score(" 4 "), parse_score("1 \u2013 2"), parse_score("")
    (2.5, 4.0, 1.5, None)
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _SCORE.fullmatch(raw)
    if not m:
        raise ValueError(raw)
    low, high = float(m.group(1)), float(m.group(2) or m.group(1))
    if low > high or not (RATING_LEVELS[0] <= low and high <= RATING_LEVELS[-1]):
        raise ValueError(raw)
    return (low + high) / 2


def _fmt(value: float) -> str:
    return f"{value:g}"


def collect(root: Path) -> tuple[dict[str, dict[str, tuple[float, str]]], list[str]]:
    """
    {record_id: {reviewer: (score, notes)}} from every .docx under <root>/<reviewer>/, plus the problems found.
    When a reviewer rated a record in more than one file, the most recently saved file with a score wins.
    """
    ratings: dict[str, dict[str, tuple[float, str]]] = {}
    problems: list[str] = []
    for reviewer_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        reviewer = reviewer_dir.name
        files = sorted((p for p in reviewer_dir.rglob("*.docx") if not p.name.startswith("~$")),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            problems.append(f"{reviewer}: no .docx files")
        for path in files:
            try:
                rows = _read_text_sheet(path)
            except Exception as e:
                problems.append(f"{reviewer}: cannot read {path.name}: {e}")
                continue
            if not rows:
                problems.append(f"{reviewer}: {path.name} has no [B000-00] records")
            for row in rows:
                rid = row["record_id"].upper()
                try:
                    score = parse_score(row["need_level"])
                except ValueError:
                    problems.append(f"{reviewer}: {path.name} {rid} has an invalid need level "
                                    f"{row['need_level']!r}")
                    continue
                if score is None:
                    continue
                previous = ratings.setdefault(rid, {}).get(reviewer)
                if previous and previous[0] != score:
                    problems.append(f"{reviewer}: {rid} rated {_fmt(previous[0])} and {_fmt(score)} in different "
                                    f"files; using {path.name} ({_fmt(score)})")
                ratings[rid][reviewer] = (score, row["notes"])
    return ratings, problems


def merge(ratings: dict[str, dict[str, tuple[float, str]]]) -> tuple[list[dict], list[str]]:
    """One row per record: each reviewer's score, the mean, the spread and a status (agreed | discuss | single)."""
    reviewers = sorted({r for given in ratings.values() for r in given})
    rows = []
    for rid in sorted(ratings, key=lambda r: tuple(int(n) for n in re.findall(r"\d+", r))):
        given = ratings[rid]
        scores = [s for s, _ in given.values()]
        spread = max(scores) - min(scores)
        row = {
            "record_id": rid,
            "batch": int(re.match(r"B(\d+)", rid).group(1)),
            "n_reviewers": len(given),
            "reviewers": "; ".join(sorted(given)),
            **{r: _fmt(given[r][0]) if r in given else "" for r in reviewers},
            "mean_score": _fmt(round(sum(scores) / len(scores), 4)),
            "rubric_amount": rubric_amount(sum(scores) / len(scores)),
            "suggested_amount": suggested_amount(sum(scores) / len(scores)),
            "spread": _fmt(spread),
            "status": "single" if len(given) < 2 else "discuss" if spread >= DISCUSS_SPREAD else "agreed",
            "notes": " | ".join(f"{r}: {n}" for r, (_, n) in sorted(given.items()) if n),
        }
        rows.append(row)
    return rows, reviewers


def merge_folder(root: Path | str, output: Path | str) -> dict:
    root, output = Path(root), Path(output)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a folder")
    ratings, problems = collect(root)
    rows, reviewers = merge(ratings)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["record_id", "batch", "n_reviewers", "reviewers", *reviewers, "mean_score", "rubric_amount", "suggested_amount", "spread", "status", "notes"]
    write_rows(output, fields, rows)
    return {"rows": rows, "problems": problems, "reviewers": reviewers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="folder holding one sub-folder per reviewer, each with their .docx batches")
    parser.add_argument("-o", "--output", default="secrets/merged_reviews.csv")
    args = parser.parse_args(argv)
    result = merge_folder(args.root, args.output)
    rows = result["rows"]
    by_status = {s: sum(r["status"] == s for r in rows) for s in ("agreed", "discuss", "single")}
    print(f"{len(rows)} records from {', '.join(result['reviewers']) or 'no reviewers'} -> {args.output}")
    print(f"  two or more reviewers: {sum(r['n_reviewers'] >= 2 for r in rows)}  " +
          "  ".join(f"{k}: {v}" for k, v in by_status.items()))
    for problem in result["problems"]:
        print(f"  WARNING {problem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
