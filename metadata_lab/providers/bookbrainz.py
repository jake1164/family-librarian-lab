"""BookBrainz adapter. Keyless.

Implemented from the samples saved in Phase 0/5 and the OpenAPI document served
at `https://api.bookbrainz.org/1/docs/`. Every entity is a UUID ``bbid``; the
facts we need (series membership, authorship) live in ``/relationships``.
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
DETAIL_LOOKUPS = 3
#: Author resolution is limited to two hits so the op stays well inside 10 calls.
AUTHOR_LOOKUP_HITS = 2
#: Work-title lookups per series. `series_members` also spends 3 calls (search, series, relationships), so 17
#: fits a case that sets `options: {max_calls: 20}`; with the default 10-call budget the operation still
#: stops early and reports `truncated` (budget reached) instead of pretending to be complete.
TITLE_LOOKUP_LIMIT = 17
#: Author lookups per work: one keeps the whole book_by_title inside the budget.
AUTHORS_PER_WORK = 1

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


def _results(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "search response was not an object")
    results = payload.get("searchResult")
    if results is None:
        return []
    if not isinstance(results, list):
        raise ProviderError(ErrorKind.PARSE, "search response had non-list searchResult")
    return [item for item in results if isinstance(item, Mapping)]


def _relationships(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "relationships response was not an object")
    relationships = payload.get("relationships")
    if relationships is None:
        return []
    if not isinstance(relationships, list):
        raise ProviderError(ErrorKind.PARSE, "relationships response had non-list relationships")
    return [item for item in relationships if isinstance(item, Mapping)]


class BookBrainzAdapter(ProviderAdapter):
    id: ClassVar[str] = "bookbrainz"
    display_name: ClassVar[str] = "BookBrainz"
    adapter_version: ClassVar[str] = "1"
    credential_env: ClassVar[str | None] = None  # keyless
    #: Verified by the live Fourth Wing run on 2026-11-14.
    VERIFIED_LIVE: ClassVar[bool] = True
    min_interval_s: ClassVar[float] = 1.0

    unsupported: ClassVar[dict[Operation, Unsupported]] = {
        Operation.BOOK_BY_ISBN: Unsupported(
            reason=(
                "The web service exposes /search, /author/{bbid}, /edition/{bbid}, "
                "/edition-group/{bbid}, /publisher/{bbid}, /series/{bbid} and /work/{bbid} plus "
                "aliases/identifiers/relationships sub-paths. There is no lookup-by-identifier "
                "endpoint and /search is free text."
            ),
            source_url="https://api.bookbrainz.org/1/docs/",
        )
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
        for index, item in enumerate(results):
            bbid = str(item.get("bbid") or "")
            if not bbid:
                continue
            record = BookRecord(
                provider_id=bbid,
                title=_name(item) or "",
                record_level="work",
                source_url=f"{SITE_URL}/work/{bbid}",
            )
            if index < DETAIL_LOOKUPS:
                detail = self._entity(ctx, "work", bbid)
                if detail is not None:
                    record.work_type = _work_type(detail.get("workType"))
                    languages = detail.get("languages") or []
                    if isinstance(languages, list) and languages:
                        record.language = str(languages[0])
                if index < AUTHOR_LOOKUP_HITS:
                    self._apply_work_authors(ctx, record, bbid)
                elif index < DETAIL_LOOKUPS:
                    # The hit's author reference exists in the API but the budget
                    # does not allow following it.
                    ctx.note_unresolved("authors", 1)
            elif results:
                # Beyond the classification bound: the author reference exists but
                # was not followed.
                ctx.note_unresolved("authors", 1)
            records.append(record)
        if len(results) > DETAIL_LOOKUPS:
            ctx.mark_truncated(
                f"classification looked up for {DETAIL_LOOKUPS} of {len(results)} search hits"
            )
        return records

    def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        work_bbid = q.provider_id or self._find_bbid(ctx, "work", q.title)
        if not work_bbid:
            return []
        relationships = _relationships(
            ctx.get_json(f"{BASE_URL}/work/{work_bbid}/relationships")
        )
        series_ids = [
            str(item.get("targetBbid"))
            for item in relationships
            if item.get("relationshipTypeName") == "Work Series"
            and str(item.get("targetEntityType")).lower() == "series"
            and item.get("targetBbid")
        ]
        records: list[SeriesRecord] = []
        for series_id in dict.fromkeys(series_ids):
            detail = self._entity(ctx, "series", series_id)
            records.append(
                SeriesRecord(
                    provider_id=series_id,
                    name=(_name(detail) if detail else None) or series_id,
                    source_url=f"{SITE_URL}/series/{series_id}",
                )
            )
        return records

    def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        series_id = q.provider_id or self._find_bbid(ctx, "series", q.series)
        if not series_id:
            return []
        detail = self._entity(ctx, "series", series_id)
        relationships = _relationships(
            ctx.get_json(f"{BASE_URL}/series/{series_id}/relationships")
        )
        work_ids = [
            str(item.get("targetBbid"))
            for item in relationships
            if str(item.get("targetEntityType")).lower() == "work" and item.get("targetBbid")
        ]
        work_ids = list(dict.fromkeys(work_ids))
        ctx.note_pagination(1, retrieved=len(work_ids), total=len(work_ids))
        if len(work_ids) > TITLE_LOOKUP_LIMIT:
            ctx.mark_truncated(
                f"member titles limited to {TITLE_LOOKUP_LIMIT} of {len(work_ids)} works"
            )
        members: list[SeriesMember] = []
        for work_id in work_ids[:TITLE_LOOKUP_LIMIT]:
            work = self._entity(ctx, "work", work_id)
            members.append(
                SeriesMember(
                    title=(_name(work) if work else None) or work_id,
                    # The API exposes no ordinal; the sample series is Automatic.
                    position=None,
                    provider_id=work_id,
                    work_type=_work_type(work.get("workType")) if work else "unknown",
                    source_url=f"{SITE_URL}/work/{work_id}",
                )
            )
        return [
            SeriesRecord(
                provider_id=series_id,
                name=(_name(detail) if detail else None) or (q.series or series_id),
                members=members,
                ordering_scheme=(str(detail.get("seriesOrderingType")).lower() if detail and detail.get("seriesOrderingType") else None),
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
        relationships = _relationships(
            ctx.get_json(f"{BASE_URL}/author/{author_id}/relationships")
        )
        work_ids = [
            str(item.get("targetBbid"))
            for item in relationships
            if str(item.get("targetEntityType")).lower() == "work" and item.get("targetBbid")
        ]
        work_ids = list(dict.fromkeys(work_ids))
        ctx.note_pagination(1, retrieved=len(work_ids), total=len(work_ids))
        if len(work_ids) > TITLE_LOOKUP_LIMIT:
            ctx.mark_truncated(
                f"titles limited to {TITLE_LOOKUP_LIMIT} of {len(work_ids)} works"
            )
        titles: list[str] = []
        for work_id in work_ids[:TITLE_LOOKUP_LIMIT]:
            work = self._entity(ctx, "work", work_id)
            titles.append((_name(work) if work else None) or work_id)
        return [
            AuthorRecord(
                provider_id=author_id,
                name=q.author or "",
                works=titles,
                source_url=f"{SITE_URL}/author/{author_id}",
            )
        ]

    # -- helpers -----------------------------------------------------------------

    def _apply_work_authors(
        self, ctx: OpContext, record: BookRecord, work_bbid: str
    ) -> None:
        """Resolve up to ``AUTHORS_PER_WORK`` authors from the work's relationships."""

        relationships = _relationships(
            ctx.get_json(f"{BASE_URL}/work/{work_bbid}/relationships")
        )
        author_ids = [
            str(item.get("targetBbid"))
            for item in relationships
            if str(item.get("targetEntityType")).lower() == "author" and item.get("targetBbid")
        ]
        author_ids = list(dict.fromkeys(author_ids))
        if len(author_ids) > AUTHORS_PER_WORK:
            ctx.note_unresolved("authors", len(author_ids) - AUTHORS_PER_WORK)
        names: list[str] = []
        for author_id in author_ids[:AUTHORS_PER_WORK]:
            entity = self._entity(ctx, "author", author_id)
            name = _name(entity) if entity else None
            if name:
                names.append(name)
            else:
                ctx.note_unresolved("authors", 1)
        record.authors = names
        record.author_ids = author_ids[:AUTHORS_PER_WORK]

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
