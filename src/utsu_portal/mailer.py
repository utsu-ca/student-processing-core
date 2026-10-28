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
