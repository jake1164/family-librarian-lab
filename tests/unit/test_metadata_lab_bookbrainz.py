"""BookBrainz adapter against the real samples saved in Phase 0/5."""

from __future__ import annotations

from pathlib import Path

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import Operation, Query
from metadata_lab.providers.bookbrainz import BookBrainzAdapter

SAMPLES = Path(__file__).resolve().parents[2] / "metadata_lab" / "samples" / "bookbrainz"
WORK_BBID = "6605b76d-d88b-4b22-9fb9-e3d940d0d335"
SERIES_BBID = "0d962f38-e382-446c-924c-438c5879e777"
AUTHOR_BBID = "2ebca27b-f884-4fa9-b97c-055076ff295d"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class BookBrainzTransport:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def send(self, method, url, *, params, headers, body, timeout):
        params = dict(params or {})
        self.requests.append(url)
        if "/search" in url:
            kind = params.get("type")
            if kind == "series":
                return TransportResponse(200, {}, sample("series_search__the-empyrean.json"))
            if kind == "author":
                return TransportResponse(200, {}, sample("author_search__rebecca-yarros.json"))
            return TransportResponse(200, {}, sample("book_by_title__fourth-wing.json"))
        if url.endswith("/work/relationships") or "/work/" in url and url.endswith("/relationships"):
            return TransportResponse(200, {}, sample("series_for_book__fourth-wing.json"))
        if url.endswith("/series/relationships") or "/series/" in url and url.endswith(
            "/relationships"
        ):
            return TransportResponse(200, {}, sample("series_members__the-empyrean.json"))
        if "/author/" in url and url.endswith("/relationships"):
            return TransportResponse(200, {}, sample("author_works__rebecca-yarros.json"))
        if "/series/" in url:
            return TransportResponse(200, {}, sample("series_resolve__the-empyrean.json"))
        if "/author/" in url:
            return TransportResponse(200, {}, sample("author__rebecca-yarros.json"))
        if "/work/" in url:
            return TransportResponse(200, {}, sample("work__fourth-wing.json"))
        raise AssertionError(f"unexpected URL in test: {url}")


def make_ctx(transport: BookBrainzTransport):
    client = HttpClient(provider_id="bookbrainz", transport=transport, min_interval_s=0.0)
    return client.context()


ADAPTER = BookBrainzAdapter()


def test_book_by_title_uses_the_typed_work_type():
    ctx = make_ctx(BookBrainzTransport())

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert records[0].title == "Fourth Wing"
    assert records[0].provider_id == WORK_BBID
    assert records[0].record_level == "work"
    # "Novel" is the value the API actually typed in the saved sample.
    assert records[0].work_type == "novel"
    assert records[0].language == "eng"
    # The "Writer" relationship was followed to the author entity.
    assert records[0].authors == ["Rebecca Yarros"]
    assert records[0].author_ids == [AUTHOR_BBID]
    assert records[0].source_url == f"https://bookbrainz.org/work/{WORK_BBID}"


def test_book_by_isbn_is_declared_unsupported_with_a_citation():
    declaration = ADAPTER.unsupported[Operation.BOOK_BY_ISBN]

    assert declaration.reason
    assert declaration.source_url.startswith("https://api.bookbrainz.org/")
    assert ADAPTER.implements(Operation.BOOK_BY_ISBN) is False
    assert ADAPTER.declared_support(Operation.BOOK_BY_ISBN) == "unsupported"


def test_series_for_book_follows_the_work_series_relationship():
    ctx = make_ctx(BookBrainzTransport())

    records = ADAPTER.series_for_book(ctx, Query(provider_id=WORK_BBID))

    assert [record.name for record in records] == ["The Empyrean"]
    assert records[0].provider_id == SERIES_BBID


def test_series_members_lists_work_members_without_positions():
    ctx = make_ctx(BookBrainzTransport())

    records = ADAPTER.series_members(ctx, Query(provider_id=SERIES_BBID))

    members = records[0].members
    assert len(members) == 3
    assert all(member.position is None for member in members)
    assert records[0].ordering_scheme == "automatic"
    assert members[0].work_type == "novel"  # typed by the work entity


def test_author_resolve_and_works():
    ctx = make_ctx(BookBrainzTransport())

    authors = ADAPTER.author_resolve(ctx, Query(author="Rebecca Yarros"))
    works = ADAPTER.author_works(ctx, Query(author="Rebecca Yarros", provider_id=AUTHOR_BBID))

    assert authors[0].name == "Rebecca Yarros"
    assert authors[0].provider_id == AUTHOR_BBID
    # Four "wrote" relationships in the saved sample. The titles repeat because
    # this fake transport serves one work-detail sample for every bbid.
    assert len(works[0].works) == 4
    assert works[0].source_url.endswith(f"/author/{AUTHOR_BBID}")


def test_author_series_is_declared_unsupported_with_a_citation():
    assert ADAPTER.implements(Operation.AUTHOR_SERIES) is False
    assert ADAPTER.declared_support(Operation.AUTHOR_SERIES) == "unsupported"

    declaration = ADAPTER.unsupported[Operation.AUTHOR_SERIES]
    assert declaration.reason.strip()
    assert declaration.source_url.startswith("https://api.bookbrainz.org/")


def test_check_connection_reports_a_count():
    ctx = make_ctx(BookBrainzTransport())

    assert "responded" in ADAPTER.check_connection(ctx)
