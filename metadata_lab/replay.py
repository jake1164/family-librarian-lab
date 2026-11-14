"""Replay a previous capture without touching the network.

Replay exists so a matcher or adapter bug can be fixed without re-hitting rate
limited APIs, and so an old comparison can be reproduced exactly. A capture is
keyed on the *public* request identity only — secrets are never stored, and POST
bodies are not stored, so a GraphQL replay matches on URL and parameters.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .http import DEFAULT_MAX_CALLS, DEFAULT_MAX_PAGES, OpContext, TransportResponse, exchange_key
from .model import ErrorKind, Operation, ProviderError, RawExchange


class ReplayHttp:
    """Rebuilds a run from a previous result file. Never touches the network."""

    def __init__(self, exchanges: Mapping[tuple[str, str, str, str, str], RawExchange]) -> None:
        self._exchanges = dict(exchanges)

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> "ReplayHttp":
        collected: dict[tuple[str, str, str, str, str], RawExchange] = {}
        for case in result.get("cases", []) or []:
            for row in case.get("results", []) or []:
                provider_id = str(row.get("provider", ""))
                for raw in row.get("raw", []) or []:
                    exchange = RawExchange(
                        method=str(raw.get("method", "GET")),
                        url=str(raw.get("url", "")),
                        params=dict(raw.get("params") or {}),
                        request_body=raw.get("request_body"),
                        status=raw.get("status"),
                        elapsed_ms=int(raw.get("elapsed_ms", 0) or 0),
                        body=raw.get("body"),
                        error=raw.get("error"),
                    )
                    # The request body is part of the key: one GraphQL URL serves
                    # every query, so a body-less key would return the wrong
                    # response for a different query.
                    key = exchange_key(
                        provider_id,
                        exchange.method,
                        exchange.url,
                        exchange.params,
                        exchange.request_body,
                    )
                    collected[key] = exchange
        return cls(collected)

    def for_provider(self, provider_id: str) -> "_ReplayClient":
        return _ReplayClient(provider_id, self._exchanges)


class _ReplayClient:
    """``HttpClient``-shaped client that answers from captures."""

    replays_only = True

    def __init__(
        self,
        provider_id: str,
        exchanges: Mapping[tuple[str, str, str, str, str], RawExchange],
    ) -> None:
        self.provider_id = provider_id
        self.secrets_seen: set[str] = set()
        self._exchanges = exchanges

    def context(
        self,
        *,
        operation: Operation | None = None,
        credential: str | None = None,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> OpContext:
        return OpContext(
            self,
            operation=operation,
            credential=credential,
            max_calls=max_calls,
            max_pages=max_pages,
        )

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
    ) -> TransportResponse:
        key = exchange_key(self.provider_id, method, url, params, body)
        exchange = self._exchanges.get(key)
        if exchange is None:
            # No body-less fallback: answering a different query's capture would
            # be a silent wrong result rather than a visible miss.
            raise ProviderError(ErrorKind.NETWORK, "not in capture")
        if exchange.error:
            kind = ErrorKind(exchange.error.get("kind", ErrorKind.NETWORK.value))
            raise ProviderError(kind, exchange.error.get("message", "not in capture"))
        text = exchange.body if isinstance(exchange.body, str) else json.dumps(exchange.body)
        return TransportResponse(status_code=exchange.status or 200, headers={}, text=text)


__all__ = ["ReplayHttp"]
