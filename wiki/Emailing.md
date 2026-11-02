# Emailing

`utsu_mail` sends mail-merge emails directly over SMTP, with a safe preview-and-confirm workflow.

```powershell
python -m utsu_mail --list_fields                 # templates + columns from the programs' schema.sql files
python -m utsu_mail senate_verified --dry_run     # render a preview, never connects to SMTP
python -m utsu_mail senate_verified               # preview, confirm, send
```

## Templates

Templates live in `templates/email/`. `<name>.txt` is a `Subject: ...` line, a blank line, then the plain-text body. An
optional `<name>.html` is added as the HTML alternative (values are HTML-escaped). Placeholders are `{{ field }}`. To change
an email, edit the file; to add one, drop in a new `.txt`.

- The only header allowed is `Subject`. Whitespace/newlines in a rendered subject are collapsed (no header injection).
- A recipient with a **blank or missing field** the template needs is skipped and listed.
- Templates are checked against the recipient columns before anything is sent. A missing field produces an error that
  points at the schema table that has the column.

Shipped templates:

| Template | Fields used |
|---|---|
| `senate_verified` | `full_name`, `division` |
| `senate_unverified` | `full_name`, `id` |
| `senate_more_info` | `full_name`, `submission_ts`, `unverified_fields`, `application_uuid`, `student_uuid` |
| `senate_rejection` | `full_name`, `division`, `submission_ts`, `rejection_reason`, `application_uuid`, `student_uuid` |
| `sabp_more_info` | `full_name`, `terms`, `submission_ts`, `unverified_fields`, `application_uuid`, `student_uuid` |
| `sabp_rejection` | `full_name`, `terms`, `submission_ts`, `rejection_reason`, `application_uuid`, `student_uuid` |
| `agm_more_info` | `full_name`, `submission_ts`, `unverified_fields`, `application_uuid`, `student_uuid` |
| `agm_rejection` | `full_name`, `submission_ts`, `rejection_reason`, `application_uuid`, `student_uuid` |

## Recipients

From a CSV (default `<output_dir>/verified.csv`, or `--recipients`) or a read-only `--query "SELECT ... FROM uoft_data ..."`
against `--db`. Each column is a field, so alias columns to match a template. The query must start with `SELECT` or
`WITH` and runs on a read-only connection.

Rows with an invalid or **duplicate** address (case-insensitive) are skipped and reported. Addresses containing control
or invisible characters are treated as invalid.

## SMTP settings

In the `smtp` section of `config.json`:

| Key | Default | Notes |
|---|---|---|
| `host` | *(required)* | |
| `from_addr` | *(required)* | |
| `port` | `587` | |
| `security` | `starttls` | `starttls`, `ssl` or `none` |
| `username` | *(none)* | enables login |
| `from_name` | *(none)* | |
| `reply_to` | *(none)* | |
| `timeout` | `30` | seconds |
| `delay` | `0.5` | seconds between messages |

The **password is never read from config**: set the environment variable `UTSU_SMTP_PASSWORD` or you are prompted (only
when `username` is set). Sending credentials with `security: none` is refused unless the host is `localhost`,
`127.0.0.1` or `::1`.

## Safety workflow

1. A sample is rendered and shown, with the list of skipped recipients.
2. Choose: type **`send`** to send to everyone, **`t`** to email that sample to the *sender address only*, **`r`** to show
   another random sample, **`n`** to cancel (blank, `no`, `q`, `quit` also cancel).
3. Every outcome is appended to `<output_dir>/mail_log_<template>.csv` (`timestamp, to, status, error, application_uuid,
   student_uuid`) as it happens.
4. Addresses already `sent` for that template are **skipped on re-run** unless `--resend`, so an interrupted send can be
   resumed (Ctrl+C stops cleanly). The connection is re-established once if the server drops it.

Exit codes: `0` everything sent (or dry run); `1` cancelled, nothing to send, or some failed/not attempted; `2`
configuration or input problem.

## Rejections and requests for more information

Subjects carry `[Ref <application uuid>]` and every body lists the application uuid, the student uuid and the
submission timestamp. These are internal database keys, kept in the email only as a reference for replies; a reply is
matched back by the student's email, student number or name, **not** by the id.

`utsu_mail.notices` builds the recipient file. Each row keeps its columns and gains `application_uuid`, `student_uuid`
and, for `more_info`, `unverified_fields` (which details could not be verified, judged from what the application actually
gave) or, for `rejection`, `rejection_reason`:

```powershell
python -m utsu_mail.notices senate more_info                              # reads <output_dir>/unverified.csv
python -m utsu_mail.notices sabp more_info                                # reads <output_dir>/sap_unverified.csv
python -m utsu_mail.notices agm rejection --reason "Not a UTSU member"    # reads <output_dir>/agm_unverified.csv
python -m utsu_mail senate_more_info --recipients <output_dir>/notices_senate_more_info.csv
```

`--input` and `--output` override the files; `--db` the database holding `uoft_data`. For `more_info`, applications that
now match the roster are skipped (nothing to ask for). A rejection needs `--reason` or a `rejection_reason` column.

## Command reference

`python -m utsu_mail [template]`

| Flag | Default | Meaning |
|---|---|---|
| `template` | | template name (file stem) |
| `--template_dir` | `templates/email` | |
| `--schema_dir` | *(the schemas of `utsu_core` and every program)* | `.sql` files used to validate fields |
| `--recipients` | `<output_dir>/verified.csv` | recipient CSV |
| `--query` | | read recipients with a SELECT instead |
| `--db` | `db` from config | database for `--query` |
| `--email_field` | `email` | column with the address |
| `--config` | `config.json` | |
| `--log_dir` | `<output_dir>` | where `mail_log_<template>.csv` goes |
| `--resend` | off | also send to addresses already sent |
| `--dry_run` | off | preview only, never connects |
| `--list_fields` | off | list templates, schema columns and template fields |

`python -m utsu_mail.notices <senate|sabp|agm> <more_info|rejection> [--input F] [--output F] [--db F] [--config F] [--reason TEXT]`
