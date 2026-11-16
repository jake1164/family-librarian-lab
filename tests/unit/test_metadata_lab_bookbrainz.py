"""BookBrainz adapter against the real samples saved in Phase 0/5."""

from __future__ import annotations

import json
from pathlib import Path

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import Operation, Query
from metadata_lab.providers.bookbrainz import BASE_URL as BASE, BookBrainzAdapter, _browsed

SAMPLES = Path(__file__).resolve().parents[2] / "metadata_lab" / "samples" / "bookbrainz"
WORK_BBID = "6605b76d-d88b-4b22-9fb9-e3d940d0d335"
SERIES_BBID = "0d962f38-e382-446c-924c-438c5879e777"
AUTHOR_BBID = "2ebca27b-f884-4fa9-b97c-055076ff295d"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _entity(bbid: str, name: str, **extra):
    return {"bbid": bbid, "defaultAlias": {"name": name, "primary": True}, **extra}


# Browse responses are NOT saved live samples. They are built from the response shape in the
# BookBrainz source (src/api/helpers/utils.js getBrowsedRelationships and formatEntityData.ts
# getWorkBasicInfo / getSeriesBasicInfo): {"bbid", "<works|series>": [{"entity", "relationship"}]}.
WORK_IDS = ["w-1", "w-2", "w-3", "w-4"]
BROWSED_SERIES = {
    "bbid": WORK_BBID,
    "series": [
        {
            "entity": _entity(SERIES_BBID, "The Empyrean", seriesOrderingType="Automatic"),
            "relationship": [{"relationshipType": "Work Series", "relationshipTypeID": 1}],
        }
    ],
}
BROWSED_SERIES_WORKS = {
    "bbid": SERIES_BBID,
    "works": [
        {
            "entity": _entity(bbid, f"Title {i}", workType="Novel", languages=["eng"]),
            "relationship": [{"relationshipType": "Work Series", "relationshipTypeID": 1}],
        }
        for i, bbid in enumerate(WORK_IDS[:3])
    ],
}
BROWSED_AUTHOR_WORKS = {
    "bbid": AUTHOR_BBID,
    "works": [
        {
            "entity": _entity(bbid, f"Written {i}", workType="Novel", languages=["eng"]),
            "relationship": [{"relationshipType": "Writer", "relationshipTypeID": 8}],
        }
        for i, bbid in enumerate(WORK_IDS)
    ],
}


class BookBrainzTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def send(self, method, url, *, params, headers, body, timeout):
        params = dict(params or {})
        self.requests.append((url, params))
        if "/search" in url:
            kind = params.get("type")
            if kind == "series":
                return TransportResponse(200, {}, sample("series_search__the-empyrean.json"))
            if kind == "author":
                return TransportResponse(200, {}, sample("author_search__rebecca-yarros.json"))
            return TransportResponse(200, {}, sample("book_by_title__fourth-wing.json"))
        if url.endswith("/series") and "work" in params:
            return TransportResponse(200, {}, json.dumps(BROWSED_SERIES))
        if url.endswith("/work") and "series" in params:
            return TransportResponse(200, {}, json.dumps(BROWSED_SERIES_WORKS))
        if url.endswith("/work") and "author" in params:
            return TransportResponse(200, {}, json.dumps(BROWSED_AUTHOR_WORKS))
        if "/series/" in url:
            return TransportResponse(200, {}, sample("series_resolve__the-empyrean.json"))
        raise AssertionError(f"unexpected URL in test (fan-out?): {url} {params}")


def make_ctx(transport: BookBrainzTransport):
    client = HttpClient(provider_id="bookbrainz", transport=transport, min_interval_s=0.0)
    return client.context()


ADAPTER = BookBrainzAdapter()


def test_book_by_title_is_one_search_with_no_per_hit_lookups():
    transport = BookBrainzTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert len(transport.requests) == 1
    assert records[0].title == "Fourth Wing"
    assert records[0].provider_id == WORK_BBID
    assert records[0].record_level == "work"
    assert records[0].source_url == f"https://bookbrainz.org/work/{WORK_BBID}"
    # A search hit carries no author; that is reported as not followed, not as missing.
    assert records[0].authors == []
    assert ctx.unresolved_counts() == {"authors": len(records)}


def test_book_by_isbn_is_declared_unsupported_with_a_citation():
    declaration = ADAPTER.unsupported[Operation.BOOK_BY_ISBN]

    assert declaration.reason
    assert declaration.source_url.startswith("https://api.bookbrainz.org/")
    assert ADAPTER.implements(Operation.BOOK_BY_ISBN) is False
    assert ADAPTER.declared_support(Operation.BOOK_BY_ISBN) == "unsupported"


def test_series_for_book_uses_the_direct_browse_route():
    transport = BookBrainzTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.series_for_book(ctx, Query(provider_id=WORK_BBID))

    assert [record.name for record in records] == ["The Empyrean"]
    assert records[0].provider_id == SERIES_BBID
    assert records[0].ordering_scheme == "automatic"
    assert transport.requests == [(f"{BASE}/series", {"work": WORK_BBID})]


def test_series_members_is_one_browse_plus_the_series_itself():
    transport = BookBrainzTransport()
    ctx = make_ctx(transport)

    records = ADAPTER.series_members(ctx, Query(provider_id=SERIES_BBID))

    members = records[0].members
    assert [m.title for m in members] == ["Title 0", "Title 1", "Title 2"]
    assert all(member.position is None for member in members)
    assert records[0].ordering_scheme == "automatic"
    assert members[0].work_type == "novel"
    # Constant cost: the series entity and one work browse, however many members.
    assert len(transport.requests) == 2


def test_author_resolve_and_works():
    transport = BookBrainzTransport()
    ctx = make_ctx(transport)

    authors = ADAPTER.author_resolve(ctx, Query(author="Rebecca Yarros"))
    works = ADAPTER.author_works(ctx, Query(author="Rebecca Yarros", provider_id=AUTHOR_BBID))

    assert authors[0].name == "Rebecca Yarros"
    assert authors[0].provider_id == AUTHOR_BBID
    assert works[0].works == ["Written 0", "Written 1", "Written 2", "Written 3"]
    assert works[0].source_url.endswith(f"/author/{AUTHOR_BBID}")
    # One search plus one browse: no request per work.
    assert len(transport.requests) == 2


def test_browse_ignores_duplicate_and_malformed_entities():
    payload = {"works": [{"entity": {"bbid": "a"}}, {"entity": {"bbid": "a"}}, {"nope": 1}, "x"]}

    assert [e["bbid"] for e in _browsed(payload, "works")] == ["a"]
    assert _browsed({}, "works") == []


def test_author_series_is_declared_unsupported_with_a_citation():
    assert ADAPTER.implements(Operation.AUTHOR_SERIES) is False
    assert ADAPTER.declared_support(Operation.AUTHOR_SERIES) == "unsupported"

    declaration = ADAPTER.unsupported[Operation.AUTHOR_SERIES]
    assert declaration.reason.strip()
    assert declaration.source_url.startswith("https://")
    # The route exists but means something else; the reason must say so.
    assert "/series?author=" in declaration.reason


def test_check_connection_reports_a_count():
    ctx = make_ctx(BookBrainzTransport())

    assert "responded" in ADAPTER.check_connection(ctx)
