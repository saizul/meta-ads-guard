"""Slack-compatible incoming webhook notifier.

Slack, Mattermost, Rocket.Chat, Google Chat and many automation tools (n8n,
Make, Zapier catch hooks) accept ``{"text": "..."}``. Discord expects the
message in ``content``; set ``field: content`` for it.
"""

from __future__ import annotations

from typing import Optional

from ..http import Transport, UrllibTransport, redact_url
from .base import NotifyError, chunk_text


class WebhookNotifier:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        field: str = "text",
        max_length: int = 3500,
        extra: Optional[dict[str, object]] = None,
        transport: Optional[Transport] = None,
        timeout: float = 15.0,
    ):
        if not url.startswith(("https://", "http://")):
            raise ValueError("webhook url must start with https:// or http://")
        self.name = name
        self._url = url
        self.field = field
        self.max_length = max_length
        self.extra = dict(extra or {})
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def send(self, subject: str, text: str) -> None:
        body = f"*{subject}*\n{text}" if subject else text
        for part in chunk_text(body, self.max_length):
            resp = self.transport.request(
                "POST", self._url, json_body={**self.extra, self.field: part}, timeout=self.timeout
            )
            if resp.status >= 300:
                raise NotifyError(
                    f"webhook {redact_url(self._url).split('?')[0][:40]}... answered "
                    f"HTTP {resp.status}: {resp.text[:200]}"
                )
