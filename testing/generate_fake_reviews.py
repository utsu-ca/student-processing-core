"""
Fake reviewed batch sheets (.docx) for testing `python -m utsu_programs.sap.merge_docx`.

    python testing/generate_fake_reviews.py [--out testing/output/reviewer_docx] [--seed 1] [--batches 6]
    python testing/generate_fake_reviews.py --from-sheets testing/output/sap_batches/by_reviewer

Writes <out>/<reviewer>/batchN.docx. The real ones are batch_NNN.txt sheets uploaded to Google Docs, so each .docx
here is just the filled-in sheet text as the document body. Like the real ones, a batch is reviewed by one or two of
the reviewers, the header may name the wrong reviewer, and the scores are messy: ranges such as "2-3", blanks, notes
continued on further lines, and a few invalid values. Scores are random but reproducible from the seed.

--from-sheets fills in the blank batch_NNN.txt sheets that `utsu-sap --reviewers ...` wrote (so the batches match the
database); without it, made-up batches are used.
"""
import argparse
import random
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
REVIEWERS = ("Justin", "David", "Mike")
NOTES = ["Clear need.", "Statement is vague.", "Rent increase documented.", "Mentions lost hours.", "Borderline.",
         "Strong case, dependents.", "Could not verify income claim.", "Similar to the last one."]
INVALID = ["n/a", "high", "6"]
RANGE_DASHES = ["-", "\u2013", " - "]


def sheet_lines(batch: int, size: int, reviewer: str) -> list[str]:
    """A blank sheet laid out like batch_NNN.txt (see utsu_programs.sap.program._write_sheet)."""
    rng = random.Random(f"sheet-{batch}")
    lines = [f"BATCH {batch:03d}  |  reviewer: {reviewer}  |  {size} applications  |  score each 0-5 against the "
             f"General Statement Rubric", ""]
    for record in range(1, size + 1):
        lines += [f"[B{batch:03d}-{record:02d}]  Living-costs Bursary (long-form)  |  requested "
                  f"${rng.choice([500, 650, 1000, 1500])}  |  WFNI {rng.randint(1, 9)}",
                  "Subsidies: Transit Mobility Subsidy (up-to $500/term)", "Statement:",
                  "Rent and food costs have risen faster than my income.", "",
                  "Need level (0-5): ____    Notes:", "-" * 78, ""]
    return lines


def fill_lines(lines: list[str], reviewer: str, seed, p_blank=0.05, p_range=0.15, p_invalid=0.03, p_far=0.1):
    """
    Fill in the need levels of a blank sheet. Returns (new lines, {record_id: expected score or None},
    [record_ids given an invalid value]). The expected score of a range is its midpoint.
    """
    out, expected, invalid, record = [], {}, [], None
    for line in lines:
        if m := re.match(r"\[(B\d+-\d+)\]", line.strip()):
            record = m.group(1)
        if record and re.match(r"Need level\s*\(0-5\)\s*:", line.strip()):
            base = random.Random(f"{seed}-{record}").randint(0, 5)  # what the application is worth, for everyone
            rng = random.Random(f"{seed}-{record}-{reviewer}")
            level = min(5, max(0, base + (rng.choice([-2, 2]) if rng.random() < p_far else rng.choice([-1, 0, 0, 1]))))
            roll = rng.random()
            if roll < p_blank:
                text, expected[record] = "____", None
            elif roll < p_blank + p_invalid:
                text, expected[record] = rng.choice(INVALID), None
                invalid.append(record)
            elif roll < p_blank + p_invalid + p_range:
                low = min(level, 4)
                text, expected[record] = f"{low}{rng.choice(RANGE_DASHES)}{low + 1}", low + 0.5
            else:
                text, expected[record] = rng.choice([f"{level}", f"__{level}__", f" {level} "]), float(level)
            note = rng.choice(NOTES) if rng.random() < 0.5 else ""
            out.append(f"Need level (0-5): {text}    Notes: {note}".rstrip())
            if note and rng.random() < 0.3:
                out.append(rng.choice(NOTES))  # notes continued on the next line
            record = None
        else:
            out.append(line)
    return out, expected, invalid


def write_docx(path: Path, lines: list[str]) -> None:
    """
    What Google Docs makes of a .txt upload: the text as the body, one plain paragraph per line, and nothing else
    (no tables, styles or other structure).
    """
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>' for line in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')


def assign(batches: list[int], reviewers, seed, p_double=0.6) -> dict[int, list[str]]:
    """Each batch goes to two reviewers (probability p_double) or one."""
    rng = random.Random(f"assign-{seed}")
    return {b: rng.sample(list(reviewers), 2 if rng.random() < p_double else 1) for b in batches}


def generate(out: Path | str = OUTPUT_DIR / "reviewer_docx", seed=1, batches: int = 6, size: int = 10,
             reviewers=REVIEWERS, from_sheets: Path | str | None = None, **rates) -> dict:
    """
    Write the reviewer folders. Returns {"expected": {record_id: {reviewer: score}}, "invalid": [(reviewer,
    record_id)], "assignments": {batch: [reviewers]}}; `expected` only holds the scores that should be read back.
    """
    out = Path(out)
    expected: dict[str, dict[str, float]] = {}
    invalid: list[tuple[str, str]] = []
    assignments: dict[int, list[str]] = {}
    if from_sheets:
        sheets = [(int(p.stem.split("_")[1]), p.parent.name, p.read_text(encoding="utf-8-sig").splitlines())
                  for p in sorted(Path(from_sheets).glob("*/batch_*.txt"))]
        for number, reviewer, _ in sheets:
            assignments.setdefault(number, []).append(reviewer)
    else:
        assignments = assign(list(range(1, batches + 1)), reviewers, seed)
        sheets = [(b, r, sheet_lines(b, size, random.Random(f"header-{seed}-{b}-{r}").choice(list(reviewers))))
                  for b, rs in assignments.items() for r in rs]
    for number, reviewer, lines in sheets:
        filled, scores, bad = fill_lines(lines, reviewer, seed, **rates)
        write_docx(out / reviewer / f"batch{number}.docx", filled)
        for record, score in scores.items():
            if score is not None:
                expected.setdefault(record, {})[reviewer] = score
        invalid += [(reviewer, record) for record in bad]
    return {"expected": expected, "invalid": invalid, "assignments": assignments}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(OUTPUT_DIR / "reviewer_docx"))
    parser.add_argument("--seed", default="1")
    parser.add_argument("--batches", type=int, default=6)
    parser.add_argument("--size", type=int, default=10, help="Applications per made-up batch")
    parser.add_argument("--reviewers", default=",".join(REVIEWERS))
    parser.add_argument("--from-sheets", help="folder of <reviewer>/batch_NNN.txt written by utsu-sap")
    args = parser.parse_args(argv)
    result = generate(args.out, args.seed, args.batches, args.size, args.reviewers.split(","), args.from_sheets)
    for batch, reviewers in sorted(result["assignments"].items()):
        print(f"batch {batch}: {', '.join(reviewers)}")
    print(f"Wrote {args.out}; merge with: python -m utsu_programs.sap.merge_docx {args.out} "
          f"-o {Path(args.out).parent / 'merged_reviews.csv'}")


if __name__ == "__main__":
    main()
