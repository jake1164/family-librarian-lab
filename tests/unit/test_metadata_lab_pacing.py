"""Pacing, the 429 circuit breaker and the persisted cool-down record.

Everything here is offline: a scripted transport and a virtual clock, so no test
waits for a real interval and none touches a provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from metadata_lab import cli
from metadata_lab.cases import Case, Reference, Suite
from metadata_lab.engine import run_suite
from metadata_lab.http import REQUEST_CLASS_SEARCH, HttpClient, TransportResponse
from metadata_lab.model import AuthorRecord, Operation, ProviderError, SeriesRecord
from metadata_lab.pacing import CooldownStore
from metadata_lab.providers.base import ProviderAdapter
from metadata_lab.providers.inventaire import InventaireAdapter
from metadata_lab.registry import http_factory

URL = "http://127.0.0.1:9/series"
LOOKUP_URL = "http://127.0.0.1:9/entities/by-uris"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ScriptedTransport:
    """Answers from a list; the last answer repeats. Records every request."""

    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[str] = []

    def send(self, method, url, *, params, headers, body, timeout):
        self.requests.append(url)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def ok(payload: dict | None = None) -> TransportResponse:
    return TransportResponse(200, {}, json.dumps(payload or {"name": "The Empyrean"}))


def throttled(retry_after: str | None = None) -> TransportResponse:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return TransportResponse(429, headers, "{}")


def client(transport, clock: Clock, **kwargs) -> HttpClient:
    kwargs.setdefault("min_interval_s", 0.0)
    return HttpClient(
        provider_id="paced",
        transport=transport,
        sleep=clock.sleep,
        clock=clock.clock,
        **kwargs,
    )


# --- pacing ------------------------------------------------------------------------


def test_the_base_interval_is_provider_wide_not_per_operation():
    clock = Clock()
    http = client(ScriptedTransport(ok()), clock, min_interval_s=2.0)

    http.context(operation=Operation.SERIES_MEMBERS).get_json(URL)
    http.context(operation=Operation.AUTHOR_WORKS).get_json(URL)

    # The second call waits the base interval even though it is another operation.
    assert clock.slept == [2.0]


def test_a_request_class_overrides_the_base_interval():
    clock = Clock()
    http = client(
        ScriptedTransport(ok()),
        clock,
        min_interval_s=3.0,
        request_intervals_s={REQUEST_CLASS_SEARCH: 10.0},
    )

    http.context(operation=Operation.BOOK_BY_TITLE).get_json(URL, request_class=REQUEST_CLASS_SEARCH)
    http.context(operation=Operation.BOOK_BY_ISBN).get_json(LOOKUP_URL)

    # Search was the first call, so the lookup only waits its own 3 s pace.
    assert clock.slept == [3.0]


def test_a_search_after_a_lookup_waits_the_search_interval():
    clock = Clock()
    http = client(
        ScriptedTransport(ok()),
        clock,
        min_interval_s=3.0,
        request_intervals_s={REQUEST_CLASS_SEARCH: 10.0},
    )

    http.context().get_json(LOOKUP_URL)
    http.context().get_json(URL, request_class=REQUEST_CLASS_SEARCH)

    assert clock.slept == [10.0]


def test_the_inventaire_adapter_paces_searches_slower_than_lookups():
    adapter = InventaireAdapter()

    assert adapter.interval_for(REQUEST_CLASS_SEARCH) == pytest.approx(10.0)
    assert adapter.interval_for() == pytest.approx(3.0)
    assert adapter.cooldown_default_s == pytest.approx(60 * 60.0)


def test_the_registry_builds_a_client_from_the_adapter_declaration():
    class Paced(ProviderAdapter):
        id = "paced"
        display_name = "Paced"
        min_interval_s = 4.0
        request_intervals_s = {REQUEST_CLASS_SEARCH: 9.0}

    http = http_factory({"paced": Paced})("paced")

    assert http.interval_for() == pytest.approx(4.0)
    assert http.interval_for(REQUEST_CLASS_SEARCH) == pytest.approx(9.0)


# --- the 429 circuit breaker ---------------------------------------------------------


def test_a_long_retry_after_trips_the_breaker_without_retrying():
    clock = Clock()
    transport = ScriptedTransport(throttled("600"), ok())
    http = client(transport, clock)

    with pytest.raises(ProviderError) as info:
        http.context().get_json(URL)

    assert info.value.kind.value == "rate_limited"
    assert http.blocked is True
    assert http.blocked_retry_after_s == 600.0
    assert len(transport.requests) == 1
    assert clock.slept == []


def test_two_consecutive_429s_trip_the_breaker():
    clock = Clock()
    transport = ScriptedTransport(throttled("1"), throttled("1"), ok())
    http = client(transport, clock)

    with pytest.raises(ProviderError):
        http.context().get_json(URL)

    assert len(transport.requests) == 2
    assert http.blocked is True
    # Only the first 429 was waited out; the second stopped the provider.
    assert clock.slept == [1.0]


def test_one_short_429_is_still_retried():
    clock = Clock()
    transport = ScriptedTransport(throttled("2"), ok())
    http = client(transport, clock)

    assert http.context().get_json(URL) == {"name": "The Empyrean"}
    assert http.blocked is False
    assert clock.slept == [2.0]


def test_a_blocked_client_refuses_before_sending():
    clock = Clock()
    transport = ScriptedTransport(ok())
    http = client(transport, clock)
    http.block("paced is cooling down")

    with pytest.raises(ProviderError) as info:
        http.context().get_json(URL)

    assert "cooling down" in info.value.message
    assert transport.requests == []


# --- the cool-down file --------------------------------------------------------------


def _store(tmp_path: Path, clock: datetime | None = None) -> CooldownStore:
    fixed = clock or datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    return CooldownStore.under(tmp_path / "results", clock=lambda: fixed)


def test_the_cooldown_file_round_trips(tmp_path):
    store = _store(tmp_path)

    store.record("inventaire", retry_after_s=1800)

    reloaded = CooldownStore.under(
        tmp_path / "results", clock=lambda: datetime(2026, 11, 15, 12, 10, tzinfo=timezone.utc)
    )
    entry = reloaded.load()["inventaire"]
    assert entry.reason == "rate_limited"
    assert entry.until == datetime(2026, 11, 15, 12, 30, tzinfo=timezone.utc)
    assert reloaded.minutes_remaining("inventaire") == 20
    payload = json.loads((tmp_path / "results" / "cooldowns.json").read_text(encoding="utf-8"))
    assert payload["providers"]["inventaire"]["until"] == "2026-11-15T12:30:00Z"
    assert set(payload["providers"]["inventaire"]) == {
        "provider",
        "until",
        "reason",
        "observed_at",
    }


def test_a_missing_file_reads_as_nothing_cooling(tmp_path):
    store = _store(tmp_path)

    assert store.load() == {}
    assert store.active("inventaire") is None
    assert store.note_for("inventaire") is None


def test_a_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "results" / "cooldowns.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all", encoding="utf-8")
    store = _store(tmp_path)

    assert store.load() == {}
    assert store.active("inventaire") is None
    # A run still records into it rather than failing.
    store.record("inventaire", default_s=60)
    assert store.active("inventaire") is not None


def test_a_non_object_file_and_a_broken_entry_are_ignored(tmp_path):
    path = tmp_path / "results" / "cooldowns.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    store = _store(tmp_path)
    assert store.load() == {}

    path.write_text(
        json.dumps({"schema": "x", "providers": {"broken": {"until": "whenever"}}}),
        encoding="utf-8",
    )
    assert store.load() == {}


def test_the_default_cooldown_is_used_without_a_retry_after(tmp_path):
    store = _store(tmp_path)

    store.record("paced", default_s=15 * 60)

    assert store.active("paced").until == datetime(2026, 11, 15, 12, 15, tzinfo=timezone.utc)


def test_an_expired_entry_is_cleared_and_a_longer_one_is_kept(tmp_path):
    store = _store(tmp_path)
    store.record("paced", retry_after_s=60)  # 12:01

    assert store.clear_expired() == []

    store.record("paced", retry_after_s=5)  # would be shorter: the later block wins
    assert store.active("paced").until == datetime(2026, 11, 15, 12, 1, tzinfo=timezone.utc)

    later = CooldownStore.under(
        tmp_path / "results", clock=lambda: datetime(2026, 11, 15, 12, 5, tzinfo=timezone.utc)
    )
    assert later.clear_expired() == ["paced"]
    assert later.load() == {}


def test_the_note_carries_the_remaining_time(tmp_path):
    store = _store(tmp_path)
    store.record("inventaire", retry_after_s=25 * 60)

    note = store.note_for("inventaire")

    assert note is not None and "25 min remaining" in note
    assert "inventaire" in note


# --- pacing/breaker through the engine ------------------------------------------------


class Tiny(ProviderAdapter):
    id = "tiny"
    display_name = "Tiny"
    VERIFIED_LIVE = True
    min_interval_s = 0.0
    cooldown_default_s = 15 * 60.0

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        payload = ctx.get_json(URL)
        return [SeriesRecord(provider_id="s1", name=str(payload.get("name") or ""))]

    def author_works(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        payload = ctx.get_json(URL)
        return [AuthorRecord(provider_id="a1", name=str(payload.get("name") or ""), works=["Wing"])]


class Other(ProviderAdapter):
    id = "other"
    display_name = "Other"
    VERIFIED_LIVE = True
    min_interval_s = 0.0

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        payload = ctx.get_json(URL)
        return [SeriesRecord(provider_id="s1", name=str(payload.get("name") or ""))]

    def author_works(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        payload = ctx.get_json(URL)
        return [AuthorRecord(provider_id="a1", name=str(payload.get("name") or ""), works=["Wing"])]


def _suite(*operations: Operation) -> Suite:
    case = Case(
        id="c1",
        description="pacing test",
        reference=Reference(source="unit test", verified_on=None),
        input={"series": "The Empyrean"},
        operations=tuple(operations or (Operation.SERIES_MEMBERS,)),
        traversal=False,
        options={},
        expect={},
        verified=False,
    )
    return Suite(name="pacing", version=1, cases=(case,), source="memory", sha256="0" * 64)


def _run(transports: dict[str, object], clock: Clock, **kwargs):
    adapters = {"tiny": Tiny, "other": Other}
    return run_suite(
        _suite(Operation.SERIES_MEMBERS, Operation.AUTHOR_WORKS),
        list(transports),
        None,
        http_factory=lambda provider_id: HttpClient(
            provider_id=provider_id,
            transport=transports[provider_id],
            min_interval_s=0.0,
            sleep=clock.sleep,
            clock=clock.clock,
        ),
        adapters=adapters,
        **kwargs,
    )


def _rows(document) -> dict[tuple[str, str], dict]:
    return {
        (row["provider"], row["operation"]): row for row in document["cases"][0]["results"]
    }


def test_later_rows_are_cooling_down_while_other_providers_continue(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    transports = {
        "tiny": ScriptedTransport(throttled("600")),
        "other": ScriptedTransport(ok()),
    }

    document = _run(transports, clock, cooldowns=store)
    rows = _rows(document)

    assert rows[("tiny", "series_members")]["error"]["kind"] == "rate_limited"
    blocked = rows[("tiny", "author_works")]
    assert blocked["outcome"] == "not_run"
    assert blocked["reason"] == "cooling_down"
    assert rows[("other", "series_members")]["outcome"] == "ok"
    assert rows[("other", "author_works")]["outcome"] == "ok"
    # One call for the 429, none after it.
    assert len(transports["tiny"].requests) == 1
    assert "tiny" in store.cooling()
    # The default cool-down was used because the 429 carried no Retry-After.
    assert store.active("tiny").reason == "rate_limited"


def test_a_recorded_cooldown_skips_the_provider_on_the_next_run(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    store.record("tiny", retry_after_s=1800)
    transports = {"tiny": ScriptedTransport(ok()), "other": ScriptedTransport(ok())}

    document = _run(transports, clock, cooldowns=store)
    rows = _rows(document)

    assert rows[("tiny", "series_members")]["reason"] == "cooling_down"
    assert any("min remaining" in note for note in rows[("tiny", "series_members")]["notes"])
    assert transports["tiny"].requests == []
    assert rows[("other", "series_members")]["outcome"] == "ok"


def test_a_skipped_cooled_provider_keeps_its_original_deadline(tmp_path):
    """Skipping a provider must not restart the countdown from the default."""

    clock = Clock()
    store = _store(tmp_path)
    recorded = store.record("tiny", retry_after_s=300)
    transports = {"tiny": ScriptedTransport(ok()), "other": ScriptedTransport(ok())}

    _run(transports, clock, cooldowns=store)

    assert store.load()["tiny"].until == recorded.until


def test_ignore_cooldown_calls_the_provider_anyway(tmp_path):
    clock = Clock()
    store = _store(tmp_path)
    store.record("tiny", retry_after_s=1800)
    transports = {"tiny": ScriptedTransport(ok()), "other": ScriptedTransport(ok())}

    document = _run(transports, clock, cooldowns=store, ignore_cooldown=True)
    rows = _rows(document)

    assert rows[("tiny", "series_members")]["outcome"] == "ok"
    assert len(transports["tiny"].requests) == 2


def test_the_cli_run_flag_reaches_the_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_adapters", lambda: {"tiny": Tiny})
    monkeypatch.setattr(
        cli,
        "_http_factory",
        lambda _adapters: lambda provider_id: HttpClient(
            provider_id=provider_id, transport=ScriptedTransport(ok()), min_interval_s=0.0
        ),
    )
    monkeypatch.setattr(cli, "_resolve_suite", lambda _target: _suite())
    CooldownStore.under(tmp_path).record("tiny", retry_after_s=1800)

    assert cli.main(["run", "unused", "--out", str(tmp_path)]) == 0
    skipped = capsys.readouterr().out
    assert "pacing estimate" in skipped
    assert "cooling_down" in skipped

    assert cli.main(["run", "unused", "--out", str(tmp_path), "--ignore-cooldown"]) == 0
    overridden = capsys.readouterr().out
    assert "pacing estimate" in overridden
    assert "cooling_down" not in overridden
