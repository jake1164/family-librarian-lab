"""Inventaire adapter. Keyless.

Implemented from the samples saved in Phase 0 and the OpenAPI document recorded
in the provider notes (`https://inventaire.io/public/api_specs.json`). Entities
are Wikidata ("wd:") or Inventaire ("inv:") URIs; an ISBN resolves to an
*edition* whose ``wdt:P629`` claim points at the work.
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable, Mapping

from ..http import REQUEST_CLASS_SEARCH, OpContext
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
from .base import ProviderAdapter

BASE_URL = "https://inventaire.io/api"
ENTITY_URL = "https://inventaire.io/entity"
SEARCH_LIMIT = 5
MAX_MEMBER_URIS = 40
#: One batched label call covers every part we keep (verified live with 39 uris).
MAX_MEMBER_TITLE_LOOKUPS = MAX_MEMBER_URIS
#: Label languages tried after English: a Latin-script label beats an arbitrary first language.
LABEL_LANGUAGE_FALLBACK = ("en", "mul", "fromclaims", "fr", "de", "es", "it", "pt", "nl")
#: Work-claim lookups per call, so the whole op stays inside the 10-call budget.
MAX_WORK_CLAIM_LOOKUPS = 3

#: Wikidata claim ids we read. Only these are used; nothing is inferred.
P_TITLE = "wdt:P1476"
P_ISBN13 = "wdt:P212"
P_ISBN10 = "wdt:P957"
P_PUBLISHED = "wdt:P577"
P_LANGUAGE = "wdt:P407"
P_WORK = "wdt:P629"
P_SERIES = "wdt:P179"
P_AUTHOR = "wdt:P50"
#: Series ordinal inside the work's series statement.
P_SERIES_ORDINAL = "wdt:P1545"


def _pipe(uris: Iterable[str]) -> str:
    """Join URIs the documented way (``uris=a|b|c``).

    A repeated ``uris=`` parameter works for a handful of values, but the server
    turns an array of more than 20 into an object and answers 400 "invalid uris".
    """

    return "|".join(uris)


def _first(values: Any) -> str | None:
    if isinstance(values, (list, tuple)):
        for value in values:
            text = str(value).strip()
            if text:
                return text
        return None
    if values is None:
        return None
    text = str(values).strip()
    return text or None


def _label(entity: Mapping[str, Any]) -> str | None:
    labels = entity.get("labels")
    if isinstance(labels, Mapping):
        for key in LABEL_LANGUAGE_FALLBACK:
            value = labels.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in labels.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _claims(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    claims = entity.get("claims")
    return claims if isinstance(claims, Mapping) else {}


def _normalise_edition(entity: Mapping[str, Any]) -> BookRecord:
    claims = _claims(entity)
    uri = str(entity.get("uri") or "")
    title = _first(claims.get(P_TITLE)) or _label(entity) or ""
    return BookRecord(
        provider_id=uri,
        title=title,
        isbn13=[_digits(isbn) for isbn in claims.get(P_ISBN13) or []],
        isbn10=[_digits(isbn) for isbn in claims.get(P_ISBN10) or []],
        published=_first(claims.get(P_PUBLISHED)),
        # P407 is a Wikidata language *entity* uri, not an ISO code: not comparable.
        language=None,
        identifiers={"inventaire": uri, "work": _first(claims.get(P_WORK)) or ""},
        record_level="edition",
        work_type="unknown",  # no typed classification on an edition
        source_url=f"{ENTITY_URL}/{uri}" if uri else None,
    )


def _digits(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit() or char.upper() == "X")


class InventaireAdapter(ProviderAdapter):
    id: ClassVar[str] = "inventaire"
    display_name: ClassVar[str] = "Inventaire"
    adapter_version: ClassVar[str] = "1"
    credential_env: ClassVar[str | None] = None  # keyless
    #: Verified by the live Fourth Wing run on 2026-11-14.
    VERIFIED_LIVE: ClassVar[bool] = True
    #: No numeric limit published (https://api.inventaire.io/). 3 s for lookups is
    #: the research doc's engineering margin; a /search block lasted over 21 min
    #: in lab use, so searches get the much slower 10 s class below. Neither is an
    #: Inventaire quota.
    min_interval_s: ClassVar[float] = 3.0
    #: Search requests are the ones that get blocked, so they are paced on their own.
    request_intervals_s: ClassVar[Mapping[str, float]] = {REQUEST_CLASS_SEARCH: 10.0}
    #: Observed block: over 21 minutes. Forgetting a block and retrying keeps it
    #: alive, so the default cool-down here is an hour.
    cooldown_default_s: ClassVar[float] = 60 * 60.0
    #: Nothing declared UNSUPPORTED: every gap is our adapter's (author_series is
    #: derived from the same endpoint that answers author_works).
    unsupported: ClassVar[dict[Operation, Any]] = {}

    def check_connection(self, ctx: OpContext) -> str:
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"search": "The Empyrean", "types": "series", "limit": 1},
            request_class=REQUEST_CLASS_SEARCH,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        return f"{self.display_name} search responded ({len(results or [])} result(s))"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        if not q.title:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"search": q.title, "types": "works", "limit": SEARCH_LIMIT},
            request_class=REQUEST_CLASS_SEARCH,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if results is None:
            raise ProviderError(ErrorKind.PARSE, "search response had no results list")
        if not isinstance(results, list):
            raise ProviderError(ErrorKind.PARSE, "search response had a non-list results")
        hits = [item for item in results if isinstance(item, Mapping)]
        records: list[BookRecord] = []
        for item in hits:
            uri = str(item.get("uri") or "")
            records.append(
                BookRecord(
                    provider_id=uri,
                    title=str(item.get("label") or ""),
                    description=str(item["description"]) if item.get("description") else None,
                    record_level="work",
                    work_type="unknown",
                    identifiers={"inventaire": uri},
                    source_url=f"{ENTITY_URL}/{uri}" if uri else None,
                )
            )
        if records:
            # Follow the work claims once per hit (bounded), then resolve every
            # author/series label in ONE batched call.
            self._enrich_works(ctx, records, hits[:MAX_WORK_CLAIM_LOOKUPS])
        return records

    def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        isbn = _digits(q.isbn)
        if not isbn:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/entities/by-uris",
            params={"uris": f"isbn:{isbn}", "attributes": "info|labels|claims"},
        )
        entities = payload.get("entities") if isinstance(payload, Mapping) else None
        if not isinstance(entities, Mapping):
            raise ProviderError(ErrorKind.PARSE, "by-uris response had no entities object")
        records = [
            _normalise_edition(entity)
            for entity in entities.values()
            if isinstance(entity, Mapping)
        ]
        if records:
            self._enrich_editions(ctx, records)
        return records

    def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        """Follow the book's work to wdt:P179 and label the series.

        A traversal can hand us a *series* uri rather than the book's work uri, so
        each candidate id is checked for `wdt:P179` before the title search is
        used as a fallback (never reporting not_found for an id mismatch).
        """

        candidates: list[str] = []
        primary = self._work_uri(ctx, q)
        if primary:
            candidates.append(primary)
        if q.title:
            found = self._work_uri(ctx, Query(title=q.title))
            if found and found not in candidates:
                candidates.append(found)
        for candidate in candidates:
            claim = self._work_claims(ctx, [candidate]).get(candidate) or {}
            series_uris = [str(uri) for uri in claim.get(P_SERIES) or [] if str(uri)]
            if not series_uris:
                continue
            labels = self._labels(ctx, series_uris)
            return [
                SeriesRecord(
                    provider_id=str(uri),
                    name=labels.get(str(uri)) or str(uri),
                    source_url=f"{ENTITY_URL}/{uri}",
                )
                for uri in series_uris
            ]
        return []

    def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        series_uri = q.provider_id or self._series_uri(ctx, q.series)
        if not series_uri:
            return []
        payload = ctx.get_json(f"{BASE_URL}/entities/serie-parts", params={"uri": series_uri})
        parts = payload.get("parts") if isinstance(payload, Mapping) else None
        if parts is None:
            raise ProviderError(ErrorKind.PARSE, "serie-parts response had no parts list")
        if not isinstance(parts, list):
            raise ProviderError(ErrorKind.PARSE, "serie-parts response had a non-list parts")
        parts = [part for part in parts if isinstance(part, Mapping)][:MAX_MEMBER_URIS]
        ctx.note_pagination(1, retrieved=len(parts), total=len(parts))
        member_uris = [str(part.get("uri")) for part in parts if part.get("uri")]
        labels = self._labels(ctx, member_uris[:MAX_MEMBER_TITLE_LOOKUPS])
        if len(member_uris) > MAX_MEMBER_TITLE_LOOKUPS:
            ctx.mark_truncated(
                f"member titles limited to {MAX_MEMBER_TITLE_LOOKUPS} of {len(member_uris)} parts"
            )
        members = [
            SeriesMember(
                title=labels.get(str(part.get("uri"))) or str(part.get("uri") or ""),
                position=str(part["ordinal"]) if part.get("ordinal") is not None else None,
                provider_id=str(part.get("uri") or ""),
                work_type="unknown",  # parts carry no typed classification
                published=str(part["date"]) if part.get("date") else None,
                source_url=f"{ENTITY_URL}/{part.get('uri')}",
            )
            for part in parts
        ]
        series_label = labels.get(series_uri) or q.series or series_uri
        return [
            SeriesRecord(
                provider_id=series_uri,
                name=str(series_label),
                members=members,
                source_url=f"{ENTITY_URL}/{series_uri}",
            )
        ]

    def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        if not q.author:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"search": q.author, "types": "humans", "limit": SEARCH_LIMIT},
            request_class=REQUEST_CLASS_SEARCH,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if results is None:
            raise ProviderError(ErrorKind.PARSE, "search response had no results list")
        records: list[AuthorRecord] = []
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, Mapping):
                continue
            uri = str(item.get("uri") or "")
            records.append(
                AuthorRecord(
                    provider_id=uri,
                    name=str(item.get("label") or ""),
                    source_url=f"{ENTITY_URL}/{uri}" if uri else None,
                )
            )
        return records

    def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        return self._author_catalogue(ctx, q, include_series=False)

    def author_series(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        return self._author_catalogue(ctx, q, include_series=True)

    # -- helpers -----------------------------------------------------------------

    def _author_catalogue(
        self, ctx: OpContext, q: Query, *, include_series: bool
    ) -> list[AuthorRecord]:
        author_uri = q.provider_id
        if not author_uri:
            resolved = self.author_resolve(ctx, q)
            if not resolved:
                return []
            author_uri = resolved[0].provider_id
        payload = ctx.get_json(f"{BASE_URL}/entities/author-works", params={"uri": author_uri})
        if not isinstance(payload, Mapping):
            raise ProviderError(ErrorKind.PARSE, "author-works response was not an object")
        works = [item for item in payload.get("works") or [] if isinstance(item, Mapping)]
        series = [item for item in payload.get("series") or [] if isinstance(item, Mapping)]
        ctx.note_pagination(1, retrieved=len(works), total=len(works))
        work_uris = [str(item.get("uri")) for item in works if item.get("uri")]
        series_uris = [str(item.get("uri")) for item in series if item.get("uri")]
        labels = self._labels(ctx, (series_uris if include_series else work_uris)[:MAX_MEMBER_URIS])
        if include_series:
            refs = [SeriesRef(name=labels.get(uri) or uri, provider_id=uri) for uri in series_uris]
            return [
                AuthorRecord(
                    provider_id=author_uri,
                    name=q.author or "",
                    series=refs,
                    source_url=f"{ENTITY_URL}/{author_uri}",
                )
            ]
        titles = [labels.get(uri) or uri for uri in work_uris]
        return [
            AuthorRecord(
                provider_id=author_uri,
                name=q.author or "",
                works=titles,
                source_url=f"{ENTITY_URL}/{author_uri}",
            )
        ]

    # -- reference following -------------------------------------------------------

    def _work_claims(self, ctx: OpContext, uris: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        """Fetch claims for several works in one call."""

        wanted = [uri for uri in dict.fromkeys(uris) if uri]
        if not wanted:
            return {}
        payload = ctx.get_json(
            f"{BASE_URL}/entities/by-uris",
            params={"uris": _pipe(wanted), "attributes": "info|labels|claims"},
        )
        entities = payload.get("entities") if isinstance(payload, Mapping) else None
        if not isinstance(entities, Mapping):
            # We saw references but could not follow them: an adapter gap.
            ctx.note_unresolved("authors", len(wanted))
            ctx.note_unresolved("series", len(wanted))
            return {}
        return {
            str(uri): _claims(entity)
            for uri, entity in entities.items()
            if isinstance(entity, Mapping)
        }

    def _apply_work_claims(
        self,
        ctx: OpContext,
        records: list[BookRecord],
        work_uris: Iterable[str | None],
    ) -> None:
        """Follow wdt:P629 -> wdt:P50 / wdt:P179 and label both in one call."""

        pairs = list(zip(records, work_uris))
        wanted = [uri for uri in dict.fromkeys(uri for _record, uri in pairs) if uri]
        followed = wanted[:MAX_WORK_CLAIM_LOOKUPS]
        if len(wanted) > len(followed):
            ctx.note_unresolved("authors", len(wanted) - len(followed))
            ctx.note_unresolved("series", len(wanted) - len(followed))
        claims = self._work_claims(ctx, followed)
        labelled = {
            uri
            for claim in claims.values()
            for key in (P_AUTHOR, P_SERIES)
            for uri in claim.get(key) or []
        }
        labels = self._labels(ctx, labelled) if labelled else {}
        for record, work in pairs:
            claim = claims.get(work or "")
            if claim is None:
                if work:
                    ctx.note_unresolved("authors", 1)
                    ctx.note_unresolved("series", 1)
                continue
            author_uris = [str(uri) for uri in claim.get(P_AUTHOR) or []]
            record.author_ids = author_uris
            record.authors = [labels.get(uri) or uri for uri in author_uris]
            series_uris = [str(uri) for uri in claim.get(P_SERIES) or []]
            position = _first(claim.get(P_SERIES_ORDINAL))
            record.series = [
                SeriesRef(
                    name=labels.get(uri) or uri,
                    position=position if index == 0 else None,
                    provider_id=uri,
                )
                for index, uri in enumerate(series_uris)
            ]

    def _enrich_editions(self, ctx: OpContext, records: list[BookRecord]) -> None:
        self._apply_work_claims(
            ctx, records, [record.identifiers.get("work") for record in records]
        )

    def _enrich_works(
        self, ctx: OpContext, records: list[BookRecord], hits: list[Mapping[str, Any]]
    ) -> None:
        # The search hit itself is the work, so its uri is the reference to follow.
        self._apply_work_claims(ctx, records, [str(hit.get("uri") or "") for hit in hits])

    def _labels(self, ctx: OpContext, uris: Iterable[str]) -> dict[str, str]:
        wanted = [uri for uri in dict.fromkeys(uris) if uri]
        if not wanted:
            return {}
        payload = ctx.get_json(
            f"{BASE_URL}/entities/by-uris", params={"uris": _pipe(wanted), "attributes": "info|labels"}
        )
        entities = payload.get("entities") if isinstance(payload, Mapping) else None
        labels: dict[str, str] = {}
        if isinstance(entities, Mapping):
            for uri, entity in entities.items():
                if isinstance(entity, Mapping):
                    label = _label(entity)
                    if label:
                        labels[str(uri)] = label
        return labels

    def _entity(self, payload: Any, uri: str) -> Mapping[str, Any] | None:
        entities = payload.get("entities") if isinstance(payload, Mapping) else None
        if not isinstance(entities, Mapping):
            raise ProviderError(ErrorKind.PARSE, "by-uris response had no entities object")
        entity = entities.get(uri)
        return entity if isinstance(entity, Mapping) else None

    def _work_uri(self, ctx: OpContext, q: Query) -> str | None:
        if q.provider_id:
            return str(q.provider_id)
        if q.isbn:
            records = self.book_by_isbn(ctx, q)
            for record in records:
                work = record.identifiers.get("work")
                if work:
                    return work
        if not q.title:
            return None
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"search": q.title, "types": "works", "limit": 1},
            request_class=REQUEST_CLASS_SEARCH,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if isinstance(results, list) and results and isinstance(results[0], Mapping):
            return str(results[0].get("uri") or "") or None
        return None

    def _series_uri(self, ctx: OpContext, name: str | None) -> str | None:
        if not name:
            return None
        payload = ctx.get_json(
            f"{BASE_URL}/search",
            params={"search": name, "types": "series", "limit": 1},
            request_class=REQUEST_CLASS_SEARCH,
        )
        results = payload.get("results") if isinstance(payload, Mapping) else None
        if isinstance(results, list) and results and isinstance(results[0], Mapping):
            return str(results[0].get("uri") or "") or None
        return None


__all__ = ["InventaireAdapter"]
