"""Small Graph API client: versioned URLs, retries, throttling, pagination."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Iterator
from typing import Any, Optional

from .errors import AuthError, GraphAPIError, RateLimitError, TransportError
from .http import HttpResponse, Transport, UrllibTransport
from .ratelimit import (
    TRANSIENT_CODES,
    RateLimiter,
    is_retryable_throttle,
    is_throttle,
    parse_usage,
)

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v27.0"
GRAPH_URL = "https://graph.facebook.com"
AUTH_CODES = frozenset({102, 190})
PERMISSION_CODES = frozenset({10, 200, 294})
_VERSION_RE = re.compile(r"^v\d+\.\d+$")


def normalize_account_id(account_id: str) -> str:
    """Return ``act_<digits>`` for ``123``, ``act_123`` or ``"act_123 "``."""
    value = str(account_id).strip()
    if value.startswith("act_"):
        value = value[4:]
    if not value.isdigit():
        raise ValueError(f"invalid ad account id: {account_id!r}")
    return f"act_{value}"


def appsecret_proof(access_token: str, app_secret: str) -> str:
    """HMAC-SHA256 of the token keyed with the app secret, as hex."""
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


class GraphClient:
    """Thin wrapper around the Graph API.

    ``transport`` is injectable so tests (and callers with their own HTTP
    stack) never touch the network. Throttling headers are fed to the
    :class:`RateLimiter` after every response.
    """

    def __init__(
        self,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        app_secret: Optional[str] = None,
        transport: Optional[Transport] = None,
        rate_limiter: Optional[RateLimiter] = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        base_url: str = GRAPH_URL,
    ):
        if not access_token:
            raise ValueError("access_token is required")
        if not _VERSION_RE.match(api_version):
            raise ValueError(f"api_version must look like v24.0, got {api_version!r}")
        self.access_token = access_token
        self.api_version = api_version
        self.app_secret = app_secret
        self.transport = transport or UrllibTransport()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.calls = 0

    # -- low level ---------------------------------------------------------

    def url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"

    def _auth(self) -> dict[str, str]:
        auth = {"access_token": self.access_token}
        if self.app_secret:
            auth["appsecret_proof"] = appsecret_proof(self.access_token, self.app_secret)
        return auth

    def request(
        self, method: str, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Call ``path`` and return the decoded JSON object.

        Retries throttling errors (with the wait Meta suggests), transient
        errors and 5xx responses up to ``max_retries`` times.
        """
        method = method.upper()
        payload = {**(params or {}), **self._auth()}
        payload = {k: _fields(v) if k == "fields" else v for k, v in payload.items()}
        url = self.url(path)
        attempt = 0
        while True:
            self.calls += 1
            try:
                if method == "GET":
                    resp = self.transport.request(
                        "GET", url, params=payload, timeout=self.timeout
                    )
                else:
                    resp = self.transport.request(
                        method, url, data=payload, timeout=self.timeout
                    )
            except TransportError:
                if attempt >= self.max_retries:
                    raise
                delay = self.rate_limiter.retry_delay(attempt)
                log.warning("network error on %s %s, retrying in %.1fs", method, path, delay)
                self.rate_limiter.sleep(delay)
                attempt += 1
                continue

            try:
                result = self._handle(resp)
            except RateLimitError as exc:
                if attempt >= self.max_retries or not is_retryable_throttle(exc.code, exc.subcode):
                    raise
                delay = self.rate_limiter.retry_delay(attempt, exc.retry_after)
                log.warning("throttled (%s), retrying %s in %.1fs", exc, path, delay)
                self.rate_limiter.sleep(delay)
                attempt += 1
                continue
            except GraphAPIError as exc:
                transient = exc.code in TRANSIENT_CODES or (exc.http_status or 0) >= 500
                if not transient or attempt >= self.max_retries:
                    raise
                delay = self.rate_limiter.retry_delay(attempt)
                log.warning("transient error (%s), retrying %s in %.1fs", exc, path, delay)
                self.rate_limiter.sleep(delay)
                attempt += 1
                continue
            self.rate_limiter.after_response(resp.headers)
            return result

    def _handle(self, resp: HttpResponse) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "error" in body:
            raise self._error(body["error"], resp)
        if resp.status >= 400:
            raise GraphAPIError(
                f"HTTP {resp.status}: {resp.text[:200]}", http_status=resp.status
            )
        if body is None:
            return {}
        if not isinstance(body, dict):
            return {"data": body}
        return body

    def _error(self, err: dict[str, Any], resp: HttpResponse) -> GraphAPIError:
        code = _int(err.get("code"))
        subcode = _int(err.get("error_subcode"))
        kwargs: dict[str, Any] = {
            "code": code,
            "subcode": subcode,
            "error_type": err.get("type"),
            "fbtrace_id": err.get("fbtrace_id"),
            "http_status": resp.status,
            "user_title": err.get("error_user_title"),
            "user_message": err.get("error_user_msg"),
            "raw": err,
        }
        message = err.get("message") or "Graph API error"
        if is_throttle(code, subcode):
            snap = parse_usage(resp.headers)
            retry_after = snap.regain_seconds or None
            return RateLimitError(message, retry_after=retry_after, **kwargs)
        if code in AUTH_CODES or code in PERMISSION_CODES:
            return AuthError(message, **kwargs)
        return GraphAPIError(message, **kwargs)

    # -- conveniences ------------------------------------------------------

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.request("GET", path, params)

    def post(self, path: str, **params: Any) -> dict[str, Any]:
        return self.request("POST", path, params)

    def paginate(
        self, path: str, params: Optional[dict[str, Any]] = None, *, max_pages: int = 1000
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of an edge, following ``paging.cursors.after``.

        Following the cursor (instead of the ``paging.next`` URL) keeps the
        token out of URLs we did not build and keeps the API version pinned.
        """
        query = dict(params or {})
        for _ in range(max_pages):
            page = self.request("GET", path, query)
            yield from page.get("data", []) or []
            paging = page.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return
            query["after"] = after
        log.warning("stopped paginating %s after %d pages", path, max_pages)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fields(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value

