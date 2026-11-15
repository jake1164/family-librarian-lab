"""One HTTP helper for every adapter: timeouts, retries, budgets, redaction.

Adapters never call ``requests`` themselves. That keeps three promises:

* every request has the same bounded timeout / retry / rate-limit behaviour;
* a credential only ever travels through ``secret_params`` / ``secret_headers``,
  so it cannot be copied into a recorded exchange by accident;
* replay can rebuild a run from recorded exchanges without touching the network.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

import requests

from . import VERSION
from .config import contact_detail
from .model import ErrorKind, Operation, Pagination, ProviderError, RawExchange

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 20.0
MAX_RETRIES = 2
BACKOFF_S = (1.0, 3.0)
RETRY_AFTER_CAP_S = 20.0
DEFAULT_MAX_CALLS = 10
DEFAULT_MAX_PAGES = 3
DEFAULT_MIN_INTERVAL_S = 1.0
REDACTED = "***"

#: Request classes an adapter may pace separately. ``default`` uses the adapter's
#: base interval; the others only apply where an adapter declares an override.
REQUEST_CLASS_DEFAULT = "default"
REQUEST_CLASS_SEARCH = "search"
REQUEST_CLASS_LOOKUP = "lookup"

_TIMEOUT_EXCEPTIONS = (TimeoutError, requests.exceptions.Timeout)
_CONNECTION_EXCEPTIONS = (ConnectionError, requests.exceptions.ConnectionError)


class BudgetExceeded(Exception):
    """Raised internally when an operation spends more calls than allowed."""


@dataclass
class TransportResponse:
    """The tiny slice of an HTTP response this tool needs."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""

    def json(self) -> Any:
        return json.loads(self.text)


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body: str | None,
        timeout: tuple[float, float],
    ) -> TransportResponse: ...


class ProviderHttp(Protocol):
    """What the engine needs from an HTTP provider (real or replay)."""

    provider_id: str
    secrets_seen: set[str]

    def context(
        self,
        *,
        operation: Operation | None = None,
        credential: str | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
    ) -> "OpContext": ...


class RequestsTransport:
    """Real network transport. Injected into ``HttpClient`` so tests stay offline."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body: str | None,
        timeout: tuple[float, float],
    ) -> TransportResponse:
        response = self._session.request(
            method, url, params=params, headers=headers, data=body, timeout=timeout
        )
        return TransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
        )


def scrub(value: Any, secrets: Iterable[str]) -> Any:
    """Public wrapper: replace secret values inside any structure."""

    return _scrub_value(value, {str(secret) for secret in secrets if secret})


def user_agent() -> str:
    """Identifying User-Agent; contact detail only when explicitly configured."""

    base = f"family-librarian-metadata-lab/{VERSION}"
    contact = contact_detail()
    return f"{base} ({contact})" if contact else base


def _scrub_text(text: str, secrets: set[str]) -> str:
    for secret in secrets:
        if len(secret) >= 3:
            text = text.replace(secret, REDACTED)
    return text


def _scrub_value(value: Any, secrets: set[str]) -> Any:
    """Recursively replace secret values inside a parsed body."""

    if not secrets:
        return value
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, list):
        return [_scrub_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item, secrets) for key, item in value.items()}
    return value


def _classify_exception(exc: BaseException) -> ErrorKind:
    if isinstance(exc, _TIMEOUT_EXCEPTIONS):
        return ErrorKind.TIMEOUT
    if isinstance(exc, _CONNECTION_EXCEPTIONS):
        return ErrorKind.NETWORK
    return ErrorKind.NETWORK


def _bump(stats: dict[str, int] | None, key: str) -> None:
    """Count a retry/429 for the op's ``stats`` block, when a caller wants it."""

    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def _retry_after_seconds(
    headers: Mapping[str, str], *, cap: float | None = RETRY_AFTER_CAP_S
) -> float | None:
    """``Retry-After`` in seconds, clamped to ``cap`` unless ``cap`` is ``None``.

    The unclamped value matters for the breaker and the cool-down record: a
    provider that asks for 30 minutes is not asked again in this run.
    """

    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    try:
        seconds = max(float(raw), 0.0)
    except ValueError:
        return None
    return seconds if cap is None else min(seconds, cap)


def _breaker_trips(consecutive_429: int, retry_after_s: float | None) -> bool:
    """Should this provider be left alone for the rest of the run?

    Two signals, both about stopping before a block gets worse: the provider asked
    for a wait longer than the retry cap, or it said 429 twice in a row.
    """

    if retry_after_s is not None and retry_after_s > RETRY_AFTER_CAP_S:
        return True
    return consecutive_429 >= 2


def body_digest(body: str | None) -> str:
    return hashlib.sha1((body or "").encode("utf-8")).hexdigest()


def exchange_key(
    provider_id: str, method: str, url: str, params: Mapping[str, Any] | None, body: str | None
) -> tuple[str, str, str, str, str]:
    """Key used by replay. Secret parameters are absent by construction."""

    cleaned = {str(k): str(v) for k, v in (params or {}).items()}
    return (
        provider_id,
        method.upper(),
        url,
        json.dumps(cleaned, sort_keys=True),
        body_digest(body),
    )


class HttpClient:
    """Shared request pipeline for one provider (sequential, never threaded).

    Pacing is per provider, not per operation: one client keeps the timestamp of
    the last call whatever operation made it. A request class (``search``,
    ``lookup``, ...) may pace a single call differently, which is how one adapter
    can slow searches down without slowing every lookup.

    The client is also where the circuit breaker lives: once a provider has asked
    for a wait longer than ``RETRY_AFTER_CAP_S``, or answered 429 twice in a row,
    ``blocked`` is set and every later call raises instead of sending.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        transport: Transport | None = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        request_intervals_s: Mapping[str, float] | None = None,
        agent: str | None = None,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_id = provider_id
        self._transport = transport or RequestsTransport()
        self._min_interval_s = max(0.0, min_interval_s)
        self._request_intervals_s = {
            str(name): max(0.0, float(value))
            for name, value in (request_intervals_s or {}).items()
        }
        self._agent = agent or user_agent()
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._clock = clock
        self._last_call_at: float | None = None
        self._consecutive_429 = 0
        self._blocked_note: str | None = None
        self._blocked_retry_after_s: float | None = None
        self.secrets_seen: set[str] = set()

    # -- pacing ----------------------------------------------------------------

    def interval_for(self, request_class: str = REQUEST_CLASS_DEFAULT) -> float:
        """Seconds to leave before a call of this class (base interval by default)."""

        return self._request_intervals_s.get(str(request_class), self._min_interval_s)

    def max_interval_s(self) -> float:
        """The slowest interval this client can pace a call at."""

        return max([self._min_interval_s, *self._request_intervals_s.values()])

    # -- circuit breaker -------------------------------------------------------

    @property
    def blocked(self) -> bool:
        return self._blocked_note is not None

    @property
    def blocked_note(self) -> str | None:
        return self._blocked_note

    @property
    def blocked_retry_after_s(self) -> float | None:
        """The last unclamped ``Retry-After``, for the cool-down record."""

        return self._blocked_retry_after_s

    def block(self, note: str, *, retry_after_s: float | None = None) -> None:
        """Stop calling this provider for the rest of this client's life."""

        if self._blocked_note is None:
            self._blocked_note = str(note)
        if retry_after_s is not None:
            self._blocked_retry_after_s = retry_after_s

    def _refuse_when_blocked(self) -> None:
        if self._blocked_note is None:
            return
        raise ProviderError(
            ErrorKind.RATE_LIMITED, f"provider cooling down: {self._blocked_note}"
        )

    def context(
        self,
        *,
        operation: Operation | None = None,
        credential: str | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> "OpContext":
        return OpContext(
            self,
            operation=operation,
            credential=credential,
            max_calls=max_calls,
            max_pages=max_pages,
        )

    def _throttle(self, interval_s: float | None = None) -> None:
        if self._last_call_at is None:
            return
        interval = self._min_interval_s if interval_s is None else max(0.0, interval_s)
        remaining = interval - (self._clock() - self._last_call_at)
        if remaining > 0:
            self._sleep(remaining)

    def send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        body: str | None,
        secrets: set[str],
        stats: dict[str, int] | None = None,
        interval_s: float | None = None,
    ) -> TransportResponse:
        """Send with retries. Raises an already-redacted ``ProviderError``."""

        self._refuse_when_blocked()
        request_headers = {"User-Agent": self._agent, **(headers or {})}
        attempt = 0
        while True:
            self._throttle(interval_s)
            self._last_call_at = self._clock()
            try:
                response = self._transport.send(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    body=body,
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                )
            except Exception as exc:
                kind = _classify_exception(exc)
                if kind in (ErrorKind.TIMEOUT, ErrorKind.NETWORK) and attempt < self._max_retries:
                    _bump(stats, "retries")
                    self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                    attempt += 1
                    continue
                raise ProviderError(
                    kind, _scrub_text(f"{type(exc).__name__}: {exc}", secrets)
                ) from None

            status = response.status_code
            retry_after = _retry_after_seconds(response.headers, cap=None)
            if status in (401, 403):
                self._consecutive_429 = 0
                raise ProviderError(ErrorKind.AUTH, _scrub_text(f"HTTP {status} from {url}", secrets))
            if status == 429:
                _bump(stats, "throttled")
                self._consecutive_429 += 1
                if _breaker_trips(self._consecutive_429, retry_after):
                    # Do not retry into a block: stop this provider for the run.
                    reason = (
                        f"HTTP 429 from {url}"
                        if retry_after is None
                        else f"HTTP 429 from {url} (Retry-After {retry_after:.0f} s)"
                    )
                    self.block(
                        _scrub_text(f"{reason}; leaving this provider alone", secrets),
                        retry_after_s=retry_after,
                    )
                    raise ProviderError(ErrorKind.RATE_LIMITED, _scrub_text(reason, secrets))
            else:
                self._consecutive_429 = 0
            if status == 429 or 500 <= status < 600:
                if attempt < self._max_retries:
                    _bump(stats, "retries")
                    backoff = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                    self._sleep(
                        backoff
                        if retry_after is None
                        else min(retry_after, RETRY_AFTER_CAP_S)
                    )
                    attempt += 1
                    continue
                kind = ErrorKind.RATE_LIMITED if status == 429 else ErrorKind.HTTP
                raise ProviderError(kind, _scrub_text(f"HTTP {status} from {url}", secrets))
            if status >= 400:
                raise ProviderError(ErrorKind.HTTP, _scrub_text(f"HTTP {status} from {url}", secrets))
            return response


class OpContext:
    """Everything one provider+operation may do, plus its recorded exchanges."""

    def __init__(
        self,
        sender: Any,
        *,
        operation: Operation | None = None,
        credential: str | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._sender = sender
        self.provider_id = sender.provider_id
        self.operation = operation
        self.credential = credential
        self.max_calls = max_calls
        self.max_pages = max_pages
        self.calls_used = 0
        self.exchanges: list[RawExchange] = []
        self.truncated = False
        self.notes: list[str] = []
        self.pagination = Pagination()
        self.unresolved: dict[str, int] = {}
        self._call_stats: dict[str, int] = {"retries": 0, "throttled": 0}
        # The credential is scrubbed even if an adapter forgets to pass it as a secret.
        self._secrets: set[str] = {credential} if credential else set()

    def stats(self) -> dict[str, int]:
        """Calls, retries and 429s for this operation (timing is added by the engine)."""

        return {
            "calls": self.calls_used,
            "retries": self._call_stats.get("retries", 0),
            "throttled": self._call_stats.get("throttled", 0),
            "elapsed_ms": 0,
        }

    def secret_values(self) -> set[str]:
        """Credential values this context must keep out of every output."""

        return set(self._secrets)

    def __repr__(self) -> str:
        credential = "set" if self.credential else "none"
        return (
            f"OpContext(provider_id={self.provider_id!r}, operation={self.operation}, "
            f"credential={credential}, calls_used={self.calls_used})"
        )

    def mark_truncated(self, reason: str) -> None:
        """Call when paging stopped while more pages are known to exist."""

        self.truncated = True
        self.pagination.truncated = True
        self.pagination.reason = reason
        self.notes.append(f"truncated: {reason}")

    def note_pagination(
        self,
        page: int,
        *,
        size: int | None = None,
        total: int | None = None,
        retrieved: int | None = None,
    ) -> None:
        """Record what paging did, so "we stopped early" is visible in the result."""

        self.pagination.pages_fetched = max(self.pagination.pages_fetched, int(page))
        if size is not None:
            self.pagination.page_size = int(size)
        if total is not None:
            self.pagination.total_reported = int(total)
        if retrieved is not None:
            self.pagination.retrieved = int(retrieved)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def note_unresolved(self, kind: str, count: int = 1) -> None:
        """Record that the adapter did not follow a reference it could see.

        The engine turns matching missing checks into ``incomplete`` ("adapter did
        not resolve"), so an adapter gap never reads as a provider omission.
        """

        self.unresolved[kind] = self.unresolved.get(kind, 0) + int(count)
        self.notes.append(f"unresolved {kind}: {count} reference(s) not followed")

    def unresolved_counts(self) -> dict[str, int]:
        return dict(self.unresolved)

    def _spend_call(self) -> None:
        if self.calls_used >= self.max_calls:
            raise BudgetExceeded(f"operation budget of {self.max_calls} calls reached")
        self.calls_used += 1

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_params: Mapping[str, Any] | None = None,
        secret_headers: Mapping[str, str] | None = None,
        request_class: str = REQUEST_CLASS_DEFAULT,
    ) -> Any:
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            body=None,
            secret_params=secret_params,
            secret_headers=secret_headers,
            request_class=request_class,
        )

    def post_json(
        self,
        url: str,
        json_body: Any,
        headers: Mapping[str, str] | None = None,
        secret_headers: Mapping[str, str] | None = None,
        request_class: str = REQUEST_CLASS_DEFAULT,
    ) -> Any:
        return self._request(
            "POST",
            url,
            params=None,
            headers=headers,
            body=json.dumps(json_body),
            secret_params=None,
            secret_headers=secret_headers,
            request_class=request_class,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        body: str | None,
        secret_params: Mapping[str, str] | None,
        secret_headers: Mapping[str, str] | None,
        request_class: str = REQUEST_CLASS_DEFAULT,
    ) -> Any:
        public_params = {str(k): v for k, v in (params or {}).items()}
        secret_param_values = {str(k): str(v) for k, v in (secret_params or {}).items()}
        secret_header_values = {str(k): str(v) for k, v in (secret_headers or {}).items()}
        for value in secret_param_values.values():
            self._secrets.add(value)
        for value in secret_header_values.values():
            self._secrets.add(value)

        if getattr(self._sender, "replays_only", False):
            # A capture never contains secrets, so replay identifies a request by
            # its public parts only.
            request_params = dict(public_params)
            request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
            secrets_for_sender: set[str] = set()
        else:
            request_params = {**public_params, **secret_param_values}
            request_headers = {str(k): str(v) for k, v in (headers or {}).items()}
            request_headers.update(secret_header_values)
            secrets_for_sender = set(self._secrets)

        self._spend_call()
        started = time.monotonic()
        recorded_body = _scrub_text(body, self._secrets) if body is not None else None
        try:
            response = self._sender.send(
                method,
                url,
                params=request_params or None,
                headers=request_headers,
                body=body,
                secrets=secrets_for_sender,
                stats=self._call_stats,
                interval_s=self._sender.interval_for(request_class),
            )
        except ProviderError as exc:
            self.exchanges.append(
                RawExchange(
                    method=method,
                    url=url,
                    params=public_params,
                    request_body=recorded_body,
                    status=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    body=None,
                    error={"kind": exc.kind.value, "message": exc.message},
                )
            )
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            parsed = response.json()
        except (ValueError, TypeError):
            self.exchanges.append(
                RawExchange(
                    method=method,
                    url=url,
                    params=public_params,
                    request_body=recorded_body,
                    status=response.status_code,
                    elapsed_ms=elapsed_ms,
                    body=_scrub_text(response.text[:2000], self._secrets),
                )
            )
            raise ProviderError(
                ErrorKind.PARSE, _scrub_text(f"response from {url} was not JSON", self._secrets)
            ) from None

        self.exchanges.append(
            RawExchange(
                method=method,
                url=url,
                params=public_params,
                request_body=recorded_body,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
                body=_scrub_value(parsed, self._secrets),
                error=None,
            )
        )
        return parsed


__all__ = [
    "BudgetExceeded",
    "HttpClient",
    "OpContext",
    "ProviderHttp",
    "REQUEST_CLASS_DEFAULT",
    "REQUEST_CLASS_LOOKUP",
    "REQUEST_CLASS_SEARCH",
    "RETRY_AFTER_CAP_S",
    "RequestsTransport",
    "Transport",
    "TransportResponse",
    "body_digest",
    "exchange_key",
    "user_agent",
]
