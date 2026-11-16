"""The optional response cache: bounded, labelled, expiring, and never used for freshness facts."""

from __future__ import annotations

import json

import pytest

from metadata_lab import registry
from metadata_lab.cache import ResponseCache
from metadata_lab.cases import Case, Reference
from metadata_lab.engine import _cache_allowed, _evidence_source
from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.measurements import fact_tables
from metadata_lab.model import Operation, RawExchange
from metadata_lab.selftest.runner import run_all


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class CountingTransport:
    def __init__(self, body: str = '{"ok": true}', status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.body = body
        self.status = status

    def send(self, method, url, *, params, headers, body, timeout):
        self.calls.append((method, url, dict(params or {})))
        return TransportResponse(self.status, {}, self.body)


def _client(transport, cache, provider_id="openlibrary"):
    return HttpClient(provider_id=provider_id, transport=transport, min_interval_s=0.0, cache=cache)


# --- the store ---------------------------------------------------------------------------


def test_a_fresh_entry_is_served_and_an_expired_one_is_removed(tmp_path):
    clock = Clock()
    cache = ResponseCache(tmp_path, ttl_s=60.0, clock=clock)
    key = cache.key("p", "GET", "https://x/y", {"q": "a"}, None)

    cache.put(key, status=200, text='{"a": 1}')
    clock.now += 30
    assert cache.get(key).text == '{"a": 1}'
    clock.now += 31
    assert cache.get(key) is None
    assert not list(tmp_path.glob("*.json")), "an expired entry must be deleted, not kept"


def test_only_successful_responses_are_stored(tmp_path):
    cache = ResponseCache(tmp_path, clock=Clock())
    key = cache.key("p", "GET", "https://x", {}, None)

    for status in (301, 400, 404, 429, 500):
        cache.put(key, status=status, text="{}")

    assert cache.get(key) is None
    assert cache.summary()["entries"] == 0


def test_the_key_ignores_parameter_order_but_not_the_body_or_provider(tmp_path):
    key = ResponseCache.key
    assert key("p", "GET", "u", {"a": 1, "b": 2}, None) == key("p", "get", "u", {"b": 2, "a": 1}, None)
    assert key("p", "POST", "u", {}, "q1") != key("p", "POST", "u", {}, "q2")
    assert key("p", "GET", "u", {}, None) != key("other", "GET", "u", {}, None)


def test_the_cache_is_bounded_by_entries_and_drops_the_oldest(tmp_path):
    import os

    cache = ResponseCache(tmp_path, max_entries=3, clock=Clock())
    keys = [cache.key("p", "GET", f"https://x/{n}", {}, None) for n in range(5)]
    for age, key in enumerate(keys):
        cache.put(key, status=200, text="{}")
        os.utime(cache._path(key), (1000 + age, 1000 + age))
        cache._evict()

    assert cache.summary()["entries"] == 3
    assert cache.get(keys[0]) is None and cache.get(keys[1]) is None
    assert cache.get(keys[4]) is not None


def test_a_corrupt_entry_is_a_miss_and_is_removed(tmp_path):
    cache = ResponseCache(tmp_path, clock=Clock())
    key = cache.key("p", "GET", "https://x", {}, None)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")

    assert cache.get(key) is None
    assert not (tmp_path / f"{key}.json").exists()


# --- the HTTP layer -------------------------------------------------------------------------


def test_a_repeated_request_is_served_from_cache_and_labelled(tmp_path):
    transport = CountingTransport()
    client = _client(transport, ResponseCache(tmp_path, clock=Clock()))
    first = client.context()
    second = client.context()

    assert first.get_json("https://x/search", params={"q": "a"}) == {"ok": True}
    assert second.get_json("https://x/search", params={"q": "a"}) == {"ok": True}

    assert len(transport.calls) == 1
    assert first.exchanges[0].source == "live" and first.exchanges[0].fetched_at is None
    assert second.exchanges[0].source == "cache" and second.exchanges[0].fetched_at
    assert second.stats()["cache_hits"] == 1
    assert second.stats()["calls"] == 1, "a cached answer still uses the operation's call budget"


def test_allow_cache_false_always_goes_to_the_provider(tmp_path):
    transport = CountingTransport()
    client = _client(transport, ResponseCache(tmp_path, clock=Clock()))
    for _ in range(2):
        ctx = client.context()
        ctx.allow_cache = False
        ctx.get_json("https://x/search", params={"q": "a"})

    assert len(transport.calls) == 2
    assert not list(tmp_path.glob("*.json")), "a bypassed request must not populate the cache either"


def test_a_request_with_a_secret_parameter_is_never_cached(tmp_path):
    transport = CountingTransport()
    client = _client(transport, ResponseCache(tmp_path, clock=Clock()), provider_id="googlebooks")
    for _ in range(2):
        client.context(credential="SECRET-KEY-VALUE").get_json(
            "https://x/volumes", params={"q": "a"}, secret_params={"key": "SECRET-KEY-VALUE"}
        )

    assert len(transport.calls) == 2
    assert not list(tmp_path.glob("*.json"))


def test_an_error_response_is_not_cached(tmp_path):
    transport = CountingTransport(body="{}", status=500)
    client = _client(transport, ResponseCache(tmp_path, clock=Clock()))
    client = HttpClient(
        provider_id="p", transport=transport, min_interval_s=0.0, max_retries=0, cache=client.cache
    )

    with pytest.raises(Exception):
        client.context().get_json("https://x/a")

    assert not list(tmp_path.glob("*.json"))


def test_stored_bodies_never_contain_the_credential(tmp_path):
    transport = CountingTransport(body='{"echo": "TOKEN-123"}')
    cache = ResponseCache(tmp_path, clock=Clock())
    client = _client(transport, cache, provider_id="hardcover")

    client.context(credential="TOKEN-123").get_json("https://x/q", params={"q": "a"})

    stored = "".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))
    assert "TOKEN-123" not in stored


def test_the_registry_never_gives_the_cache_to_an_adapter_that_forbids_it(tmp_path):
    adapters = registry.discover()
    cache = ResponseCache(tmp_path)
    factory = registry.http_factory(adapters, cache)

    assert adapters["googlebooks"].cache_policy == "none"
    assert factory("googlebooks").cache is None
    assert factory("openlibrary").cache is cache
    assert registry.http_factory(adapters)("openlibrary").cache is None, "the cache is off by default"


# --- the engine --------------------------------------------------------------------------------


def _case(expect):
    return Case(
        id="c",
        description="",
        reference=Reference(source="s"),
        input={"series": "S"},
        operations=(Operation.SERIES_MEMBERS,),
        expect=expect,
    )


def test_a_freshness_fixture_is_never_read_from_the_cache():
    plain = {"series_members": {"series": "S", "members": [{"title": "T", "position": "1"}]}}
    dated = {"series_members": {"series": "S", "members": [{"title": "T", "published": "2026-01-01"}]}}
    first_published = {"book_by_title": {"title": "T", "first_published": "2020"}}

    assert _cache_allowed(_case(plain), Operation.SERIES_MEMBERS, "2026-11-14") is True
    assert _cache_allowed(_case(dated), Operation.SERIES_MEMBERS, "2026-11-14") is False
    assert _cache_allowed(_case(first_published), Operation.BOOK_BY_TITLE, "2026-11-14") is False
    assert _cache_allowed(_case({}), Operation.SERIES_MEMBERS, None) is True


def test_evidence_source_distinguishes_live_cache_mixed_and_replay():
    def exchange(source, fetched=None):
        return RawExchange(method="GET", url="u", source=source, fetched_at=fetched)

    assert _evidence_source([]) == (None, None)
    assert _evidence_source([exchange("live")]) == ("live", None)
    assert _evidence_source([exchange("cache", "2026-11-16T01:00:00Z")]) == ("cache", "2026-11-16T01:00:00Z")
    assert _evidence_source(
        [exchange("live"), exchange("cache", "2026-11-16T02:00:00Z"), exchange("cache", "2026-11-16T01:00:00Z")]
    ) == ("mixed", "2026-11-16T01:00:00Z")
    assert _evidence_source([exchange("replay")]) == ("replay", None)


def test_fact_tables_count_rows_by_how_their_evidence_was_obtained():
    cases = [
        {
            "id": "c",
            "operations": ["series_members"],
            "expect": {"series_members": {"series": "S"}},
            "results": [
                {
                    "provider": "p",
                    "operation": "series_members",
                    "mode": "independent",
                    "outcome": "ok",
                    "data_source": "cache",
                    "checks": [{"name": "series.name", "outcome": "ok", "dimension": "coverage"}],
                }
            ],
        }
    ]

    profile = fact_tables(cases, ["p"], as_of="2026-11-14")["modes"]["independent"]["providers"]["p"]

    assert profile["evidence"] == {"cache": 1}


# --- selftest meta-tests --------------------------------------------------------------------------


def test_h30_detects_a_cache_that_answers_freshness_fixtures(monkeypatch):
    monkeypatch.setattr("metadata_lab.engine._cache_allowed", lambda case, operation, as_of: True)

    summary = run_all(only=["H30"])

    assert summary["failed"] == 1, "H30 passed although a freshness fixture was served from the cache"


def test_h30_detects_a_cache_that_ignores_its_time_to_live(monkeypatch):
    original = ResponseCache.get

    def ignore_ttl(self, key):
        self.ttl_s = float("inf")
        return original(self, key)

    monkeypatch.setattr(ResponseCache, "get", ignore_ttl)

    summary = run_all(only=["H30"])

    assert summary["failed"] == 1, "H30 passed although an expired answer was served"


def test_h30_detects_a_cache_that_hides_where_the_answer_came_from(monkeypatch):
    monkeypatch.setattr("metadata_lab.engine._evidence_source", lambda exchanges: ("live", None))

    summary = run_all(only=["H30"])

    assert summary["failed"] == 1, "H30 passed although a cached row was labelled live"


def test_a_cache_hit_does_not_change_the_semantic_result(tmp_path):
    """Live and cached answers carry the same facts (H30 asserts this end to end)."""

    transport = CountingTransport(body=json.dumps({"ok": True}))
    client = _client(transport, ResponseCache(tmp_path, clock=Clock()))
    live = client.context().get_json("https://x/a")
    cached = client.context().get_json("https://x/a")

    assert live == cached
