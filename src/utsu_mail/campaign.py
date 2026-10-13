import csv
import logging
import random
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from utsu_mail.templates import RenderedMessage, Template
from utsu_mail.transport import SmtpSender, SmtpSettings, build_message
from utsu_std.csv_safe import SafeWriter, open_csv, read_cell

logger = logging.getLogger(__name__)

LOG_FIELDS = ["timestamp", "to", "status", "error", "application_uuid", "student_uuid"]
CONFIRM_WORD = "send"


@dataclass
class Prepared:
    to: str
    rendered: RenderedMessage
    application_uuid: str = ""
    student_uuid: str = ""


def prepare(template: Template, rows: list[dict[str, str]], email_field: str
            ) -> tuple[list[Prepared], list[tuple[str, str]]]:
    """Render every row up front, so problems surface before anything is sent. Returns (ready, [(address, reason)])."""
    ready, skipped = [], []
    for row in rows:
        missing = template.missing_fields(row)
        if missing:
            skipped.append((row.get(email_field, "?"), f"blank/missing {', '.join(missing)}"))
        else:
            ready.append(Prepared(row[email_field], template.render(row),
                                  row.get("application_uuid", ""), row.get("student_uuid", "")))
    return ready, skipped


def already_sent(log_path: Path) -> set[str]:
    if not log_path.is_file():
        return set()
    with open_csv(log_path) as f:
        return {read_cell(r["to"]).lower() for r in csv.DictReader(f) if r.get("status") == "sent"}


def append_log(log_path: Path, to: str, status: str, error: str = "", application_uuid: str = "",
               student_uuid: str = ""):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.is_file()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = SafeWriter(f)
        if is_new:
            writer.writerow(LOG_FIELDS)
        writer.writerow([datetime.now().isoformat(timespec="seconds"), to, status, error, application_uuid, student_uuid])


def format_preview(settings: SmtpSettings, item: Prepared, position: int, total: int) -> str:
    msg = build_message(settings, item.to, item.rendered)
    bar = "=" * 72
    lines = [bar, f"SAMPLE {position}/{total}", f"From:    {msg['From']}", f"To:      {msg['To']}"]
    if settings.reply_to:
        lines.append(f"Reply-To: {settings.reply_to}")
    lines += [f"Subject: {item.rendered.subject}", "-" * 72, item.rendered.text.rstrip(), bar]
    if item.rendered.html is not None:
        lines.insert(-1, "[an HTML version is also attached]")
    return "\n".join(lines)


def review(settings: SmtpSettings, ready: list[Prepared], skipped: list[tuple[str, str]],
           send_test: Callable[[Prepared], None], ask: Callable[[str], str] = input,
           say: Callable[[str], None] = print, rng: random.Random | None = None) -> bool:
    """
    Show a sample email and ask the user what to do. Returns True only if the user typed the
    confirmation word to send to everyone.
    """
    rng = rng or random.Random()
    if skipped:
        say(f"{len(skipped)} recipient(s) will be skipped:")
        for address, reason in skipped[:20]:
            say(f"  - {address}: {reason}")
        if len(skipped) > 20:
            say(f"  ... and {len(skipped) - 20} more")

    index = 0
    while True:
        say(format_preview(settings, ready[index], index + 1, len(ready)))
        say(f"[{CONFIRM_WORD}] send to all {len(ready)} recipients | [t] email this sample to {settings.from_addr} only | "
            f"[r] show another random sample | [n] cancel")
        try:
            choice = ask("> ").strip().lower()
        except EOFError:
            return False
        if choice == CONFIRM_WORD:
            return True
        if choice == "t":
            try:
                send_test(ready[index])
                say(f"Test email sent to {settings.from_addr}.")
            except (smtplib.SMTPException, OSError) as e:
                say(f"Test email failed: {e}")
        elif choice == "r":
            index = rng.randrange(len(ready))
        elif choice in ("n", "no", "q", "quit", ""):
            return False
        else:
            say(f"Type '{CONFIRM_WORD}' to confirm, or t / r / n.")


def send_all(settings: SmtpSettings, ready: list[Prepared], log_path: Path,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """Send each message, recording every outcome to the CSV log as it happens (so a rerun can resume)."""
    counts = {"sent": 0, "failed": 0}
    with SmtpSender(settings) as sender:
        try:
            for i, item in enumerate(ready):
                try:
                    sender.send(build_message(settings, item.to, item.rendered))
                except (smtplib.SMTPException, OSError, ValueError) as e:
                    counts["failed"] += 1
                    append_log(log_path, item.to, "failed", str(e), item.application_uuid, item.student_uuid)
                    logger.error(f"FAILED {item.to}: {e}")
                else:
                    counts["sent"] += 1
                    append_log(log_path, item.to,                     "sent", "", item.application_uuid, item.student_uuid)
                    logger.info(f"SENT {item.to} ({i + 1}/{len(ready)})")
                if settings.delay and i < len(ready) - 1:
                    sleep(settings.delay)
        except KeyboardInterrupt:
            logger.warning("Interrupted; rerun to resume (already-sent addresses are skipped).")
    counts["remaining"] = len(ready) - counts["sent"] - counts["failed"]
    return counts
