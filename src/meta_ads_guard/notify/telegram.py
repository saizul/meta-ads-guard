"""Telegram Bot API notifier (``sendMessage``)."""

from __future__ import annotations

from typing import Optional

from ..http import HttpResponse, Transport, UrllibTransport
from .base import NotifyError, chunk_text

TELEGRAM_API = "https://api.telegram.org"
# Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE = 4096


class TelegramNotifier:
    def __init__(
        self,
        name: str,
        bot_token: str,
        chat_id: str,
        *,
        transport: Optional[Transport] = None,
        api_url: str = TELEGRAM_API,
        disable_notification: bool = False,
        timeout: float = 15.0,
    ):
        if not bot_token or not chat_id:
            raise ValueError("telegram notifier needs a bot token and a chat id")
        self.name = name
        self._token = bot_token
        self.chat_id = str(chat_id)
        self.transport = transport or UrllibTransport()
        self.api_url = api_url.rstrip("/")
        self.disable_notification = disable_notification
        self.timeout = timeout

    def send(self, subject: str, text: str) -> None:
        body = f"{subject}\n\n{text}" if subject else text
        url = f"{self.api_url}/bot{self._token}/sendMessage"
        for part in chunk_text(body, MAX_MESSAGE):
            resp = self.transport.request(
                "POST",
                url,
                json_body={
                    "chat_id": self.chat_id,
                    "text": part,
                    "disable_web_page_preview": True,
                    "disable_notification": self.disable_notification,
                },
                timeout=self.timeout,
            )
            _check(resp)


def _check(resp: HttpResponse) -> None:
    try:
        doc = resp.json()
    except ValueError:
        doc = None
    if resp.status >= 400 or not (isinstance(doc, dict) and doc.get("ok")):
        description = doc.get("description") if isinstance(doc, dict) else resp.text[:200]
        raise NotifyError(f"telegram sendMessage failed (HTTP {resp.status}): {description}")
