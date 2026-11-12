"""Email notifier over SMTP (STARTTLS, implicit TLS or plain)."""

from __future__ import annotations

import contextlib
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Optional

from .base import NotifyError

SmtpFactory = Callable[..., Any]


class EmailNotifier:
    def __init__(
        self,
        name: str,
        *,
        host: str,
        sender: str,
        recipients: list[str],
        port: int = 587,
        username: Optional[str] = None,
        password: Optional[str] = None,
        security: str = "starttls",
        subject_prefix: str = "[meta-ads-guard]",
        timeout: float = 30.0,
        smtp_factory: Optional[SmtpFactory] = None,
        ssl_factory: Optional[SmtpFactory] = None,
    ):
        if security not in {"starttls", "ssl", "none"}:
            raise ValueError("security must be starttls, ssl or none")
        if not recipients:
            raise ValueError("email notifier needs at least one recipient")
        self.name = name
        self.host = host
        self.port = port
        self.sender = sender
        self.recipients = list(recipients)
        self.username = username
        self._password = password
        self.security = security
        self.subject_prefix = subject_prefix
        self.timeout = timeout
        self._smtp = smtp_factory or smtplib.SMTP
        self._smtp_ssl = ssl_factory or smtplib.SMTP_SSL

    def build_message(self, subject: str, text: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"{self.subject_prefix} {subject}".strip()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or None)
        msg.set_content(text)
        return msg

    def send(self, subject: str, text: str) -> None:
        msg = self.build_message(subject, text)
        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                server = self._smtp_ssl(
                    self.host, self.port, timeout=self.timeout, context=context
                )
            else:
                server = self._smtp(self.host, self.port, timeout=self.timeout)
            try:
                if self.security == "starttls":
                    server.starttls(context=context)
                if self.username:
                    server.login(self.username, self._password or "")
                server.send_message(msg)
            finally:
                with contextlib.suppress(smtplib.SMTPException, OSError):
                    server.quit()
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"email via {self.host}:{self.port} failed: {exc}") from exc
