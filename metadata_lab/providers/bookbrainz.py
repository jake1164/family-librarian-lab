"""BookBrainz adapter. Keyless.

Implemented from the samples saved in Phase 0/5 and the OpenAPI document served
at `https://api.bookbrainz.org/1/docs/`. Every entity is a UUID ``bbid``. Series
membership and authorship come from the direct browse routes
(``/series?work=``, ``/work?series=``, ``/work?author=``), each of which returns
the related entities with their names in ONE request, so no operation fans out
over the related records.
"""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

from ..http import OpContext
from ..model import (
    AuthorRecord,
    BookRecord,
    ErrorKind,
    Operation,
    ProviderError,
    Query,
    SeriesMember,
    SeriesRecord,
    SeriesRef,
)
from .base import ProviderAdapter, Unsupported

BASE_URL = "https://api.bookbrainz.org/1"
SITE_URL = "https://bookbrainz.org"
SEARCH_SIZE = 5

#: Only "Novel" has been observed live; every other value maps only to the
#: documented work-type vocabulary and anything unrecognised stays "unknown".
_WORK_TYPE_MAP = {
    "novel": "novel",
    "novella": "novella",
    "short story": "short_story",
    "collection": "collection",
    "omnibus": "omnibus",
    "play": "play",
    "graphic novel": "graphic_novel",
    "comic": "graphic_novel",
    "anthology": "collection",
    "magazine": "other",
}


def _work_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _WORK_TYPE_MAP.get(text, "unknown")


def _name(entity: Mapping[str, Any]) -> str | None:
    alias = entity.get("defaultAlias")
    if isinstance(alias, Mapping):
        name = alias.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _ordering(entity: Mapping[str, Any]) -> str | None:
    value = entity.get("seriesOrderingType")
    return str(value).lower() if value else None


def _results(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "search response was not an object")
    results = payload.get("searchResult")
    if results is None:
        return []
    if not isinstance(results, list):
        raise ProviderError(ErrorKind.PARSE, "search response had non-list searchResult")
    return [item for item in results if isinstance(item, Mapping)]


def _browsed(payload: Any, key: str) -> list[Mapping[str, Any]]:
    """Entities from a browse response: ``{key: [{"entity": {...}, "relationship": [...]}]}``."""

    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "browse response was not an object")
    items = payload.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ProviderError(ErrorKind.PARSE, f"browse response had non-list {key}")
    entities: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        entity = item.get("entity") if isinstance(item, Mapping) else None
        if not isinstance(entity, Mapping):
            continue
        bbid = str(entity.get("bbid") or "")
        if not bbid or bbid in seen:
            continue
        seen.add(bbid)
        entities.append(entity)
    return entities


class BookBrainzAdapter(ProviderAdapter):
    id: ClassVar[str] = "bookbrainz"
    display_name: ClassVar[str] = "BookBrainz"
    adapter_version: ClassVar[str] = "2"
    credential_env: ClassVar[str | None] = None  # keyless
    #: Verified by the live Fourth Wing run on 2026-11-14.
    VERIFIED_LIVE: ClassVar[bool] = True
    #: Source-code evidence: 100 search *and* 100 lookup requests per 5 minutes
    #: before a 500 ms-per-request slowdown
    #: (https://github.com/metabrainz/bookbrainz-site/blob/master/src/api/helpers/rateLimiter.js).
    #: No published quota: 3.5 s is a lab safety margin, not a BookBrainz limit.
    min_interval_s: ClassVar[float] = 3.5

    unsupported: ClassVar[dict[Operation, Unsupported]] = {
        Operation.BOOK_BY_ISBN: Unsupported(
            reason=(
                "The web service exposes /search, /author/{bbid}, /edition/{bbid}, "
                "/edition-group/{bbid}, /publisher/{bbid}, /series/{bbid} and /work/{bbid} plus "
                "aliases/identifiers/relationships sub-paths. There is no lookup-by-identifier "
                "endpoint and /search is free text."
            ),
            source_url="https://api.bookbrainz.org/1/docs/",
        ),
        Operation.AUTHOR_SERIES: Unsupported(
            reason=(
                "GET /series?author={bbid} exists but returns the series the Author entity is itself "
                "a member of (the 'Author Series' relationship, e.g. a series of authors), not the "
                "book series of the works this author wrote. Those need an author-works browse "
                "followed by one work-series browse per work, a fan-out this lab does not perform."
            ),
            source_url="https://github.com/metabrainz/bookbrainz-site/blob/master/src/api/routes/series.js",
        ),
    }

    def check_connection(self, ctx: OpContext) -> str:
        payload = ctx.get_json(
            f"{BASE_URL}/search", params={"q": "Fourth Wing", "type": "work", "size": 1}
        )
        return f"{self.display_name} search responded ({len(_results(payload))} result(s))"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        if not q.title:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"q": q.title, "type": "work", "size": SEARCH_SIZE},
        )
        results = _results(payload)
        ctx.note_pagination(1, size=SEARCH_SIZE, retrieved=len(results))
        records: list[BookRecord] = []
        for item in results:
            bbid = str(item.get("bbid") or "")
            if not bbid:
                continue
            records.append(
                BookRecord(
                    provider_id=bbid,
                    title=_name(item) or "",
                    record_level="work",
                    source_url=f"{SITE_URL}/work/{bbid}",
                )
            )
        if records:
            # A search hit carries no author, type or language. The references exist
            # in the API, but following them is one request per hit, which this lab
            # does not do; report them as not followed rather than as missing.
            ctx.note_unresolved("authors", len(records))
        return records

    def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        work_bbid = q.provider_id or self._find_bbid(ctx, "work", q.title)
        if not work_bbid:
            return []
        entities = _browsed(ctx.get_json(f"{BASE_URL}/series", params={"work": work_bbid}), "series")
        ctx.note_pagination(1, retrieved=len(entities), total=len(entities))
        return [
            SeriesRecord(
                provider_id=str(entity["bbid"]),
                name=_name(entity) or str(entity["bbid"]),
                ordering_scheme=_ordering(entity),
                source_url=f"{SITE_URL}/series/{entity['bbid']}",
            )
            for entity in entities
        ]

    def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        series_id = q.provider_id or self._find_bbid(ctx, "series", q.series)
        if not series_id:
            return []
        detail = self._entity(ctx, "series", series_id)
        entities = _browsed(ctx.get_json(f"{BASE_URL}/work", params={"series": series_id}), "works")
        ctx.note_pagination(1, retrieved=len(entities), total=len(entities))
        members = [
            SeriesMember(
                title=_name(work) or str(work["bbid"]),
                # The browse response carries no ordinal: positions are not exposed.
                position=None,
                provider_id=str(work["bbid"]),
                work_type=_work_type(work.get("workType")),
                source_url=f"{SITE_URL}/work/{work['bbid']}",
            )
            for work in entities
        ]
        return [
            SeriesRecord(
                provider_id=series_id,
                name=(_name(detail) if detail else None) or (q.series or series_id),
                members=members,
                ordering_scheme=_ordering(detail) if detail else None,
                source_url=f"{SITE_URL}/series/{series_id}",
            )
        ]

    def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        if not q.author:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/search", params={"q": q.author, "type": "author", "size": SEARCH_SIZE}
        )
        results = _results(payload)
        ctx.note_pagination(1, size=SEARCH_SIZE, retrieved=len(results))
        return [
            AuthorRecord(
                provider_id=str(item.get("bbid") or ""),
                name=_name(item) or "",
                source_url=f"{SITE_URL}/author/{item.get('bbid')}",
            )
            for item in results
            if item.get("bbid")
        ]

    def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        author_id = q.provider_id or self._find_bbid(ctx, "author", q.author)
        if not author_id:
            return []
        entities = _browsed(ctx.get_json(f"{BASE_URL}/work", params={"author": author_id}), "works")
        ctx.note_pagination(1, retrieved=len(entities), total=len(entities))
        return [
            AuthorRecord(
                provider_id=author_id,
                name=q.author or "",
                works=[_name(work) or str(work["bbid"]) for work in entities],
                source_url=f"{SITE_URL}/author/{author_id}",
            )
        ]

    # -- helpers -----------------------------------------------------------------

    def _entity(self, ctx: OpContext, kind: str, bbid: str) -> Mapping[str, Any] | None:
        payload = ctx.get_json(f"{BASE_URL}/{kind}/{bbid}")
        return payload if isinstance(payload, Mapping) else None

    def _find_bbid(self, ctx: OpContext, kind: str, text: str | None) -> str | None:
        if not text:
            return None
        payload = ctx.get_json(
            f"{BASE_URL}/search", params={"q": text, "type": kind, "size": 1}
        )
        results = _results(payload)
        if not results:
            return None
        bbid = results[0].get("bbid")
        return str(bbid) if bbid else None


__all__ = ["BookBrainzAdapter"]
