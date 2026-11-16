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
        if "/search.json" in url and params.get("author") and "page" in params:
            return sample("author_series__yarros.json")
        if "/search.json" in url:
            query = str(params.get("q", ""))
            if query.startswith("series_key:"):
                return sample("series_members__the-empyrean-keyed.json")
            if query.startswith("series_name:"):
                return sample("series_members__the-empyrean-keyed.json")
            if "series_key" in str(params.get("fields", "")) and params.get("limit") == 1:
                return sample("series_for_book__fourth-wing.json")
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


def test_series_for_book_is_one_search_with_series_fields():
    transport = SampleTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.series_for_book(ctx, Query(title="Fourth Wing", author="Yarros"))

    assert [(r.provider_id, r.name) for r in records] == [("OL331363L", "The Empyrean")]
    # One request: no edition probing.
    assert len(transport.requests) == 1
    assert "series_key" in transport.requests[0][2]["fields"]


def test_series_for_book_uses_the_isbn_in_the_same_single_search():
    transport = SampleTransport()
    ctx = make_ctx(transport)

    ADAPTER.series_for_book(ctx, Query(title="Fourth Wing", isbn="978-1-64937-404-2"))

    assert len(transport.requests) == 1
    assert transport.requests[0][2]["q"] == "isbn:9781649374042"


def test_series_for_book_returns_nothing_when_the_top_hit_has_no_series():
    body = json.dumps({"docs": [{"key": "/works/OL1W", "title": "Fourth Wing (Collection)"}]})
    ctx = make_ctx(SampleTransport(overrides={"/search.json": (200, body)}))

    assert ADAPTER.series_for_book(ctx, Query(title="Fourth Wing")) == []
    assert any("no series data" in note for note in ctx.notes)


def test_series_members_by_key_carry_positions_in_order():
    transport = SampleTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.series_members(ctx, Query(provider_id="OL331363L"))

    assert len(records) == 1 and records[0].provider_id == "OL331363L"
    assert [(m.title, m.position) for m in records[0].members] == [
        ("Fourth Wing", "1"),
        ("Iron Flame", "2"),
        ("Onyx Storm", "3"),
    ]
    assert transport.requests[0][2]["q"] == "series_key:OL331363L"
    assert ctx.truncated is False


def test_series_members_by_name_keep_exact_name_matches_only():
    longer = json.dumps(
        {
            "numFound": 2,
            "docs": [
                {"key": "/works/OL1W", "title": "A", "series_key": ["OL1L"],
                 "series_name": ["The Empyrean"], "series_position": ["1"]},
                {"key": "/works/OL2W", "title": "B", "series_key": ["OL2L"],
                 "series_name": ["The Empyrean Chronicles"], "series_position": ["1"]},
            ],
        }
    )
    ctx = make_ctx(SampleTransport(overrides={"/search.json": (200, longer)}))

    records = ADAPTER.series_members(ctx, Query(series="The Empyrean"))

    assert [(r.provider_id, [m.title for m in r.members]) for r in records] == [("OL1L", ["A"])]


def test_series_members_same_name_in_two_series_stays_two_records():
    body = json.dumps(
        {
            "numFound": 2,
            "docs": [
                {"key": "/works/OL1W", "title": "A", "series_key": ["OL1L"],
                 "series_name": ["Shared"], "series_position": ["1"]},
                {"key": "/works/OL2W", "title": "B", "series_key": ["OL2L"],
                 "series_name": ["Shared"], "series_position": ["1"]},
            ],
        }
    )
    ctx = make_ctx(SampleTransport(overrides={"/search.json": (200, body)}))

    records = ADAPTER.series_members(ctx, Query(series="Shared"))

    assert sorted(r.provider_id for r in records) == ["OL1L", "OL2L"]


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


def test_author_series_is_derived_from_one_list_and_deduplicated_by_series_key():
    transport = SampleTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.author_series(ctx, Query(author="Rebecca Yarros", provider_id="OL7825177A"))

    assert ADAPTER.unsupported == {}
    assert ADAPTER.declared_support(Operation.AUTHOR_SERIES) == "supported"
    assert len(records) == 1
    assert records[0].provider_id == "/authors/OL7825177A"
    refs = {ref.provider_id: ref.name for ref in records[0].series}
    # The live response had three series keys across eleven works with series data.
    assert refs == {
        "OL331363L": "The Empyrean",
        "OL331365L": "The Renegades",
        "OL331364L": "Flight & Glory",
    }
    searches = [r for r in transport.requests if r[1].endswith("/search.json")]
    assert len(searches) == 1, "one paginated list, never a request per work"
    assert searches[0][2]["author"] == "Rebecca Yarros"
    assert any(note.startswith("derived:") for note in ctx.notes)


def test_author_series_ignores_works_by_a_namesake():
    ctx = make_ctx(SampleTransport())

    records = ADAPTER.author_series(ctx, Query(author="Rebecca Yarros", provider_id="OL999999A"))

    assert records == []


def test_author_series_pages_until_the_total_is_retrieved_and_flags_truncation():
    class Paged(SampleTransport):
        def _body(self, url, params):
            if url.endswith("/search.json"):
                doc = {
                    "key": "/works/OL1W",
                    "title": "T",
                    "author_key": ["OL7825177A"],
                    "series_key": [f"OL{params['page']}L"],
                    "series_name": [f"S{params['page']}"],
                }
                return json.dumps({"numFound": 999, "docs": [doc] * 100})
            return super()._body(url, params)

    ctx = make_ctx(Paged())
    ctx.max_pages = 2

    records = ADAPTER.author_series(ctx, Query(author="A B", provider_id="OL7825177A"))

    assert {ref.provider_id for ref in records[0].series} == {"OL1L", "OL2L"}
    assert ctx.truncated


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
