"""Model contract: enums, record kinds and JSON-friendly serialisation."""

from __future__ import annotations

import json

from metadata_lab.model import (
    AuthorRecord,
    BookRecord,
    ErrorKind,
    Operation,
    Outcome,
    ProviderError,
    Query,
    RawExchange,
    SeriesMember,
    SeriesRecord,
    SeriesRef,
)


def test_operation_values_match_the_plan():
    assert [op.value for op in Operation] == [
        "book_by_title",
        "book_by_isbn",
        "series_for_book",
        "series_members",
        "author_resolve",
        "author_works",
        "author_series",
    ]


def test_outcome_vocabulary_is_exactly_the_short_plan():
    assert [o.value for o in Outcome] == [
        "ok",
        "differs_from_reference",
        "unsupported",
        "not_implemented",
        "not_configured",
        "not_found",
        "partial",
        "incomplete",
        "ambiguous",
        "needs_review",
        "error",
        "not_run",
    ]


def test_controlled_vocabularies():
    from metadata_lab.model import FORMATS, NOT_RUN_REASONS, RECORD_LEVELS, WORK_TYPES

    assert "novel" in WORK_TYPES and "unknown" in WORK_TYPES
    assert "audiobook" in FORMATS
    assert RECORD_LEVELS == {"work", "edition", "unknown"}
    # cooling_down: the provider is in a rate-limit cool-down, so nothing was attempted.
    # request_budget: the run-wide per-provider request cap was already spent.
    assert NOT_RUN_REASONS == {
        "excluded",
        "cancelled",
        "chain_broken",
        "not_requested",
        "cooling_down",
        "request_budget",
    }


def test_new_record_fields_default_to_unknown_not_a_guess():
    record = BookRecord(provider_id="x", title="Something")
    member = SeriesMember(title="A")

    assert record.work_type == "unknown"
    assert record.format == "unknown"
    assert record.record_level == "unknown"
    assert member.work_type == "unknown"
    assert member.authors == []


def test_error_kinds():
    assert [k.value for k in ErrorKind] == [
        "timeout",
        "rate_limited",
        "auth",
        "http",
        "parse",
        "network",
    ]


def test_records_serialise_with_a_kind_and_nested_positions_stay_strings():
    record = BookRecord(
        provider_id="x",
        title="Fourth Wing",
        series=[SeriesRef(name="The Empyrean", position="3.5", provider_id="s1")],
        identifiers={"goodreads": "1"},
    )
    payload = record.to_dict()

    assert payload["kind"] == "book"
    assert payload["series"][0]["kind"] == "series_ref"
    assert payload["series"][0]["position"] == "3.5"
    # Round-trips through JSON without custom encoders.
    assert json.loads(json.dumps(payload))["series"][0]["position"] == "3.5"


def test_other_record_kinds():
    author = AuthorRecord(provider_id="x", name="Rebecca Yarros")
    series = SeriesRecord(
        provider_id="x",
        name="The Empyrean",
        members=[SeriesMember(title="Fourth Wing", position="1")],
    )
    exchange = RawExchange(method="GET", url="https://example.org", params={"q": "a"})
    query = Query(title="Fourth Wing")

    assert author.to_dict()["kind"] == "author"
    member = series.to_dict()["members"][0]
    # SeriesMember.kind is the domain field (novel vs supplementary), so the
    # record discriminator for it is record_type.
    assert member["record_type"] == "series_member"
    assert member["kind"] is None
    assert exchange.to_dict()["kind"] == "exchange"
    assert "kind" not in query.to_dict()


def test_provider_error_carries_kind_and_message():
    error = ProviderError(ErrorKind.AUTH, "HTTP 401")

    assert error.kind is ErrorKind.AUTH
    assert error.message == "HTTP 401"
    assert "HTTP 401" in str(error)
    assert "auth" in repr(error)
