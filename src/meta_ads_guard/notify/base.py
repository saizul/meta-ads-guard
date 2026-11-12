"""Notifier interface."""

from __future__ import annotations

from typing import Protocol

from ..errors import MetaAdsGuardError


class NotifyError(MetaAdsGuardError):
    """A notification could not be delivered."""


class Notifier(Protocol):
    name: str

    def send(self, subject: str, text: str) -> None: ...


def chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, on line breaks when possible."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()]
