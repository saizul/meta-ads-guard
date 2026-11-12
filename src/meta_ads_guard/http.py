"""Minimal HTTP layer.

Everything that talks to the network goes through a :class:`Transport`, so the
Graph client, the notifiers and the tests can swap the real implementation for
a fake one. The default transport uses :mod:`urllib` from the standard library.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import __version__
from .errors import TransportError

USER_AGENT = f"meta-ads-guard/{__version__} (+https://github.com/saizul/meta-ads-guard)"


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def json(self) -> Any:
        if not self.text:
            return None
        return json.loads(self.text)


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> HttpResponse: ...


def encode_params(params: Optional[dict[str, Any]]) -> str:
    """Form-encode a parameter dict the way the Graph API expects.

    Lists and dicts are sent as JSON strings (``fields`` lists are joined with
    commas by the caller before they reach this function). Booleans become
    ``true``/``false`` and ``None`` values are dropped.
    """
    if not params:
        return ""
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            value = json.dumps(value, separators=(",", ":"))
        pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)


class UrllibTransport:
    """Transport built on :func:`urllib.request.urlopen`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        query = encode_params(params)
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        body: Optional[bytes] = None
        send_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"
        elif data is not None:
            body = encode_params(data).encode("utf-8")
            send_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            send_headers.update(headers)
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=send_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return HttpResponse(resp.status, dict(resp.headers.items()), text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, text)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method.upper()} {redact_url(url)} failed: {exc}") from exc


SENSITIVE_PARAMS = ("access_token", "appsecret_proof", "client_secret", "password")


def redact_url(url: str) -> str:
    """Hide tokens in a URL before it is logged or shown in an error message."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(
        [
            (k, "REDACTED" if k in SENSITIVE_PARAMS else v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    # Telegram puts the bot token in the path: /bot<token>/sendMessage
    segments = [
        "botREDACTED" if s.startswith("bot") and ":" in s else s for s in parts.path.split("/")
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/".join(segments), query, parts.fragment)
    )
