"""Exception hierarchy used across the package."""

from __future__ import annotations

from typing import Any, Optional


class MetaAdsGuardError(Exception):
    """Base class for every error raised by meta-ads-guard."""


class ConfigError(MetaAdsGuardError):
    """The configuration file or a rule is invalid."""


class ExpressionError(ConfigError):
    """A rule condition could not be parsed or refers to unknown names."""

    def __init__(self, message: str, expression: str = "", position: Optional[int] = None):
        self.expression = expression
        self.position = position
        detail = message
        if expression and position is not None:
            pointer = " " * position + "^"
            detail = f"{message}\n  {expression}\n  {pointer}"
        super().__init__(detail)


class TransportError(MetaAdsGuardError):
    """The HTTP request could not be completed (DNS, TLS, timeout, ...)."""


class GraphAPIError(MetaAdsGuardError):
    """The Graph API answered with an error object."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        error_type: Optional[str] = None,
        fbtrace_id: Optional[str] = None,
        http_status: Optional[int] = None,
        user_title: Optional[str] = None,
        user_message: Optional[str] = None,
        raw: Optional[dict[str, Any]] = None,
    ):
        self.message = message
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id
        self.http_status = http_status
        self.user_title = user_title
        self.user_message = user_message
        self.raw = raw or {}
        parts = [message]
        if code is not None:
            parts.append(f"code={code}")
        if subcode is not None:
            parts.append(f"subcode={subcode}")
        if fbtrace_id:
            parts.append(f"fbtrace_id={fbtrace_id}")
        super().__init__(" ".join(parts))


class RateLimitError(GraphAPIError):
    """A throttling error (codes 4, 17, 32, 613 or 80000-80014)."""

    def __init__(self, *args: Any, retry_after: Optional[float] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class AuthError(GraphAPIError):
    """The access token is invalid, expired or lacks a permission (code 190, 102, 10, 200)."""


class ReportJobError(MetaAdsGuardError):
    """An asynchronous insights report failed, was skipped or timed out."""
