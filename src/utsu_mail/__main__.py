"""
Mail-merge CLI. Run from the `src` directory:

    python -m utsu_mail --list_fields
    python -m utsu_mail senate_verified --dry_run
    python -m utsu_mail senate_verified --recipients secrets/output/verified.csv
"""
import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from utsu_mail.campaign import already_sent, prepare, review, send_all
from utsu_mail.campaign import format_preview
from utsu_mail.recipients import read_csv, read_sql, split_by_validity
from utsu_mail.schema import schema_fields, tables_providing
from utsu_mail.templates import DEFAULT_TEMPLATE_DIR, RenderedMessage, TemplateError, list_templates, load_template
from utsu_mail.transport import SmtpSender, SmtpSettings, build_message
from utsu_std.utils import absfile, load_config_file

logger = logging.getLogger("utsu_mail")
PASSWORD_ENV = "UTSU_SMTP_PASSWORD"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="utsu_mail", description="Send templated emails to a list of recipients over SMTP.")
    p.add_argument("template", nargs="?", help="Template name (a .txt file in the template directory, without extension)")
    p.add_argument("--template_dir", default=DEFAULT_TEMPLATE_DIR, help="Directory containing templates")
    p.add_argument("--schema_dir", default="src/schema", help="Directory of .sql schema files used to validate fields")
    p.add_argument("--recipients", help="CSV of recipients (default: <output_dir>/verified.csv)")
    p.add_argument("--query", help="Read recipients with this SELECT against --db instead of a CSV; "
                                   "column names become template fields")
    p.add_argument("--db", help="SQLite database for --query (default: 'db' from the config file)")
    p.add_argument("--email_field", default="email", help="Recipient column holding the address")
    p.add_argument("--config", default="config.json", help="Config file; SMTP settings live in its 'smtp' section")
    p.add_argument("--log_dir", help="Where send logs are written (default: <output_dir>)")
    p.add_argument("--resend", action="store_true", help="Also send to addresses already logged as sent for this template")
    p.add_argument("--dry_run", action="store_true", help="Render and preview only; never connect to SMTP")
    p.add_argument("--list_fields", action="store_true", help="List templates, schema columns and template fields, then exit")
    return p


def print_fields(args, schema: dict[str, list[str]]):
    template_dir = absfile(args.template_dir)
    print(f"Templates in {template_dir}: {', '.join(list_templates(template_dir)) or 'none'}")
    print("Fields available from the schema:")
    for table, columns in schema.items():
        print(f"  {table}: {', '.join(columns)}")
    if args.template:
        print(f"Fields used by '{args.template}': {', '.join(sorted(load_template(args.template, template_dir).fields)) or 'none'}")


def load_recipients(args, cfg: dict) -> list[dict[str, str]]:
    if args.query:
        db = args.db or cfg.get("db")
        if not db:
            raise ValueError("--query needs --db or a 'db' entry in the config file")
        return read_sql(absfile(db), args.query)
    path = absfile(args.recipients or Path(cfg.get("output_dir", "secrets/output")) / "verified.csv")
    if not path.is_file():
        raise FileNotFoundError(f"Recipients file not found: {path}")
    return read_csv(path)


def check_fields(template, rows, schema):
    """Fail early if a template uses a field the recipient data doesn't have, hinting at the schema table."""
    available = set(rows[0]) if rows else set()
    unknown = sorted(template.fields - available)
    if unknown:
        hints = []
        for field in unknown:
            tables = tables_providing(field, schema)
            hints.append(f"{field}" + (f" (a column of {', '.join(tables)}; select it in --query)" if tables else ""))
        raise TemplateError(f"Template '{template.name}' uses fields the recipient data lacks: {'; '.join(hints)}. "
                            f"Recipient columns: {', '.join(sorted(available))}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    cfg = load_config_file(str(absfile(args.config)))
    schema = schema_fields(absfile(args.schema_dir))

    try:
        if args.list_fields:
            print_fields(args, schema)
            return 0
        if not args.template:
            print("A template name is required (see --list_fields).")
            return 2

        template = load_template(args.template, absfile(args.template_dir))
        rows = load_recipients(args, cfg)
        check_fields(template, rows, schema)
    except (TemplateError, ValueError, OSError) as e:
        logger.error(e)
        return 2

    rows, invalid = split_by_validity(rows, args.email_field)
    ready, skipped = prepare(template, rows, args.email_field)
    skipped = [(f"row {n}", why) for n, why in invalid] + skipped

    log_path = absfile(args.log_dir or cfg.get("output_dir", "secrets/output")) / f"mail_log_{template.name}.csv"
    if not args.resend:
        done = already_sent(log_path)
        if done:
            before = len(ready)
            ready = [r for r in ready if r.to.lower() not in done]
            if before != len(ready):
                logger.info(f"Skipping {before - len(ready)} address(es) already sent for '{template.name}' (use --resend to override)")

    if not ready:
        logger.error(f"Nothing to send ({len(skipped)} skipped).")
        for who, why in skipped[:20]:
            logger.error(f"  {who}: {why}")
        return 1

    try:
        settings = SmtpSettings.from_config(cfg.get("smtp", {}))
        settings.validate()
    except ValueError as e:
        if not args.dry_run:
            logger.error(e)
            return 2
        logger.warning(f"{e}; previewing with a placeholder sender")
        settings = SmtpSettings(host="", from_addr="noreply@example.invalid")

    if args.dry_run:
        print(format_preview(settings, ready[0], 1, len(ready)))
        print(f"DRY RUN: {len(ready)} email(s) would be sent, {len(skipped)} skipped. Nothing was sent.")
        return 0

    settings.password = os.environ.get(PASSWORD_ENV, "")
    if settings.username and not settings.password:
        settings.password = getpass.getpass(f"SMTP password for {settings.username}: ")

    def send_test(item):
        test = RenderedMessage("[TEST] " + item.rendered.subject, item.rendered.text, item.rendered.html)
        with SmtpSender(settings) as sender:
            sender.send(build_message(settings, settings.from_addr, test))

    if not review(settings, ready, skipped, send_test):
        print("Cancelled; nothing was sent.")
        return 1

    try:
        counts = send_all(settings, ready, log_path)
    except OSError as e:
        logger.error(f"Could not connect/authenticate to SMTP server: {e}")
        return 1
    except Exception as e:
        logger.error(f"SMTP error: {e}")
        return 1
    print(f"Done: {counts['sent']} sent, {counts['failed']} failed, {counts['remaining']} not attempted. Log: {log_path}")
    return 0 if not counts["failed"] and not counts["remaining"] else 1


if __name__ == "__main__":
    sys.exit(main())
