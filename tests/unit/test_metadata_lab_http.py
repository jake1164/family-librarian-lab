"""HTTP pipeline: retries, error classes, budget, redaction and offline replay."""

from __future__ import annotations

import json

import pytest

from metadata_lab.http import (
    RETRY_AFTER_CAP_S,
    BudgetExceeded,
    HttpClient,
    TransportResponse,
    exchange_key,
    user_agent,
)
from metadata_lab.model import ErrorKind, ProviderError
from metadata_lab.replay import ReplayHttp

URL = "https://api.example.org/v1/volumes"


class FakeTransport:
    """Injected transport; no global patching, no network ever."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, method, url, *, params, headers, body, timeout):
        self.requests.append(
            {"method": method, "url": url, "params": params, "headers": headers, "body": body}
        )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client(transport, **kwargs) -> HttpClient:
    kwargs.setdefault("min_interval_s", 0.0)
    return HttpClient(provider_id="fake", transport=transport, sleep=lambda _s: None, **kwargs)


def test_retries_server_error_then_succeeds():
    transport = FakeTransport(
        [
            TransportResponse(503, {}, "{}"),
            TransportResponse(200, {}, '{"ok": true}'),
        ]
    )
    ctx = client(transport).context()

    assert ctx.get_json(URL, params={"q": "x"}) == {"ok": True}
    assert len(transport.requests) == 2
    assert len(ctx.exchanges) == 1
    assert ctx.exchanges[0].status == 200


def test_retry_cap_then_http_error():
    transport = FakeTransport([TransportResponse(500, {}, "{}") for _ in range(3)])
    ctx = client(transport).context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL)

    assert info.value.kind is ErrorKind.HTTP
    assert len(transport.requests) == 3  # initial attempt + 2 retries
    assert ctx.exchanges[0].status is None
    assert ctx.exchanges[0].error["kind"] == "http"


def test_rate_limited_after_retries():
    transport = FakeTransport([TransportResponse(429, {"Retry-After": "1"}, "{}") for _ in range(3)])
    ctx = client(transport).context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL)

    assert info.value.kind is ErrorKind.RATE_LIMITED
    # Two consecutive 429s trip the breaker: the third attempt is never sent.
    assert len(transport.requests) == 2


def test_retry_after_is_honoured_for_a_short_wait():
    delays: list[float] = []
    transport = FakeTransport(
        [
            TransportResponse(429, {"Retry-After": "7"}, "{}"),
            TransportResponse(200, {}, "{}"),
        ]
    )
    http = HttpClient(
        provider_id="fake",
        transport=transport,
        min_interval_s=0.0,
        sleep=delays.append,
    )

    http.context().get_json(URL)

    assert delays == [7.0]
    assert max(delays) <= RETRY_AFTER_CAP_S
    assert http.blocked is False


def test_a_retry_after_beyond_the_cap_is_not_slept_through():
    delays: list[float] = []
    transport = FakeTransport(
        [
            TransportResponse(429, {"Retry-After": "999"}, "{}"),
            TransportResponse(200, {}, "{}"),
        ]
    )
    http = HttpClient(
        provider_id="fake",
        transport=transport,
        min_interval_s=0.0,
        sleep=delays.append,
    )

    with pytest.raises(ProviderError) as info:
        http.context().get_json(URL)

    assert info.value.kind is ErrorKind.RATE_LIMITED
    assert delays == []
    assert len(transport.requests) == 1
    assert http.blocked is True
    assert http.blocked_retry_after_s == 999.0


def test_auth_error_is_not_retried():
    transport = FakeTransport([TransportResponse(401, {}, "{}")])
    ctx = client(transport).context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL)

    assert info.value.kind is ErrorKind.AUTH
    assert len(transport.requests) == 1


def test_timeout_is_classified_and_retried_then_raised():
    transport = FakeTransport([TimeoutError("read timed out") for _ in range(3)])
    ctx = client(transport).context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL)

    assert info.value.kind is ErrorKind.TIMEOUT
    assert len(transport.requests) == 3


def test_budget_exceeded_after_max_calls():
    transport = FakeTransport([TransportResponse(200, {}, "{}") for _ in range(3)])
    ctx = client(transport).context(max_calls=2)

    ctx.get_json(URL)
    ctx.get_json(URL)
    with pytest.raises(BudgetExceeded):
        ctx.get_json(URL)

    assert len(transport.requests) == 2
    assert ctx.calls_used == 2


def test_non_json_body_is_a_parse_error():
    transport = FakeTransport([TransportResponse(200, {}, "<html>not json</html>")])
    ctx = client(transport).context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL)

    assert info.value.kind is ErrorKind.PARSE
    assert ctx.exchanges[0].body.startswith("<html>")


def test_mark_truncated_records_a_note():
    ctx = client(FakeTransport([])).context()

    ctx.mark_truncated("stopped at page 3 of 9")

    assert ctx.truncated is True
    assert any("stopped at page 3" in note for note in ctx.notes)


def test_user_agent_identifies_the_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FAMILY_LIBRARIAN_LAB_METADATA_CONTACT", raising=False)

    assert user_agent() == "family-librarian-metadata-lab/0.1.0"


def test_user_agent_includes_opt_in_contact(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAMILY_LIBRARIAN_LAB_METADATA_CONTACT", "lab@example.org")

    assert user_agent().endswith("(lab@example.org)")


# --- redaction (the critical ones) -----------------------------------------------


def test_request_body_is_recorded_and_scrubbed():
    secret = "SECRET-12345"
    transport = FakeTransport([TransportResponse(200, {}, "{}")])
    ctx = client(transport).context(credential=secret)

    ctx.post_json(
        URL,
        {"query": "query { me { id } }", "echo": secret},
        secret_headers={"Authorization": f"Bearer {secret}"},
    )

    recorded = ctx.exchanges[0].request_body
    assert recorded is not None
    assert secret not in recorded
    assert "***" in recorded


def test_planted_secret_never_reaches_exchanges_errors_or_json():
    secret = "SECRET-12345"
    echoed_body = json.dumps({"key": secret, "data": {"nested": [secret]}})
    transport = FakeTransport(
        [
            TransportResponse(200, {}, echoed_body),
            ConnectionError(f"failed to reach {URL}?key={secret}"),
        ]
    )
    ctx = client(transport, max_retries=0).context(credential=secret)

    ctx.get_json(
        URL,
        params={"q": "visible"},
        headers={"X-Public": "1"},
        secret_params={"key": secret},
        secret_headers={"Authorization": f"Bearer {secret}"},
    )
    with pytest.raises(ProviderError) as info:
        ctx.get_json(
            URL,
            secret_params={"key": secret},
            secret_headers={"Authorization": f"Bearer {secret}"},
        )

    dumped = json.dumps([exchange.to_dict() for exchange in ctx.exchanges], default=str)
    assert secret not in dumped
    assert secret not in info.value.message
    assert secret not in repr(info.value)
    assert secret not in repr(ctx)
    # The secret really was sent (it is only absent from what we *store*).
    assert transport.requests[0]["params"]["key"] == secret
    assert transport.requests[0]["headers"]["Authorization"] == f"Bearer {secret}"
    # Public parameters survive for debugging.
    assert ctx.exchanges[0].params == {"q": "visible"}
    # And the redaction is visible rather than silent.
    assert "***" in dumped


# --- replay ----------------------------------------------------------------------


def _result_document_with_one_exchange() -> dict:
    return {
        "schema": "metadata-lab.result/1",
        "cases": [
            {
                "id": "c1",
                "results": [
                    {
                        "provider": "fake",
                        "raw": [
                            {
                                "method": "GET",
                                "url": URL,
                                "params": {"q": "x"},
                                "status": 200,
                                "elapsed_ms": 12,
                                "body": {"items": [{"title": "Fourth Wing"}]},
                                "error": None,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_replay_returns_recorded_bodies():
    replay = ReplayHttp.from_result(_result_document_with_one_exchange())
    ctx = replay.for_provider("fake").context()

    assert ctx.get_json(URL, params={"q": "x"}) == {"items": [{"title": "Fourth Wing"}]}
    assert ctx.exchanges[0].status == 200


def test_replay_refuses_unknown_requests_without_network():
    replay = ReplayHttp.from_result(_result_document_with_one_exchange())
    ctx = replay.for_provider("fake").context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json("https://api.example.org/elsewhere")

    assert info.value.kind is ErrorKind.NETWORK
    assert info.value.message == "not in capture"


def test_replay_reproduces_a_recorded_error():
    document = _result_document_with_one_exchange()
    document["cases"][0]["results"][0]["raw"][0].update(
        {"status": None, "body": None, "error": {"kind": "rate_limited", "message": "HTTP 429"}}
    )
    replay = ReplayHttp.from_result(document)
    ctx = replay.for_provider("fake").context()

    with pytest.raises(ProviderError) as info:
        ctx.get_json(URL, params={"q": "x"})

    assert info.value.kind is ErrorKind.RATE_LIMITED


def test_exchange_key_is_order_insensitive_for_params():
    first = exchange_key("p", "get", URL, {"a": 1, "b": 2}, None)
    second = exchange_key("p", "GET", URL, {"b": 2, "a": 1}, None)

    assert first == second
    assert exchange_key("p", "GET", URL, {"a": 1}, "body") != first


GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
QUERY_A = {"query": "query GetSeries { series(limit: 1) { id } }"}
QUERY_B = {"query": "query GetBooks { books(limit: 1) { id } }"}


def _graphql_result_document() -> dict:
    """Two POSTs to one URL: only the request body tells them apart."""

    return {
        "schema": "metadata-lab.result/1",
        "cases": [
            {
                "id": "c1",
                "results": [
                    {
                        "provider": "hardcover",
                        "raw": [
                            {
                                "method": "POST",
                                "url": GRAPHQL_URL,
                                "params": {},
                                "request_body": json.dumps(QUERY_A),
                                "status": 200,
                                "elapsed_ms": 5,
                                "body": {"data": {"series": [{"id": 1}]}},
                                "error": None,
                            },
                            {
                                "method": "POST",
                                "url": GRAPHQL_URL,
                                "params": {},
                                "request_body": json.dumps(QUERY_B),
                                "status": 200,
                                "elapsed_ms": 6,
                                "body": {"data": {"books": [{"id": 2}]}},
                                "error": None,
                            },
                        ],
                    }
                ],
            }
        ],
    }


def test_replay_distinguishes_two_post_bodies_on_one_url():
    replay = ReplayHttp.from_result(_graphql_result_document())

    first = replay.for_provider("hardcover").context().post_json(GRAPHQL_URL, QUERY_A)
    second = replay.for_provider("hardcover").context().post_json(GRAPHQL_URL, QUERY_B)

    assert first == {"data": {"series": [{"id": 1}]}}
    assert second == {"data": {"books": [{"id": 2}]}}


def test_replay_does_not_fall_back_to_a_body_less_key():
    replay = ReplayHttp.from_result(_graphql_result_document())
    ctx = replay.for_provider("hardcover").context()

    with pytest.raises(ProviderError) as info:
        ctx.post_json(GRAPHQL_URL, {"query": "query SomethingElse { authors { id } }"})

    assert info.value.kind is ErrorKind.NETWORK
    assert info.value.message == "not in capture"
    assert ctx.exchanges[0].error is not None
