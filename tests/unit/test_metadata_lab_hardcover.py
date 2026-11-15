"""Hardcover adapter.

**No credential is configured in this environment**, so no live response exists
to capture. The JSON below is built from the field names and shapes in the
documentation recorded in the provider notes (searching guide, get-book-details
guide, get-books-in-series guide, series schema). It is synthetic test data, not
a captured provider response, and the adapter stays ``VERIFIED_LIVE = False``.
"""

from __future__ import annotations

import json

import pytest

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import ErrorKind, Operation, ProviderError, Query
from metadata_lab.providers.hardcover import HardcoverAdapter

SECRET = "SECRET-12345"

SEARCH_BOOK = {
    "data": {
        "search": {
            "ids": [1],
            # Real shape observed live: a Typesense object with `hits[].document`.
            "results": {
                "found": 1,
                "hits": [
                    {
                        "document": {
                            "id": 1,
                            "slug": "fourth-wing",
                            "title": "Fourth Wing",
                            "subtitle": None,
                            "author_names": ["Rebecca Yarros"],
                            "isbns": ["9781649374042", "1649374046"],
                            "pages": 517,
                            "release_year": 2023,
                            "series_names": ["The Empyrean"],
                            "featured_series_position": 1.0,
                            "contributions": [
                                {
                                    "author": {"id": 9, "name": "Rebecca Yarros", "slug": "rebecca-yarros"},
                                    "contribution": "Author",
                                },
                                {
                                    "author": {"id": 10, "name": "A Narrator", "slug": "a-narrator"},
                                    "contribution": "Narrator",
                                },
                            ],
                        }
                    }
                ],
            },
        }
    }
}

SEARCH_AUTHOR = {
    "data": {
        "search": {
            "ids": [9],
            "results": {
                "found": 1,
                "hits": [
                    {
                        "document": {
                            "id": 9,
                            "slug": "rebecca-yarros",
                            "name": "Rebecca Yarros",
                            "alternate_names": ["Yarros, Rebecca"],
                        }
                    }
                ],
            },
        }
    }
}

EDITIONS = {
    "data": {
        "editions": [
            {
                "id": 21953653,
                "title": "Fourth Wing",
                "isbn_10": ["1649374046"],
                "isbn_13": ["9781649374042"],
                "pages": 517,
                "release_date": "2023-05-02",
                "edition_format": "Hardcover",
                "publisher": {"name": "Red Tower Books"},
                "book": {
                    "id": 1,
                    "title": "Fourth Wing",
                    "subtitle": None,
                    "release_date": "2023-05-02",
                    "slug": "fourth-wing",
                    "book_series": [{"position": "1", "series": {"id": 7, "name": "The Empyrean"}}],
                },
            }
        ]
    }
}

BOOK_SERIES = {
    "data": {
        "books": [
            {
                "id": 1,
                "title": "Fourth Wing",
                "book_series": [
                    {"position": "1", "compilation": False, "series": {"id": 7, "name": "The Empyrean"}}
                ],
            }
        ]
    }
}

SERIES_MEMBERS = {
    "data": {
        "series": [
            {
                "id": 7,
                "name": "The Empyrean",
                "books_count": 4,
                "primary_books_count": 3,
                "book_series": [
                    {
                        "position": "1",
                        "details": "1",
                        "compilation": False,
                        "book": {"id": 1, "title": "Fourth Wing", "slug": "fourth-wing"},
                    },
                    {
                        "position": "3.5",
                        "details": "3.5",
                        "compilation": False,
                        "book": {"id": 2, "title": "Threshing Day", "slug": "threshing-day"},
                    },
                ],
            }
        ]
    }
}

AUTHOR_WORKS = {
    "data": {
        "authors": [
            {
                "id": 9,
                "name": "Rebecca Yarros",
                "contributions": [
                    {"book": {"id": 1, "title": "Fourth Wing", "slug": "fourth-wing"}}
                ],
            }
        ]
    }
}

AUTHOR_SERIES = {"data": {"series": [{"id": 7, "name": "The Empyrean"}]}}


class GraphQLTransport:
    """Routes by the fixed template name embedded in the query text."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def send(self, method, url, *, params, headers, body, timeout):
        payload = json.loads(body or "{}")
        self.requests.append({"headers": dict(headers or {}), "body": payload})
        query = payload.get("query", "")
        variables = payload.get("variables") or {}
        if "MetadataLabMe" in query:
            return TransportResponse(200, {}, json.dumps({"data": {"me": {"id": 1}}}))
        if "MetadataLabSearch" in query:
            answer = SEARCH_AUTHOR if variables.get("type") == "author" else SEARCH_BOOK
            return TransportResponse(200, {}, json.dumps(answer))
        if "MetadataLabEditionByIsbn" in query:
            return TransportResponse(200, {}, json.dumps(EDITIONS))
        if "MetadataLabBookSeries" in query:
            return TransportResponse(200, {}, json.dumps(BOOK_SERIES))
        if "MetadataLabSeriesMembers" in query:
            return TransportResponse(200, {}, json.dumps(SERIES_MEMBERS))
        if "MetadataLabAuthorWorks" in query:
            return TransportResponse(200, {}, json.dumps(AUTHOR_WORKS))
        if "MetadataLabAuthorSeries" in query:
            return TransportResponse(200, {}, json.dumps(AUTHOR_SERIES))
        raise AssertionError(f"unexpected GraphQL document: {query}")


def make_ctx(transport: GraphQLTransport, credential: str = SECRET):
    client = HttpClient(provider_id="hardcover", transport=transport, min_interval_s=0.0)
    return client.context(credential=credential)


ADAPTER = HardcoverAdapter()


def test_book_by_title_maps_the_search_hit():
    ctx = make_ctx(GraphQLTransport())

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert records[0].title == "Fourth Wing"
    assert records[0].authors == ["Rebecca Yarros"]
    assert records[0].isbn13 == ["9781649374042"]
    assert records[0].isbn10 == ["1649374046"]
    assert records[0].pages == 517
    assert records[0].published == "2023"
    # Only "Author" contributions give an id the traversal can hand to author_works.
    assert records[0].author_ids == ["rebecca-yarros"]
    assert [(ref.name, ref.position) for ref in records[0].series] == [("The Empyrean", "1")]


def test_book_by_isbn_uses_the_edition_query():
    ctx = make_ctx(GraphQLTransport())

    records = ADAPTER.book_by_isbn(ctx, Query(isbn="9781649374042"))

    assert records[0].provider_id == "21953653"
    assert records[0].format == "hardcover"
    assert records[0].record_level == "edition"
    assert records[0].publisher == "Red Tower Books"
    assert records[0].series[0].provider_id == "7"


def test_series_for_book_and_members_keep_positions_as_strings():
    transport = GraphQLTransport()

    series = ADAPTER.series_for_book(make_ctx(transport), Query(title="Fourth Wing"))
    members = ADAPTER.series_members(make_ctx(transport), Query(series="The Empyrean"))

    assert [record.name for record in series] == ["The Empyrean"]
    assert [member.position for member in members[0].members] == ["1", "3.5"]
    assert members[0].ordering_scheme == "provider_default"


def test_author_operations():
    transport = GraphQLTransport()
    ctx = make_ctx(transport)

    authors = ADAPTER.author_resolve(ctx, Query(author="Rebecca Yarros"))
    works = ADAPTER.author_works(make_ctx(transport), Query(provider_id="rebecca-yarros"))
    series = ADAPTER.author_series(make_ctx(transport), Query(provider_id="rebecca-yarros"))

    assert authors[0].name == "Rebecca Yarros"
    assert authors[0].aliases == ["Yarros, Rebecca"]
    assert works[0].works == ["Fourth Wing"]
    assert [ref.name for ref in series[0].series] == ["The Empyrean"]


def test_every_query_is_a_fixed_template_with_variables():
    transport = GraphQLTransport()
    ctx = make_ctx(transport)

    ADAPTER.book_by_title(ctx, Query(title='Fourth Wing"; DROP'))

    body = transport.requests[0]["body"]
    # User input travels only in variables, never inside the document text.
    assert 'DROP' not in body["query"]
    assert body["variables"]["query"] == 'Fourth Wing"; DROP'


def test_token_is_sent_as_a_secret_header_and_never_recorded():
    transport = GraphQLTransport()
    ctx = make_ctx(transport)

    ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert transport.requests[0]["headers"]["authorization"] == f"Bearer {SECRET}"
    dumped = json.dumps([exchange.to_dict() for exchange in ctx.exchanges])
    assert SECRET not in dumped
    assert ctx.exchanges[0].request_body is not None
    assert SECRET not in ctx.exchanges[0].request_body


def test_missing_credential_is_an_auth_error():
    ctx = make_ctx(GraphQLTransport(), credential=None)

    with pytest.raises(ProviderError) as info:
        ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert info.value.kind is ErrorKind.AUTH


def test_graphql_errors_are_reported():
    class ErrorTransport(GraphQLTransport):
        def send(self, method, url, *, params, headers, body, timeout):
            return TransportResponse(200, {}, json.dumps({"errors": [{"message": "nope"}]}))

    ctx = make_ctx(ErrorTransport())

    with pytest.raises(ProviderError) as info:
        ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert info.value.kind is ErrorKind.HTTP


def test_credential_env_and_alias_are_declared():
    assert ADAPTER.credential_env == "FAMILY_LIBRARIAN_LAB_HARDCOVER_TOKEN"
    # True since the first live run on 2026-11-15 (see the provider notes).
    assert ADAPTER.VERIFIED_LIVE is True
    assert ADAPTER.implements(Operation.AUTHOR_WORKS) is True
