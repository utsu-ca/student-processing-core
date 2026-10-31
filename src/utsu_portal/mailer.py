from __future__ import annotations

import logging

log = logging.getLogger("utsu_portal.mail")


class Mailer:
    """Override `send` to deliver real mail (e.g. via utsu_mail.transport)."""

    def send(self, to: str, subject: str, body: str) -> None:
        log.info("mail to %s: %s", to, subject)


class OutboxMailer(Mailer):
    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, str]] = []

    def send(self, to, subject, body):
        self.outbox.append((to, subject, body))


class SMTPMailer(Mailer):
    """Provider-neutral delivery over SMTP (SES, SendGrid, Office 365, Gmail relay, Postfix...).

    Configure with PORTAL_SMTP_URL=smtp[s]://user:pass@host:port and PORTAL_MAIL_FROM.
    """

    def __init__(self, url: str, sender: str) -> None:
        from urllib.parse import unquote, urlsplit

        u = urlsplit(url)
        self.ssl, self.host = u.scheme == "smtps", u.hostname
        self.port = u.port or (465 if self.ssl else 587)
        self.user, self.password, self.sender = unquote(u.username or ""), unquote(u.password or ""), sender

    def send(self, to: str, subject: str, body: str) -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with (smtplib.SMTP_SSL(self.host, self.port, timeout=20) if self.ssl else smtplib.SMTP(self.host, self.port, timeout=20)) as s:
            if not self.ssl:
                s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)


def mailer_from_env() -> Mailer:
    import os

    url = os.environ.get("PORTAL_SMTP_URL")
    return SMTPMailer(url, os.environ.get("PORTAL_MAIL_FROM", "no-reply@localhost")) if url else Mailer()
