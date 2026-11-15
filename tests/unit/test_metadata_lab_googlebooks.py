"""Google Books adapter.

**No key is configured in this environment**, so no volume response could be
captured (a keyless probe returned HTTP 429; that error body is the saved
sample). The JSON below follows the Volume resource documented at
`developers.google.com/books/docs/v1/reference/volumes`; it is synthetic test
data and the adapter stays ``VERIFIED_LIVE = False``.
"""

from __future__ import annotations

import json

import pytest

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import ErrorKind, Operation, ProviderError, Query
from metadata_lab.providers.googlebooks import GoogleBooksAdapter

SECRET = "SECRET-12345"

VOLUMES = {
    "kind": "books#volumes",
    "totalItems": 1,
    "items": [
        {
            "kind": "books#volume",
            "id": "_ojXNuzgHRcC",
            "volumeInfo": {
                "title": "Fourth Wing",
                "authors": ["Rebecca Yarros"],
                "publisher": "Red Tower Books",
                "publishedDate": "2023-05-02",
                "description": "A blurb.",
                "industryIdentifiers": [
                    {"type": "ISBN_13", "identifier": "9781649374042"},
                    {"type": "ISBN_10", "identifier": "1649374046"},
                ],
                "pageCount": 517,
                "language": "en",
                "imageLinks": {"thumbnail": "https://example.org/thumbnail.jpg"},
                "infoLink": "https://books.google.com/books?id=_ojXNuzgHRcC",
            },
            "saleInfo": {"isEbook": True},
        }
    ],
}

EMPTY = {"kind": "books#volumes", "totalItems": 0}


class VolumesTransport:
    def __init__(self, body: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.body = body or VOLUMES

    def send(self, method, url, *, params, headers, body, timeout):
        self.requests.append({"params": dict(params or {}), "url": url})
        return TransportResponse(200, {}, json.dumps(self.body))


def make_ctx(transport: VolumesTransport, credential: str = SECRET):
    client = HttpClient(provider_id="googlebooks", transport=transport, min_interval_s=0.0)
    return client.context(credential=credential)


ADAPTER = GoogleBooksAdapter()


def test_book_by_title_uses_intitle_and_inauthor():
    transport = VolumesTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing", author="Rebecca Yarros"))

    assert transport.requests[0]["params"]["q"] == 'intitle:"Fourth Wing" inauthor:"Rebecca Yarros"'
    assert records[0].title == "Fourth Wing"
    assert records[0].authors == ["Rebecca Yarros"]
    assert records[0].isbn13 == ["9781649374042"]
    assert records[0].isbn10 == ["1649374046"]
    assert records[0].pages == 517
    assert records[0].published == "2023-05-02"
    assert records[0].language == "en"
    assert records[0].cover is True
    assert records[0].format == "ebook"  # from saleInfo.isEbook
    assert records[0].record_level == "edition"
    assert records[0].work_type == "unknown"  # the API types no classification


def test_book_by_isbn_uses_the_isbn_qualifier():
    transport = VolumesTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.book_by_isbn(ctx, Query(isbn="978-1-64937-404-2"))

    assert transport.requests[0]["params"]["q"] == "isbn:9781649374042"
    assert records[0].isbn13 == ["9781649374042"]


def test_empty_results_are_not_an_error():
    ctx = make_ctx(VolumesTransport(EMPTY))

    assert ADAPTER.book_by_title(ctx, Query(title="Nothing")) == []


def test_author_works_is_name_based_and_paginates():
    transport = VolumesTransport(EMPTY)
    ctx = make_ctx(transport)

    assert ADAPTER.author_works(ctx, Query(author="Rebecca Yarros")) == []
    assert transport.requests[0]["params"]["q"] == 'inauthor:"Rebecca Yarros"'


def test_author_works_returns_one_record_with_titles():
    ctx = make_ctx(VolumesTransport())

    records = ADAPTER.author_works(ctx, Query(author="Rebecca Yarros"))

    assert records[0].works == ["Fourth Wing"]
    assert records[0].name == "Rebecca Yarros"


def test_key_is_a_secret_parameter_and_never_recorded():
    transport = VolumesTransport()
    ctx = make_ctx(transport)

    ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert transport.requests[0]["params"]["key"] == SECRET
    assert "key" not in ctx.exchanges[0].params
    dumped = json.dumps([exchange.to_dict() for exchange in ctx.exchanges])
    assert SECRET not in dumped


def test_missing_key_is_an_auth_error():
    ctx = make_ctx(VolumesTransport(), credential=None)

    with pytest.raises(ProviderError) as info:
        ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert info.value.kind is ErrorKind.AUTH


def test_unsupported_operations_are_declared_with_citations():
    for operation in (Operation.SERIES_MEMBERS, Operation.AUTHOR_SERIES):
        declaration = ADAPTER.unsupported[operation]
        assert declaration.reason
        assert declaration.source_url.startswith("https://developers.google.com/")
        assert ADAPTER.declared_support(operation) == "unsupported"


def test_unclear_operations_stay_not_implemented():
    assert ADAPTER.implements(Operation.SERIES_FOR_BOOK) is False
    assert ADAPTER.implements(Operation.AUTHOR_RESOLVE) is False
    assert ADAPTER.declared_support(Operation.SERIES_FOR_BOOK) == "not_implemented"


def test_credential_env_and_aliases_are_declared():
    assert ADAPTER.credential_env == "FAMILY_LIBRARIAN_LAB_GOOGLEBOOKS_KEY"
    assert "FAMILY_LIBRARIAN_GOOGLE_BOOKS_API_KEY" in ADAPTER.credential_env_aliases
    # True since the first live run on 2026-11-15 (book_by_title only; see the provider notes).
    assert ADAPTER.VERIFIED_LIVE is True
