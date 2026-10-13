"""
Student Aid Bursary Program (SABP) application processor.

Stage 1-3 of the SOP: read the form export, keep each student's newest application, verify them against
uoft_data, score the Weighted Financial Need Index (WFNI), record every application in the student_aid table, and
cut the verified ones into anonymized batches of general statements for the committee to review.
"""
import csv
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from utsu_core.selection import dedupe_by_uuid, parse_submission_ts
from utsu_core.pipeline import Run, ensure_table
from utsu_core.verification import match_students, setup_reader
from utsu_core.wfni import compute_wfni, current_weights
from utsu_std.csv_safe import SafeDictWriter, open_csv, write_rows
from utsu_std.utils import absfile, application_uuid_for, clean_text, normalize_case, student_uuid_for
from utsu_std.parsing_utils import split_multi

logger = logging.getLogger(__name__)

LC, AP = "LC", "AP"
OPTIONAL_FIELDS = ("id", "first_name", "last_name")
IDENTITY_FIELDS = ("first_name", "last_name")  # the form needs a name column; the student number is optional
STREAM_NAMES = {LC: "Living-costs Bursary", AP: "Academic Pursuits Bursary"}
# Largest request per term: LABs are $1,500 plus up to $1,000 of Emergency Bursary; APRs are $1,500.
MAX_REQUEST = {LC: 2500, AP: 1500}
INSTANT_APPROVAL_BELOW = 150

MASTER_FIELDS = ["uuid", "status", "student_uuid", "uoft_uuid", "verification_level", "match_method",
                 "submission_ts",
                 "terms", "id", "full_name", "email", "faculty",
                 "stream", "subsidies", "requested_amount", "wfni", "emergency", "flags", "batch", "record"]
REVIEW_FIELDS = ["record_id", "uuid", "stream", "subsidies", "requested_amount", "wfni", "emergency",
                 "general_statement", "need_level", "notes"]

# The WFNI answer weights live in data/wfni_weights.csv; see utsu_core/wfni.py.


def parse_amount(value: str) -> float | None:
    """
    >>> parse_amount("$1,500")
    1500.0
    >>> parse_amount("706.77")
    706.77
    >>> parse_amount("abc") is None
    True
    """
    try:
        return float(re.sub(r"[$,\s]", "", value or ""))
    except ValueError:
        return None


def redact(text: str, names: list[str] = ()) -> str:
    """
    Remove contact details, student numbers, postal codes, links and the applicant's own name from free text.
    A name is found however it is spelled (`Jos\u00e9` is the name `Jose`), and short names such as `Li` count too.

    >>> redact("Hi, I'm Ann Lee (ann@mail.utoronto.ca, 416-555-0100, 1001231617). See https://x.co/a", ["Ann", "Lee"])
    "Hi, I'm [name] [name] ([email], [phone], [student number]). See [link]"
    >>> redact("Li and Jos\\u00e9 O'Neil-Wu agree", ["Li Wu", "Jose O'Neil"])
    "[name] and [name] O'[name]-[name] agree"
    """
    text = re.sub(r"https?://\S+", "[link]", text or "")
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[email]", text)
    text = re.sub(r"(?<!\d)\d{9,10}(?!\d)", "[student number]", text)
    text = re.sub(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)", "[phone]", text)
    text = re.sub(r"\b[A-Za-z]\d[A-Za-z][\s-]?\d[A-Za-z]\d\b", "[postal code]", text)
    words = {clean_text(w).casefold() for part in names for w in re.split(r"[\s'-]+", part)}
    words = {w for w in words if len(w) >= 2}
    return re.sub(r"\w+", lambda m: "[name]" if clean_text(m.group(0)).casefold() in words else m.group(0), text)


def sap_uuid(app: dict[str, str]) -> str:
    """Internal id for an application, so reprocessing the same form yields the same uuid."""
    return application_uuid_for("sap", app.get("submission_ts", ""), app.get("email", ""), app.get("id", ""),
                                app.get("full_name", ""))


def build_application(row: dict[str, str]) -> dict:
    """Turn one mapped form row into an application: cleaned fields, stream, amount, WFNI and review flags."""
    app = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}
    app["full_name"] = normalize_case(f"{app.get('first_name', '')} {app.get('last_name', '')}")
    bursary = app.get("bursary_type", "").lower()
    app["stream"] = LC if bursary.startswith("living") else AP if bursary.startswith("academic") else ""
    prefix = app["stream"].lower()
    app["subsidies"] = "; ".join(split_multi(app.get(f"{prefix}_subsidies", "")))
    app["statement"] = app.get(f"{prefix}_statement", "")
    amount = parse_amount(app.get(f"{prefix}_amount", ""))
    app["requested_amount"] = "" if amount is None else f"{amount:g}"
    app["emergency"] = int(any("emergency bursary" in s.lower() for s in split_multi(app.get("lc_subsidies", ""))))
    app["faculty"] = app.get("faculty", "").split(" - ")[0].strip()
    app["uuid"] = sap_uuid(app)
    app["student_uuid"] = student_uuid_for(app.get("email", ""), app.get("id", ""), app["full_name"])
    weights = current_weights()
    wfni, unknown = compute_wfni(app, weights)
    app["wfni"] = "" if wfni is None else wfni
    flags = []
    if app.get("member_confirmed", "").lower() != "yes":
        flags.append("membership_not_confirmed")
    if not app["stream"]:
        flags.append("unknown_bursary_type")
    if amount is None:
        flags.append("no_amount")
    elif app["stream"] in MAX_REQUEST and amount > MAX_REQUEST[app["stream"]]:
        flags.append("amount_over_max")
    elif amount < INSTANT_APPROVAL_BELOW:
        flags.append("instant_approval_eligible")
    if not app["statement"]:
        flags.append("no_statement")
    if unknown:
        flags.append("wfni_incomplete")
    if wfni is not None and wfni >= weights.high_threshold:
        flags.append("high_wfni")
    app["flags"] = ";".join(flags)
    return app


def read_applications(csv_file: Path) -> list[dict]:
    mapping = absfile("data/conversion_table_sap.csv")
    reader = setup_reader(csv_file, mapping, optional=OPTIONAL_FIELDS, identity=IDENTITY_FIELDS)
    applications = [build_application(row) for row in reader]
    return dedupe_by_uuid([a for a in applications if a.get("id") or a["full_name"]])


def verify_applications(applications: list[dict], cursor: sqlite3.Cursor) -> None:
    """
    Check applications against uoft_data by name, student number or both (whatever each one gives) and set
    status, uoft_uuid, verification_level and match_method on each.
    """
    for app, match in zip(applications, match_students(applications, cursor)):
        app.update(uoft_uuid=match.uoft_uuid, verification_level=match.level, match_method=match.method,
                   status="verified" if match.verified else "unverified")


def assign_batches(verified: list[dict], batch_size: int = 50, placed: dict[str, tuple[int, int]] | None = None,
                   closed=frozenset()) -> list[list[dict]]:
    """
    Cut verified applications into batches of at most `batch_size`, oldest submission first. Sets each
    application's batch and record.

    Batches already handed out stay as they are when new applications arrive: `placed` maps uuid -> (batch, record)
    for applications that already have a slot (a slot is never reused, even if its application was superseded),
    and new applications go to the last batch only if it has room and is not in `closed` (batches assigned to
    reviewers); otherwise they start a new batch.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    placed = placed or {}
    used: dict[int, int] = {}
    for batch, record in placed.values():
        used[batch] = max(used.get(batch, 0), record)
    last = max(used, default=0)

    ordered = sorted(verified, key=lambda a: (parse_submission_ts(a.get("submission_ts", "")) or datetime.min,
                                              a["uuid"]))
    for app in ordered:
        if app["uuid"] in placed:
            app["batch"], app["record"] = placed[app["uuid"]]
            continue
        if last == 0 or last in closed or used.get(last, 0) >= batch_size:
            last += 1
        used[last] = used.get(last, 0) + 1
        app["batch"], app["record"] = last, used[last]

    batches: list[list[dict]] = [[] for _ in range(max((a["batch"] for a in ordered), default=0))]
    for app in ordered:
        batches[app["batch"] - 1].append(app)
    for batch in batches:
        batch.sort(key=lambda a: a["record"])
    return batches


def review_row(app: dict) -> dict:
    """The anonymized view the committee sees: no name, student number, contact details or faculty."""
    names = [app.get("first_name", ""), app.get("last_name", "")]
    return {
        "record_id": f"B{app['batch']:03d}-{app['record']:02d}",
        "uuid": app["uuid"],
        "stream": STREAM_NAMES.get(app["stream"], ""),
        "subsidies": redact(app["subsidies"], names),
        "requested_amount": app["requested_amount"],
        "wfni": app["wfni"],
        "emergency": "yes" if app["emergency"] else "",
        "general_statement": redact(app["statement"], names) or "(no statement provided)",
        "need_level": "",
        "notes": "",
    }


REVIEWERS_PER_BATCH = 2
RATING_LEVELS = range(6)
# General Statement Rubric: recommended maximum per need level (SAS-002).
RUBRIC_AMOUNT = {0: 0, 1: 125, 2: 300, 3: 650, 4: 900, 5: 1500}
# Two reviewers whose levels are this far apart (or more) must discuss the application.
DISCUSS_SPREAD = 2
MERGED_FIELDS = ["uuid", "record_id", "stream", "requested_amount", "wfni", "emergency", "reviewers", "ratings",
                 "n_ratings", "mean_level", "spread", "suggested_amount", "status", "notes"]


def reviewer_id(name: str) -> str:
    """
    A reviewer's name as used in folder names and the database.

    >>> reviewer_id(" Ann  Lee ")
    'Ann_Lee'
    """
    return re.sub(r"[^\w.-]+", "_", name.strip()).strip("_")


def parse_reviewers(value) -> list[str]:
    """
    Reviewer names from a comma separated string or a list, without blanks or duplicates.

    >>> parse_reviewers("Ann, Bo,,Ann")
    ['Ann', 'Bo']
    """
    parts = value.split(",") if isinstance(value, str) else (value or [])
    return list(dict.fromkeys(r for r in (reviewer_id(str(p)) for p in parts) if r))


STAGES = ("scored", "in_review", "discuss", "reconciled", "decided")
ISSUED = STAGES[1:]  # the stages after a batch has been handed to reviewers
# An application is complete once the committee's decision is recorded: verification, the reviewers' need levels,
# their reconciliation and the decision are all done, and it is ready for disbursement (which this program does not
# track). A student's complete application for a term is final: no other application of theirs for that term is
# considered (see process_sap).
COMPLETE_STAGE = "decided"


def ensure_schema(conn: sqlite3.Connection):
    ensure_table(conn, "src/schema/student_aid.sql")
    if "stage" not in {row[1] for row in conn.execute("PRAGMA table_info(student_aid)")}:
        # a database from before stage tracking: work out where each application already is
        conn.execute("ALTER TABLE student_aid ADD COLUMN stage TEXT")
        conn.execute("""UPDATE student_aid SET stage = CASE
                            WHEN decision IS NOT NULL THEN 'decided'
                            WHEN batch IN (SELECT batch FROM sap_batch_reviewer) THEN 'in_review'
                            WHEN status = 'verified' THEN 'scored' END""")
        conn.commit()


def mark_in_review(conn: sqlite3.Connection, batches) -> int:
    """Move the scored applications of these batches to in_review (their sheets went to reviewers). Returns how many."""
    batches = sorted(set(batches))
    if not batches:
        return 0
    cursor = conn.execute("UPDATE student_aid SET stage = 'in_review' WHERE stage = 'scored' AND status = 'verified' "
                          f"AND batch IN ({','.join('?' * len(batches))})", batches)
    conn.commit()
    return cursor.rowcount


def issue_batches(conn: sqlite3.Connection, which: str) -> list[int]:
    """
    Record that batch sheets were handed to reviewers outside the program: `which` is 'all' (every batch not yet
    issued) or batch numbers such as '1,2'. Those batches are then never added to or rewritten. Returns the
    batches that were marked.
    """
    ensure_schema(conn)
    pending = {b for (b,) in conn.execute("SELECT DISTINCT batch FROM student_aid WHERE stage = 'scored' "
                                          "AND batch IS NOT NULL")}
    if which.strip().lower() == "all":
        wanted = pending
    else:
        try:
            wanted = {int(part) for part in which.split(",") if part.strip()}
        except ValueError:
            raise ValueError(f"--issue_batches takes 'all' or batch numbers like 1,2 (got {which!r})")
        for batch in sorted(wanted - pending):
            logger.warning(f"Batch {batch} has nothing to issue (unknown, or already issued)")
    issued = sorted(wanted & pending)
    mark_in_review(conn, issued)
    logger.info(f"Marked {len(issued)} batch(es) as handed to reviewers: {issued}")
    return issued


def record_decisions(conn: sqlite3.Connection, path: Path | str) -> dict:
    """
    Record final decisions from a CSV with `uuid`, `decision` and (optionally) `awarded_amount` columns; other
    columns are ignored, so the sap_merged_ratings.csv can be filled in and used. Each application recorded
    moves to the `decided` stage. Rows that cannot be recorded are reported in `problems`, not applied.
    """
    ensure_schema(conn)
    recorded, problems = 0, []
    with open_csv(path) as f:
        reader = csv.DictReader(f)
        missing = {"uuid", "decision"} - {(h or "").strip() for h in reader.fieldnames or []}
        if missing:
            raise ValueError(f"{path} needs the columns uuid and decision (missing: {', '.join(sorted(missing))})")
        for line, raw in enumerate(reader, start=2):
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            uuid, decision, amount_text = row["uuid"], row["decision"], row.get("awarded_amount", "")
            if not uuid and not decision:
                continue
            found = conn.execute("SELECT status, stage FROM student_aid WHERE uuid = ?", (uuid,)).fetchone()
            amount = parse_amount(amount_text) if amount_text else None
            if not found:
                problems.append(f"line {line}: unknown application {uuid!r}")
            elif found[0] != "verified":
                problems.append(f"line {line}: {uuid} is {found[0]}, not verified")
            elif not decision:
                problems.append(f"line {line}: {uuid} has no decision")
            elif amount_text and (amount is None or amount < 0):
                problems.append(f"line {line}: {uuid} has an invalid awarded_amount {amount_text!r}")
            else:
                if found[1] in (None, "scored", "in_review"):
                    logger.warning(f"{uuid} was decided before its reviews were reconciled (stage {found[1]})")
                conn.execute("UPDATE student_aid SET decision = ?, awarded_amount = ?, stage = 'decided' "
                             "WHERE uuid = ?", (decision, amount, uuid))
                recorded += 1
    conn.commit()
    for problem in problems:
        logger.warning(problem)
    logger.info(f"Recorded {recorded} decision(s); {len(problems)} row(s) skipped")
    return {"recorded": recorded, "problems": problems}


def assign_reviewers(conn: sqlite3.Connection, n_batches: int, reviewers: list[str]) -> dict[int, list[str]]:
    """
    Two reviewers per batch, saved in sap_batch_reviewer. Batches that already have reviewers keep them; new
    batches go to the two reviewers with the fewest batches so far (ties in the order given), so the workload
    stays even.
    """
    if len(reviewers) < REVIEWERS_PER_BATCH:
        raise ValueError(f"Need at least {REVIEWERS_PER_BATCH} reviewers, got {len(reviewers)}: {reviewers}")
    ensure_schema(conn)
    assigned: dict[int, list[str]] = {}
    for batch, reviewer in conn.execute("SELECT batch, reviewer FROM sap_batch_reviewer ORDER BY batch, reviewer"):
        assigned.setdefault(batch, []).append(reviewer)
    load = {r: sum(r in rs for rs in assigned.values()) for r in reviewers}
    for batch in range(1, n_batches + 1):
        if batch in assigned:
            continue
        picked = sorted(reviewers, key=lambda r: (load[r], reviewers.index(r)))[:REVIEWERS_PER_BATCH]
        for r in picked:
            load[r] += 1
            conn.execute("INSERT INTO sap_batch_reviewer (batch, reviewer) VALUES (?, ?)", (batch, r))
        assigned[batch] = sorted(picked)
    conn.commit()
    return {b: rs for b, rs in assigned.items() if b <= n_batches}


def _write_sheet(rows: list[dict], number: int, directory: Path, reviewer: str | None = None):
    """batch_NNN.csv (to fill in) and batch_NNN.txt (to read) for one batch."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"batch_{number:03d}.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = SafeDictWriter(f, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    title = f"BATCH {number:03d}" + (f"  |  reviewer: {reviewer}" if reviewer else "")
    lines = [f"{title}  |  {len(rows)} applications  |  score each 0-5 against the General Statement Rubric", ""]
    for r in rows:
        lines += [f"[{r['record_id']}]  {r['stream']}  |  requested ${r['requested_amount']}  |  WFNI "
                  f"{r['wfni'] if r['wfni'] != '' else 'n/a'}" + ("  |  EMERGENCY" if r["emergency"] else ""),
                  f"Subsidies: {r['subsidies'] or 'n/a'}", "Statement:", r["general_statement"], "",
                  "Need level (0-5): ____    Notes:", "-" * 78, ""]
    (directory / f"batch_{number:03d}.txt").write_text("\n".join(lines), encoding="utf-8")


def _sheet_uuids(path: Path) -> set[str]:
    with open_csv(path) as f:
        return {row.get("uuid", "") for row in csv.DictReader(f)}


def write_batches(batches: list[list[dict]], batch_dir: Path, assignments: dict[int, list[str]] | None = None,
                  closed=frozenset()):
    """
    Write blank batch_NNN.{csv,txt} per batch (replacing older ones) and, for assigned reviewers, a copy in
    by_reviewer/<reviewer>/. Batches in `closed` were already handed to reviewers: their blank sheets are left
    exactly as issued. A reviewer's existing sheet is never overwritten, since it may hold their ratings.
    """
    batch_dir.mkdir(parents=True, exist_ok=True)
    for old in (*batch_dir.glob("batch_*.csv"), *batch_dir.glob("batch_*.txt")):
        if int(old.stem.split("_")[1]) not in closed:
            old.unlink()
    for number, batch in enumerate(batches, start=1):
        if not batch:  # every application in it was superseded
            continue
        rows = [review_row(a) for a in batch]
        if number not in closed:
            _write_sheet(rows, number, batch_dir)
        for reviewer in (assignments or {}).get(number, []):
            folder = batch_dir / "by_reviewer" / reviewer
            existing = folder / f"batch_{number:03d}.csv"
            if not existing.exists():
                _write_sheet(rows, number, folder, reviewer)
            elif _sheet_uuids(existing) != {r["uuid"] for r in rows}:
                logger.warning(f"{existing} no longer matches batch {number} (applications were added or "
                               f"changed); left untouched")
    logger.info(f"Wrote {len(batches)} review batch(es) to {batch_dir}")


def merge_reviews(conn: sqlite3.Connection, output_dir: Path | str = "secrets/output") -> dict:
    """
    Merge the completed reviewer sheets in <output_dir>/sap_batches/by_reviewer/*/ into sap_merged_ratings.csv and
    the sap_review table. Per application: who rated it, each level, the mean, the spread, a suggested amount
    (the mean of the rubric maximums, capped at the requested amount) and a status:
    agreed | discuss (levels differ by 2 or more) | incomplete (one rating) | unrated.
    """
    out = Path(absfile(output_dir))
    batch_dir = out / "sap_batches"
    with open_csv(out / "sap_verified.csv") as f:
        applications = {row["uuid"]: row for row in csv.DictReader(f)}
    ensure_schema(conn)

    ratings: dict[str, dict[str, tuple[int, str]]] = {}
    cleared: list[tuple[str, str]] = []  # (uuid, reviewer) whose sheet now has no rating for it
    problems: list[str] = []
    for sheet in sorted(batch_dir.glob("by_reviewer/*/batch_*.csv")):
        reviewer = sheet.parent.name
        with open_csv(sheet) as f:
            for row in csv.DictReader(f):
                uuid, raw = row.get("uuid", ""), (row.get("need_level") or "").strip()
                if uuid not in applications:
                    problems.append(f"{reviewer}: {sheet.name} has an unknown application {uuid!r}")
                elif raw:
                    try:
                        level = int(float(raw))
                        if level not in RATING_LEVELS or level != float(raw):
                            raise ValueError
                    except (ValueError, OverflowError):
                        problems.append(f"{reviewer}: {row.get('record_id')} has an invalid need level {raw!r}")
                        continue
                    ratings.setdefault(uuid, {})[reviewer] = (level, (row.get("notes") or "").strip())
                else:
                    cleared.append((uuid, reviewer))

    assigned: dict[int, list[str]] = {}
    for batch, reviewer in conn.execute("SELECT batch, reviewer FROM sap_batch_reviewer"):
        assigned.setdefault(batch, []).append(reviewer)

    rows = []
    for uuid, app in sorted(applications.items(), key=lambda kv: (int(kv[1]["batch"]), int(kv[1]["record"]))):
        given = ratings.get(uuid, {})
        levels = [level for level, _ in given.values()]
        spread = max(levels) - min(levels) if levels else ""
        mean = sum(levels) / len(levels) if levels else ""
        suggested = ""
        if levels:
            suggested = sum(RUBRIC_AMOUNT[v] for v in levels) / len(levels)
            if app["requested_amount"]:
                suggested = min(suggested, float(app["requested_amount"]))
        status = ("unrated" if not levels else "incomplete" if len(levels) < REVIEWERS_PER_BATCH
                  else "discuss" if spread >= DISCUSS_SPREAD else "agreed")
        rows.append({
            "uuid": uuid, "record_id": f"B{int(app['batch']):03d}-{int(app['record']):02d}", "stream": app["stream"],
            "requested_amount": app["requested_amount"], "wfni": app["wfni"], "emergency": app["emergency"],
            "reviewers": "; ".join(sorted(assigned.get(int(app["batch"]), given))),
            "ratings": "; ".join(f"{r}={level}" for r, (level, _) in sorted(given.items())),
            "n_ratings": len(levels), "mean_level": "" if mean == "" else f"{mean:g}", "spread": spread,
            "suggested_amount": "" if suggested == "" else f"{suggested:g}", "status": status,
            "notes": " | ".join(f"{r}: {note}" for r, (_, note) in sorted(given.items()) if note)})
    with open(out / "sap_merged_ratings.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = SafeDictWriter(f, fieldnames=MERGED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    conn.executemany("DELETE FROM sap_review WHERE uuid = ? AND reviewer = ?", cleared)  # a rating that was taken back
    conn.executemany("INSERT OR REPLACE INTO sap_review (uuid, reviewer, need_level, notes) VALUES (?, ?, ?, ?)",
                     [(u, r, level, note) for u, given in ratings.items() for r, (level, note) in given.items()])
    # reviewed applications move on: agreed -> reconciled, far apart -> discuss, anything else stays in_review.
    # Decided applications, and ones with no rating yet, are left alone.
    stage_of = {"agreed": "reconciled", "discuss": "discuss"}
    conn.executemany("UPDATE student_aid SET stage = ? WHERE uuid = ? "
                     "AND stage IN ('scored', 'in_review', 'discuss', 'reconciled')",
                     [(stage_of.get(r["status"], "in_review"), r["uuid"]) for r in rows if r["n_ratings"]])
    conn.commit()

    counts = {s: sum(r["status"] == s for r in rows) for s in ("agreed", "discuss", "incomplete", "unrated")}
    for problem in problems:
        logger.warning(problem)
    logger.info(f"Merged ratings for {len(rows)} applications: {counts}")
    return {"rows": rows, "counts": counts, "problems": problems}


_UPSERT = """
INSERT INTO student_aid (uuid, student_uuid, uoft_uuid, verification_level, match_method, student_number,
                         submission_ts, terms, status, stage, aid_type, requested_amount, wfni, emergency, flags,
                         batch, record)
VALUES (:uuid, :student_uuid, :uoft_uuid, :verification_level, :match_method, :id, :submission_ts, :terms, :status,
        :stage, :stream, :requested_amount, :wfni, :emergency, :flags, :batch, :record)
ON CONFLICT(uuid) DO UPDATE SET student_uuid = excluded.student_uuid, uoft_uuid = excluded.uoft_uuid,
    match_method = excluded.match_method, status = excluded.status,
    verification_level = excluded.verification_level,
    stage = COALESCE(student_aid.stage, excluded.stage),
    aid_type = excluded.aid_type, requested_amount = excluded.requested_amount, wfni = excluded.wfni,
    emergency = excluded.emergency, flags = excluded.flags,
    batch = COALESCE(excluded.batch, student_aid.batch), record = COALESCE(excluded.record, student_aid.record)
"""


def save_to_db(conn: sqlite3.Connection, applications: list[dict]):
    """Add (or refresh) one student_aid row per application. Committee decisions already stored are kept."""
    ensure_schema(conn)
    rows = [{**a, "requested_amount": float(a["requested_amount"]) if a["requested_amount"] != "" else None,
             "wfni": a["wfni"] if a["wfni"] != "" else None, "batch": a.get("batch"), "record": a.get("record"),
             "stage": a.get("stage"), "uoft_uuid": a.get("uoft_uuid"),
             "verification_level": a.get("verification_level"), "match_method": a.get("match_method"),
             "student_uuid": a.get("student_uuid")} for a in applications]
    conn.executemany(_UPSERT, rows)
    conn.commit()
    logger.info(f"Saved {len(rows)} application row(s) to student_aid")


def write_master(applications: list[dict], path: Path):
    write_rows(path, MASTER_FIELDS, applications)


def application_key(app: dict) -> str:
    """Whose application and for which term: one binding application per student per term."""
    return (app.get("student_uuid") or app["uuid"]) + "|" + clean_text(app.get("terms", "")).casefold()


def stored_applications(conn: sqlite3.Connection, present: set[str]) -> list[dict]:
    """
    The verified or unverified applications an earlier run stored that are not in this export (the student edited
    or re-submitted the form, which gives a new row, or the row was deleted). They are not processed again, but
    they compete with the export for being a student's binding application, and one that is complete (decided)
    is final: see selection.select_current.
    """
    return [{"uuid": uuid, "status": status, "stage": stage, "student_uuid": student, "terms": terms,
             "submission_ts": ts, "final": stage == COMPLETE_STAGE}
            for uuid, status, stage, student, terms, ts in conn.execute(
                "SELECT uuid, status, stage, student_uuid, terms, submission_ts FROM student_aid "
                "WHERE status IN ('verified', 'unverified')") if uuid not in present]


def process_sap(csv_file: Path | str, conn: sqlite3.Connection, output_dir: Path | str = "secrets/output",
                batch_size: int = 50, reviewers=()) -> dict:
    """
    Process a SABP form export. Writes sap_verified.csv and sap_unverified.csv (private, with names and contact
    details) and sap_batches/batch_NNN.{csv,txt} (anonymized, for the committee) under output_dir, and records
    every application in the student_aid table. With `reviewers` (two or more names), each batch is also assigned
    to two of them and copied to sap_batches/by_reviewer/<name>/ for them to fill in; see merge_reviews.

    Give the full export each time. Every application in it is verified, so an edit or a re-submission (which Google
    Forms stamps with a new timestamp) is a newer application. Per student and term, the newest verified application
    is the binding one; a newer unverified one never replaces it (it becomes non-binding), and one that was verified
    before is never downgraded when the student data changes (it is raised in alerts_sap.csv). Applications already
    in a batch keep their batch and record, and the committee's work (ratings, decisions) is never overwritten.
    Once a student's application for a term is complete (the committee decision is recorded, see COMPLETE_STAGE)
    it is final: any other application of theirs for that term is not considered.
    A stored application that is no longer in the export is kept as it is, and raised in alerts_sap.csv if it is
    still verified; if the student has a newer application it is superseded.
    """
    reviewers = parse_reviewers(reviewers)
    csv_file = Path(csv_file)
    out = Path(absfile(output_dir))
    out.mkdir(parents=True, exist_ok=True)

    applications = read_applications(csv_file)
    ensure_schema(conn)
    stages = dict(conn.execute("SELECT uuid, stage FROM student_aid"))
    for app in applications:
        app["final"] = stages.get(app["uuid"]) == COMPLETE_STAGE
    absent = stored_applications(conn, {a["uuid"] for a in applications})
    run = Run(conn, "student_aid", "SABP", "sap", applications, key=application_key, absent=absent)

    verify_applications(applications, conn.cursor())
    run.keep_verified()
    for app in applications:
        if app["status"] == "unverified":
            logger.warning(f"UNVERIFIED SABP application {app['uuid']}")
    run.select()
    verified = [a for a in run.current if a["status"] == "verified"]
    unverified = [a for a in run.current if a["status"] == "unverified"]
    for app in applications:  # scored on arrival; an application already further along keeps its stage (see _UPSERT)
        app["stage"] = "scored" if app["status"] == "verified" else None

    placed = {u: (b, r) for u, b, r in conn.execute(
        "SELECT uuid, batch, record FROM student_aid WHERE batch IS NOT NULL AND record IS NOT NULL")}
    # batches already handed to reviewers: by assignment, or by stage (issued by hand, reviewed or decided)
    closed = {b for (b,) in conn.execute(
        "SELECT batch FROM sap_batch_reviewer UNION SELECT batch FROM student_aid WHERE batch IS NOT NULL "
        f"AND stage IN ({','.join(repr(s) for s in ISSUED)})")}
    batches = assign_batches(verified, batch_size, placed, closed)
    save_to_db(conn, applications)
    run.settle(("student_uuid", "terms"))
    assignments = assign_reviewers(conn, len(batches), reviewers) if reviewers else {}
    write_master(verified, out / "sap_verified.csv")
    write_master(unverified, out / "sap_unverified.csv")
    write_batches(batches, out / "sap_batches", assignments, closed)
    mark_in_review(conn, assignments)  # newly assigned batches are now with reviewers
    gone = [{"severity": "WARNING", "kind": "missing_from_export", "program": run.label, "uuid": a["uuid"],
             "student_uuid": a["student_uuid"] or "",
             "detail": "verified, but its form row is not in this export (the student edited or re-submitted it, or "
                       "the row was deleted); kept as it is, with its batch, ratings and decision"}
            for a in absent if a["status"] == "verified"]
    alerts = run.alert(out, extra=gone)
    stage_counts = {stage or "(none)": n for stage, n in conn.execute(
        "SELECT stage, COUNT(*) FROM student_aid WHERE status = 'verified' GROUP BY stage ORDER BY stage")}

    current = run.current
    summary = {**run.summary(alerts),
               "binding_applications": len(current),
               "verified_applications": len(verified), "unverified_applications": len(unverified),
               "not_considered": sum(s == "not_considered" for s in run.statuses.values()),
               "missing_from_export": len(gone),
               "stages": stage_counts, "wfni_weights_sha256": current_weights().sha256,
               "batches": len(batches), "batch_size": batch_size,
               "reviewers": {str(b): rs for b, rs in assignments.items()},
               "emergency": sum(a["emergency"] for a in verified),
               "flags": {flag: sum(flag in a["flags"].split(";") for a in current)
                         for flag in sorted({f for a in current for f in a["flags"].split(";") if f})}}
    (out / "sap_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info(f"SABP: {len(verified)} verified, {len(unverified)} unverified, {len(batches)} batch(es) "
                f"of up to {batch_size}")
    return {"verified": verified, "unverified": unverified, "alerts": alerts, "batches": batches,
            "assignments": assignments, "summary": summary}


if __name__ == "__main__":
    import doctest
    doctest.testmod()
