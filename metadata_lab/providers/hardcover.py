"""Hardcover adapter. Keyed (Personal Access Token), GraphQL.

Built from the documentation recorded in the provider notes. Queries are fixed
templates with variables — user input never becomes part of the query text. No
credential is configured in this environment, so the adapter is
``VERIFIED_LIVE = False`` and its rows report ``not_configured``.
"""

from __future__ import annotations

import json
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
from .base import ProviderAdapter

GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
SITE_URL = "https://hardcover.app"

SEARCH_QUERY = """
query MetadataLabSearch($query: String!, $type: String!, $perPage: Int!) {
  search(query: $query, query_type: $type, per_page: $perPage) {
    ids
    results
  }
}
"""

EDITION_BY_ISBN_QUERY = """
query MetadataLabEditionByIsbn($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}, limit: 5) {
    id
    title
    isbn_10
    isbn_13
    pages
    release_date
    edition_format
    publisher { name }
    book {
      id
      title
      subtitle
      release_date
      slug
      book_series(where: {compilation: {_eq: false}}) {
        position
        series { id name }
      }
    }
  }
}
"""

BOOK_SERIES_QUERY = """
query MetadataLabBookSeries($title: String!) {
  books(where: {title: {_eq: $title}}, limit: 5) {
    id
    title
    book_series(where: {book: {canonical_id: {_is_null: true}}}) {
      position
      compilation
      details
      series { id name }
    }
  }
}
"""

SERIES_MEMBERS_QUERY = """
query MetadataLabSeriesMembers($name: String!) {
  series(where: {name: {_eq: $name}, books_count: {_gt: 0}}, limit: 5) {
    id
    name
    books_count
    primary_books_count
    book_series(
      where: {
        book: {canonical_id: {_is_null: true}, is_partial_book: {_eq: false}}
        compilation: {_eq: false}
      }
      order_by: [{position: asc}, {book: {users_count: desc}}]
    ) {
      position
      details
      compilation
      book { id title slug }
    }
  }
}
"""

AUTHOR_WORKS_QUERY = """
query MetadataLabAuthorWorks($slug: String!) {
  authors(where: {slug: {_eq: $slug}}, limit: 1) {
    id
    name
    contributions(
      where: {
        contributable_type: {_eq: "Book"}
        contributor_role: {slug: {_eq: "author"}}
        book: {canonical_id: {_is_null: true}, is_partial_book: {_eq: false}}
      }
      order_by: {book: {users_count: desc}}
      limit: 100
    ) {
      book { id title slug release_date }
    }
  }
}
"""

AUTHOR_SERIES_QUERY = """
query MetadataLabAuthorSeries($slug: String!) {
  series(where: {author: {slug: {_eq: $slug}}}, limit: 100) {
    id
    name
  }
}
"""

#: Hardcover's own edition_format vocabulary -> our controlled `format` values.
_FORMAT_MAP = {
    "hardcover": "hardcover",
    "paperback": "paperback",
    "ebook": "ebook",
    "e-book": "ebook",
    "audiobook": "audiobook",
    "audio": "audiobook",
}


def _payload_data(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, "GraphQL response was not an object")
    if payload.get("errors"):
        raise ProviderError(ErrorKind.HTTP, "GraphQL returned errors")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ProviderError(ErrorKind.PARSE, "GraphQL response had no data object")
    return data


def _search_results(payload: Any) -> list[Mapping[str, Any]]:
    data = _payload_data(payload)
    search = data.get("search")
    if not isinstance(search, Mapping):
        return []
    results = search.get("results")
    if isinstance(results, str):
        # Typesense-backed field: a JSON scalar rather than an object.
        try:
            results = json.loads(results)
        except json.JSONDecodeError:
            raise ProviderError(ErrorKind.PARSE, "search results were not valid JSON") from None
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, Mapping)]


def _format(value: Any) -> str:
    return _FORMAT_MAP.get(str(value or "").strip().lower(), "unknown")


class HardcoverAdapter(ProviderAdapter):
    id: ClassVar[str] = "hardcover"
    display_name: ClassVar[str] = "Hardcover"
    adapter_version: ClassVar[str] = "1"
    credential_env: ClassVar[str | None] = "FAMILY_LIBRARIAN_LAB_HARDCOVER_TOKEN"
    credential_env_aliases: ClassVar[tuple[str, ...]] = ()
    VERIFIED_LIVE: ClassVar[bool] = False
    #: Free plan allows 60 requests/minute; one per second stays well inside it.
    min_interval_s: ClassVar[float] = 1.0
    unsupported: ClassVar[dict[Operation, Any]] = {}

    def check_connection(self, ctx: OpContext) -> str:
        payload = self._post(
            ctx,
            "query MetadataLabMe { me { id username } }",
            {},
        )
        data = _payload_data(payload)
        me = data.get("me")
        return f"{self.display_name} answered an authenticated query ({'yes' if me else 'no'} profile)"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        if not q.title:
            return []
        results = _search_results(
            self._post(ctx, SEARCH_QUERY, {"query": q.title, "type": "book", "perPage": 5})
        )
        records: list[BookRecord] = []
        for item in results:
            isbns = item.get("isbns") or []
            isbn10 = [str(v) for v in isbns if len("".join(c for c in str(v) if c.isdigit())) == 10]
            isbn13 = [str(v) for v in isbns if len("".join(c for c in str(v) if c.isdigit())) == 13]
            series_refs: list[SeriesRef] = []
            # `series_names` is the documented list of series names on a search
            # hit; `featured_series_position` only describes the first one.
            names = [str(name) for name in item.get("series_names") or []]
            position = item.get("featured_series_position")
            for index, series_name in enumerate(names):
                series_refs.append(
                    SeriesRef(
                        name=series_name,
                        position=str(position)
                        if index == 0 and position is not None
                        else None,
                    )
                )
            records.append(
                BookRecord(
                    provider_id=str(item.get("slug") or item.get("id") or ""),
                    title=str(item.get("title") or ""),
                    subtitle=str(item["subtitle"]) if item.get("subtitle") else None,
                    authors=[str(name) for name in item.get("author_names") or []],
                    isbn10=isbn10,
                    isbn13=isbn13,
                    pages=item.get("pages") if isinstance(item.get("pages"), int) else None,
                    published=str(item["release_year"]) if item.get("release_year") else None,
                    description=str(item["description"]) if item.get("description") else None,
                    series=series_refs,
                    record_level="work",
                    # Hardcover types nothing about work type in the search hit.
                    work_type="unknown",
                    source_url=f"{SITE_URL}/books/{item.get('slug')}" if item.get("slug") else None,
                )
            )
        return records

    def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        isbn = "".join(char for char in str(q.isbn or "") if char.isdigit() or char == "X")
        if not isbn:
            return []
        data = _payload_data(
            self._post(ctx, EDITION_BY_ISBN_QUERY, {"isbn": isbn})
        )
        editions = data.get("editions")
        records: list[BookRecord] = []
        for edition in editions if isinstance(editions, list) else []:
            if not isinstance(edition, Mapping):
                continue
            book = edition.get("book") if isinstance(edition.get("book"), Mapping) else {}
            publisher = edition.get("publisher")
            series_refs = [
                SeriesRef(
                    name=str(link["series"]["name"]),
                    position=str(link["position"]) if link.get("position") is not None else None,
                    provider_id=str(link["series"].get("id")) if link.get("series") else None,
                )
                for link in book.get("book_series") or []
                if isinstance(link, Mapping) and isinstance(link.get("series"), Mapping)
            ]
            records.append(
                BookRecord(
                    provider_id=str(edition.get("id") or ""),
                    title=str(book.get("title") or edition.get("title") or ""),
                    subtitle=str(book["subtitle"]) if book.get("subtitle") else None,
                    isbn10=[str(v) for v in edition.get("isbn_10") or []],
                    isbn13=[str(v) for v in edition.get("isbn_13") or []],
                    publisher=str(publisher["name"]) if isinstance(publisher, Mapping) else None,
                    published=str(edition.get("release_date") or book.get("release_date") or "") or None,
                    pages=edition.get("pages") if isinstance(edition.get("pages"), int) else None,
                    series=series_refs,
                    record_level="edition",
                    format=_format(edition.get("edition_format")),
                    work_type="unknown",
                    source_url=f"{SITE_URL}/books/{book.get('slug')}" if book.get("slug") else None,
                )
            )
        return records

    def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        if not q.title:
            return []
        data = _payload_data(self._post(ctx, BOOK_SERIES_QUERY, {"title": q.title}))
        books = data.get("books")
        records: list[SeriesRecord] = []
        for book in books if isinstance(books, list) else []:
            if not isinstance(book, Mapping):
                continue
            for link in book.get("book_series") or []:
                if not isinstance(link, Mapping) or not isinstance(link.get("series"), Mapping):
                    continue
                series = link["series"]
                records.append(
                    SeriesRecord(
                        provider_id=str(series.get("id") or ""),
                        name=str(series.get("name") or ""),
                        source_url=f"{SITE_URL}/series/{series.get('slug') or series.get('id')}",
                    )
                )
        return records

    def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        name = q.series or q.provider_id
        if not name:
            return []
        data = _payload_data(self._post(ctx, SERIES_MEMBERS_QUERY, {"name": name}))
        series_list = data.get("series")
        records: list[SeriesRecord] = []
        for series in series_list if isinstance(series_list, list) else []:
            if not isinstance(series, Mapping):
                continue
            members: list[SeriesMember] = []
            for link in series.get("book_series") or []:
                if not isinstance(link, Mapping) or not isinstance(link.get("book"), Mapping):
                    continue
                position = link.get("position")
                members.append(
                    SeriesMember(
                        title=str(link["book"].get("title") or ""),
                        # `position` is a string; compilations carry `details` like "1-3".
                        position=str(position) if position is not None else None,
                        provider_id=str(link["book"].get("id") or ""),
                        work_type="unknown",  # not typed in this response
                        source_url=f"{SITE_URL}/books/{link['book'].get('slug')}"
                        if link["book"].get("slug")
                        else None,
                    )
                )
            primary = series.get("primary_books_count")
            ctx.note_pagination(
                1,
                retrieved=len(members),
                total=primary if isinstance(primary, int) else None,
            )
            records.append(
                SeriesRecord(
                    provider_id=str(series.get("id") or ""),
                    name=str(series.get("name") or name),
                    members=members,
                    ordering_scheme="provider_default",
                    source_url=f"{SITE_URL}/series/{series.get('slug') or series.get('id')}",
                )
            )
        return records

    def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        if not q.author:
            return []
        results = _search_results(
            self._post(ctx, SEARCH_QUERY, {"query": q.author, "type": "author", "perPage": 5})
        )
        return [
            AuthorRecord(
                provider_id=str(item.get("slug") or item.get("id") or ""),
                name=str(item.get("name") or ""),
                aliases=[str(name) for name in item.get("alternate_names") or []],
                source_url=f"{SITE_URL}/authors/{item.get('slug')}" if item.get("slug") else None,
            )
            for item in results
        ]

    def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        slug = q.provider_id or self._author_slug(ctx, q.author)
        if not slug:
            return []
        data = _payload_data(self._post(ctx, AUTHOR_WORKS_QUERY, {"slug": slug}))
        authors = data.get("authors")
        records: list[AuthorRecord] = []
        for author in authors if isinstance(authors, list) else []:
            if not isinstance(author, Mapping):
                continue
            titles = [
                str(contribution["book"].get("title") or "")
                for contribution in author.get("contributions") or []
                if isinstance(contribution, Mapping)
                and isinstance(contribution.get("book"), Mapping)
            ]
            ctx.note_pagination(1, retrieved=len(titles), total=None)
            records.append(
                AuthorRecord(
                    provider_id=str(author.get("id") or slug),
                    name=str(author.get("name") or q.author or ""),
                    works=titles,
                    source_url=f"{SITE_URL}/authors/{slug}",
                )
            )
        return records

    def author_series(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        slug = q.provider_id or self._author_slug(ctx, q.author)
        if not slug:
            return []
        data = _payload_data(self._post(ctx, AUTHOR_SERIES_QUERY, {"slug": slug}))
        series_list = data.get("series")
        refs = [
            SeriesRef(name=str(item.get("name") or ""), provider_id=str(item.get("id") or ""))
            for item in series_list if isinstance(item, Mapping)
        ] if isinstance(series_list, list) else []
        return [
            AuthorRecord(
                provider_id=slug,
                name=q.author or "",
                series=refs,
                source_url=f"{SITE_URL}/authors/{slug}",
            )
        ]

    # -- helpers -----------------------------------------------------------------

    def _post(self, ctx: OpContext, query: str, variables: Mapping[str, Any]) -> Any:
        if not ctx.credential:
            raise ProviderError(ErrorKind.AUTH, "no Hardcover token configured")
        return ctx.post_json(
            GRAPHQL_URL,
            {"query": query, "variables": dict(variables)},
            secret_headers={"authorization": f"Bearer {ctx.credential}"},
        )

    def _author_slug(self, ctx: OpContext, name: str | None) -> str | None:
        if not name:
            return None
        resolved = self.author_resolve(ctx, Query(author=name))
        return resolved[0].provider_id if resolved else None


__all__ = ["HardcoverAdapter"]
