"""Google Books adapter. Keyed (API key as a ``key=`` query parameter).

Built from the documentation recorded in the provider notes. First answered live
on 2026-11-15 (``book_by_title`` only; see the provider notes); without a key its
rows report ``not_configured``. The key travels as a **secret parameter**, so
the HTTP helper never records it.
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
)
from .base import ProviderAdapter, Unsupported

BASE_URL = "https://www.googleapis.com/books/v1/volumes"
MAX_RESULTS = 20

_UNSUPPORTED = {
    Operation.AUTHOR_SERIES: Unsupported(
        reason=(
            "No documented author-to-series query. Books does have series data (volumeInfo.seriesInfo, "
            "series.get and series.membership.get), but nothing takes an author: volumes.list has no "
            "series or author-id parameter, so series for an author would need a per-volume lookup "
            "that this lab does not perform. Whether volumes.list returns usable seriesInfo is "
            "unverified."
        ),
        source_url="https://developers.google.com/books/docs/v1/reference/volumes/list",
    ),
    Operation.AUTHOR_RESOLVE: Unsupported(
        reason=(
            "The Books API has no author resource. volumes.list takes no author identifier (the "
            "inauthor: operator only narrows volume search) and the Volume resource exposes no "
            "author id, alias list or disambiguation field, so authors cannot be resolved."
        ),
        source_url="https://developers.google.com/books/docs/v1/reference/volumes",
    ),
}


def _identifier(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit() or char.upper() == "X")


def _items(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "volumes response was not an object")
    items = payload.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ProviderError(ErrorKind.PARSE, "volumes response had non-list items")
    return [item for item in items if isinstance(item, Mapping)]


def _normalise_volume(item: Mapping[str, Any]) -> BookRecord:
    info = item.get("volumeInfo") if isinstance(item.get("volumeInfo"), Mapping) else {}
    sale = item.get("saleInfo") if isinstance(item.get("saleInfo"), Mapping) else {}
    isbn10: list[str] = []
    isbn13: list[str] = []
    identifiers: dict[str, str] = {}
    for entry in info.get("industryIdentifiers") or []:
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("type") or "")
        value = _identifier(entry.get("identifier"))
        if kind == "ISBN_10" and value:
            isbn10.append(value)
        elif kind == "ISBN_13" and value:
            isbn13.append(value)
        elif value:
            identifiers[kind.lower()] = value
    image_links = info.get("imageLinks") if isinstance(info.get("imageLinks"), Mapping) else {}
    volume_id = str(item.get("id") or "")
    return BookRecord(
        provider_id=volume_id,
        title=str(info.get("title") or ""),
        subtitle=str(info["subtitle"]) if info.get("subtitle") else None,
        authors=[str(name) for name in info.get("authors") or []],
        isbn10=isbn10,
        isbn13=isbn13,
        publisher=str(info["publisher"]) if info.get("publisher") else None,
        published=str(info["publishedDate"]) if info.get("publishedDate") else None,
        pages=info.get("pageCount") if isinstance(info.get("pageCount"), int) else None,
        language=str(info["language"]) if info.get("language") else None,
        cover=bool(image_links.get("thumbnail") or image_links.get("smallThumbnail")),
        description=str(info["description"]) if info.get("description") else None,
        identifiers=identifiers,
        record_level="edition",
        # The Volume resource types no work classification.
        work_type="unknown",
        # `saleInfo.isEbook` is the only format signal the API supplies.
        format="ebook" if sale.get("isEbook") is True else "unknown",
        source_url=str(info["infoLink"]) if info.get("infoLink") else None,
    )


class GoogleBooksAdapter(ProviderAdapter):
    id: ClassVar[str] = "googlebooks"
    display_name: ClassVar[str] = "Google Books"
    adapter_version: ClassVar[str] = "1"
    credential_env: ClassVar[str | None] = "FAMILY_LIBRARIAN_LAB_GOOGLEBOOKS_KEY"
    credential_env_aliases: ClassVar[tuple[str, ...]] = ("FAMILY_LIBRARIAN_GOOGLE_BOOKS_API_KEY",)
    VERIFIED_LIVE: ClassVar[bool] = True
    #: No published Books-specific quota: the effective limit is the Cloud project's
    #: (https://developers.google.com/books/docs/v1/using,
    #: https://docs.cloud.google.com/apis/docs/capping-api-usage), and the lab has
    #: seen only an unkeyed "quota_limit_value: 0" 429. 3 s is lab judgement, not a
    #: Google-published allowance.
    min_interval_s: ClassVar[float] = 3.0
    unsupported: ClassVar[dict[Operation, Unsupported]] = _UNSUPPORTED

    def check_connection(self, ctx: OpContext) -> str:
        payload = self._get(ctx, {"q": "isbn:9781649374042", "maxResults": 1})
        return f"{self.display_name} volumes responded ({len(_items(payload))} item(s))"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        if not q.title:
            return []
        query = f'intitle:"{q.title}"'
        if q.author:
            query += f' inauthor:"{q.author}"'
        payload = self._get(ctx, {"q": query, "maxResults": MAX_RESULTS})
        items = _items(payload)
        ctx.note_pagination(
            1,
            size=MAX_RESULTS,
            total=payload.get("totalItems") if isinstance(payload.get("totalItems"), int) else None,
            retrieved=len(items),
        )
        return [_normalise_volume(item) for item in items]

    def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        isbn = _identifier(q.isbn)
        if not isbn:
            return []
        payload = self._get(ctx, {"q": f"isbn:{isbn}", "maxResults": MAX_RESULTS})
        items = _items(payload)
        ctx.note_pagination(
            1,
            size=MAX_RESULTS,
            total=payload.get("totalItems") if isinstance(payload.get("totalItems"), int) else None,
            retrieved=len(items),
        )
        return [_normalise_volume(item) for item in items]

    def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        """Name-based volume search: the Books API has no author entity."""

        if not q.author:
            return []
        records: list[BookRecord] = []
        start_index = 0
        total: int | None = None
        complete = False
        for page in range(ctx.max_pages):
            payload = self._get(
                ctx,
                {"q": f'inauthor:"{q.author}"', "maxResults": MAX_RESULTS, "startIndex": start_index},
            )
            items = _items(payload)
            records.extend(_normalise_volume(item) for item in items)
            total = payload.get("totalItems") if isinstance(payload.get("totalItems"), int) else None
            ctx.note_pagination(page + 1, size=MAX_RESULTS, total=total, retrieved=len(records))
            start_index += len(items)
            if len(items) < MAX_RESULTS or (total is not None and start_index >= total):
                complete = True
                break
        if not complete and records:
            ctx.mark_truncated(f"stopped after {ctx.max_pages} page(s) of volumes")
        if not records:
            return []
        return [
            AuthorRecord(
                provider_id="",
                name=q.author,
                works=[record.title for record in records if record.title],
                source_url=f"{BASE_URL}?q=inauthor:\"{q.author}\"",
            )
        ]

    # -- helpers -----------------------------------------------------------------

    def _get(self, ctx: OpContext, params: Mapping[str, Any]) -> Any:
        if not ctx.credential:
            raise ProviderError(ErrorKind.AUTH, "no Google Books key configured")
        return ctx.get_json(
            BASE_URL,
            params=dict(params),
            secret_params={"key": ctx.credential},
        )


__all__ = ["GoogleBooksAdapter"]
