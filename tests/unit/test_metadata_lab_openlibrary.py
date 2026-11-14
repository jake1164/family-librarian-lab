"""Open Library adapter against the real samples saved in Phase 0."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import ErrorKind, Operation, ProviderError, Query
from metadata_lab.providers.openlibrary import (
    OpenLibraryAdapter,
    _normalise_edition,
    _normalise_search_doc,
    _series_refs,
)

SAMPLES = Path(__file__).resolve().parents[2] / "metadata_lab" / "samples" / "openlibrary"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class SampleTransport:
    """Serves the saved sample bodies; never touches the network."""

    def __init__(self, overrides: dict[str, tuple[int, str]] | None = None) -> None:
        self.requests: list[tuple[str, str, dict]] = []
        self.overrides = overrides or {}

    def send(self, method, url, *, params, headers, body, timeout):
        self.requests.append((method, url, dict(params or {})))
        for marker, (status, text) in self.overrides.items():
            if marker in url:
                return TransportResponse(status, {}, text)
        return TransportResponse(200, {}, self._body(url, params or {}))

    def _body(self, url: str, params: dict) -> str:
        if "/search/authors.json" in url:
            return sample("author_resolve__rebecca-yarros.json")
        if "/search.json" in url:
            if str(params.get("q", "")).startswith("series:"):
                return sample("series_members__the-empyrean.json")
            return sample("book_by_title__fourth-wing.json")
        if "/authors/OL7825177A/works.json" in url:
            return sample("author_works__OL7825177A.json")
        if "/authors/OL7825177A.json" in url:
            return sample("author__OL7825177A.json")
        if "/isbn/" in url or "/books/" in url:
            return sample("book_by_isbn__9781649374042.json")
        raise AssertionError(f"unexpected URL in test: {url}")


def make_ctx(transport: SampleTransport):
    client = HttpClient(provider_id="openlibrary", transport=transport, min_interval_s=0.0)
    return client.context()


ADAPTER = OpenLibraryAdapter()


def test_book_by_title_normalises_the_search_sample():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing", author="Yarros"))

    assert len(records) == 3
    first = records[0]
    assert first.title == "Fourth Wing"
    assert first.authors == ["Rebecca Yarros"]
    assert "9781649374042" in first.isbn13
    assert "1649374046" in first.isbn10
    assert first.publisher
    assert first.published
    assert first.pages == 522
    # The work-level sample lists several languages; the adapter takes the first
    # entry rather than guessing a preferred one.
    assert first.language == "spa"
    assert first.cover is True
    assert first.author_ids == ["/authors/OL7825177A"]
    # The search endpoint does not return series data for works.
    assert first.series == []


def test_book_by_isbn_normalises_the_edition_sample():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.book_by_isbn(ctx, Query(isbn="978-1-64937-404-2"))

    assert len(records) == 1
    record = records[0]
    assert record.provider_id == "/books/OL39767438M"
    assert record.title == "Fourth Wing"
    assert record.authors == ["Rebecca Yarros"]  # resolved through the author sample
    assert record.isbn10 == ["1649374046"]
    assert record.isbn13 == ["9781649374042"]
    assert record.pages == 517
    assert record.publisher == "Entangled Publishing, LLC"
    assert record.language == "eng"
    assert record.cover is True
    assert record.identifiers["goodreads"] == "61431922"
    assert [(ref.name, ref.position) for ref in record.series] == [("The Empyrean", "1")]


def test_book_by_isbn_404_is_not_an_error():
    ctx = make_ctx(SampleTransport(overrides={"/isbn/": (404, '{"error": "notfound"}')}))

    assert ADAPTER.book_by_isbn(ctx, Query(isbn="9999999999999")) == []


def test_malformed_body_is_a_parse_error():
    ctx = make_ctx(SampleTransport(overrides={"/search.json": (200, "<html>nope</html>")}))

    with pytest.raises(ProviderError) as info:
        ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert info.value.kind is ErrorKind.PARSE
    # The raw exchange is kept as evidence.
    assert ctx.exchanges[0].body.startswith("<html>")


def test_empty_docs_is_an_empty_result():
    ctx = make_ctx(SampleTransport(overrides={"/search.json": (200, '{"docs": []}')}))

    assert ADAPTER.book_by_title(ctx, Query(title="Nothing At All")) == []


def test_series_for_book_reads_the_edition_series_field():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.series_for_book(ctx, Query(provider_id="/books/OL39767438M"))

    assert [record.name for record in records] == ["The Empyrean"]
    assert records[0].members == []


def test_series_for_book_uses_the_cases_isbn_first():
    transport = SampleTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.series_for_book(ctx, Query(title="Fourth Wing", isbn="9781649374042"))

    assert [record.name for record in records] == ["The Empyrean"]
    # The ISBN edition answered directly: one call, no search needed.
    assert len(transport.requests) == 1


def test_series_for_book_returns_nothing_when_no_edition_has_series():
    edition = json.loads(sample("book_by_isbn__9781649374042.json"))
    edition.pop("series")
    transport = SampleTransport(
        overrides={
            "/isbn/": (404, '{"error": "notfound"}'),
            "/books/": (200, json.dumps(edition)),
        }
    )
    ctx = make_ctx(transport)

    assert ADAPTER.series_for_book(ctx, Query(title="Fourth Wing", author="Yarros")) == []
    assert any("series data" in note for note in ctx.notes)


def test_series_members_lists_works_and_admits_no_positions():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.series_members(ctx, Query(series="The Empyrean"))

    assert len(records) == 1
    members = records[0].members
    assert "Fourth Wing" in [member.title for member in members]
    assert all(member.position is None for member in members)
    assert ctx.truncated is False


def test_author_resolve_normalises_keys_and_aliases():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.author_resolve(ctx, Query(author="Rebecca Yarros"))

    assert [record.provider_id for record in records] == [
        "/authors/OL7825177A",
        "/authors/OL15661320A",
    ]
    assert records[0].name == "Rebecca Yarros"
    assert records[0].aliases == ["Yarros, Rebecca"]


def test_author_works_returns_titles():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.author_works(ctx, Query(author="Rebecca Yarros", provider_id="OL7825177A"))

    assert len(records) == 1
    assert records[0].provider_id == "/authors/OL7825177A"
    assert "Fourth Wing" in records[0].works or records[0].works
    assert len(records[0].works) == 10


def test_author_series_is_not_implemented_by_this_adapter():
    assert ADAPTER.implements(Operation.AUTHOR_SERIES) is False
    assert ADAPTER.declared_support(Operation.AUTHOR_SERIES) == "not_implemented"
    assert ADAPTER.declared_support(Operation.SERIES_MEMBERS) == "supported"


def test_no_operation_is_declared_unsupported():
    # Every gap here is our adapter's, not Open Library's (see provider notes).
    assert ADAPTER.unsupported == {}


def test_check_connection_reports_a_result_count():
    ctx = make_ctx(SampleTransport())

    assert "responded" in ADAPTER.check_connection(ctx)


# --- pure normalisers (no HTTP at all) --------------------------------------------


def test_series_ref_parsing_keeps_positions_as_strings():
    refs = _series_refs(["The Empyrean (#3.5)", "Flight & Glory"])

    assert [(ref.name, ref.position) for ref in refs] == [
        ("The Empyrean", "3.5"),
        ("Flight & Glory", None),
    ]


def test_normalise_edition_handles_text_objects_and_missing_fields():
    raw = json.loads(sample("book_by_isbn__9781649374042.json"))
    raw["description"] = {"type": "/type/text", "value": "  A blurb.  "}
    raw.pop("number_of_pages", None)
    raw.pop("covers", None)

    record = _normalise_edition(raw)

    assert record.description == "A blurb."
    assert record.pages is None
    assert record.cover is False
    assert record.authors == []  # no names supplied


def test_normalise_search_doc_tolerates_missing_lists():
    record = _normalise_search_doc({"key": "/works/OL1W", "title": "Something"})

    assert record.title == "Something"
    assert record.authors == []
    assert record.isbn10 == [] and record.isbn13 == []
    assert record.cover is False
