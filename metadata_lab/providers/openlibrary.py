"""Open Library adapter. Keyless.

Written from the samples saved in Phase 0 (`metadata_lab/samples/openlibrary/`)
and the documentation recorded in `.ai_docs/metadata-lab-provider-notes.md`.
Open Library indexes each work's series (``series_key`` / ``series_name`` /
``series_position``, parallel lists) in ``search.json``, so series questions are
answered by ONE search request instead of probing editions one by one. A
``/books/<id>`` path given as an explicit id is still read directly (one request).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Mapping, Sequence

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

BASE_URL = "https://openlibrary.org"
SEARCH_FIELDS = (
    "key,title,subtitle,author_name,author_key,first_publish_year,edition_count,"
    "isbn,publisher,publish_date,number_of_pages_median,language,cover_i,edition_key"
)
WORK_FIELDS = "key,title,author_name,author_key,edition_key"
SERIES_FIELDS = (
    "key,title,author_name,author_key,series_key,series_name,series_position"
)
AUTHOR_FIELDS = "key,name,alternate_names"
SEARCH_LIMIT = 5
SERIES_PAGE_LIMIT = 100
WORKS_PAGE_LIMIT = 50

#: `"The Empyrean (#1)"` / `"The Empyrean (#3.5)"` -> name + position.
_SERIES_POSITION = re.compile(r"^(?P<name>.*?)\s*\(#?(?P<position>\d+(?:\.\d+)?)\)\s*$")
_SERIES_POSITION_ONLY = re.compile(r"^#?(?P<position>\d+(?:\.\d+)?)$")
_SERIES_OLID = re.compile(r"^OL\d+L$")


#: Open Library types an edition's ``physical_format`` as free text; map the
#: common values and leave everything else "unknown" rather than guessing.
_FORMAT_HINTS = (
    ("audiobook", "audiobook"),
    ("audio", "audiobook"),
    ("hardcover", "hardcover"),
    ("hardback", "hardcover"),
    ("paperback", "paperback"),
    ("softcover", "paperback"),
    ("ebook", "ebook"),
    ("e-book", "ebook"),
    ("kindle", "ebook"),
)


def _format_from_edition(raw: Mapping[str, Any]) -> str:
    text = str(raw.get("physical_format") or "").strip().lower()
    for marker, value in _FORMAT_HINTS:
        if marker in text:
            return value
    return "unknown"


def _text(value: Any) -> str | None:
    """Open Library sometimes wraps text in ``{"type": …, "value": …}``."""

    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(values: Any) -> str | None:
    if isinstance(values, (list, tuple)):
        for value in values:
            text = _text(value)
            if text:
                return text
        return None
    return _text(values)


def _digits(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _isbn_lists(values: Sequence[Any]) -> tuple[list[str], list[str]]:
    isbn10: list[str] = []
    isbn13: list[str] = []
    for raw in values:
        isbn = _digits(raw)
        if len(isbn) == 13 and isbn not in isbn13:
            isbn13.append(isbn)
        elif len(isbn) == 10 and isbn not in isbn10:
            isbn10.append(isbn)
    return isbn10, isbn13


def _language(values: Any) -> str | None:
    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        key = value.get("key") if isinstance(value, dict) else value
        if key:
            return str(key).rstrip("/").rsplit("/", 1)[-1]
    return None


def _series_refs(values: Any) -> list[SeriesRef]:
    if not isinstance(values, (list, tuple)):
        return []
    refs: list[SeriesRef] = []
    for raw in values:
        text = _text(raw)
        if not text:
            continue
        match = _SERIES_POSITION.match(text)
        if match:
            refs.append(SeriesRef(name=match.group("name").strip(), position=match.group("position")))
            continue
        match = _SERIES_POSITION_ONLY.match(text)
        if match:
            refs.append(SeriesRef(name=text, position=match.group("position")))
            continue
        refs.append(SeriesRef(name=text))
    return refs


def _doc_series(doc: Mapping[str, Any]) -> list[tuple[str, str, str | None]]:
    """``(key, name, position)`` per series of a search doc (the lists are parallel)."""

    keys = doc.get("series_key") if isinstance(doc.get("series_key"), list) else []
    names = doc.get("series_name") if isinstance(doc.get("series_name"), list) else []
    positions = doc.get("series_position") if isinstance(doc.get("series_position"), list) else []
    result: list[tuple[str, str, str | None]] = []
    for index, raw_key in enumerate(keys):
        key = _text(raw_key)
        if not key:
            continue
        name = _text(names[index]) if index < len(names) else None
        position = _text(positions[index]) if index < len(positions) else None
        result.append((key, name or key, position))
    return result


def _position_sort(member: SeriesMember) -> tuple[int, float]:
    try:
        return (0, float(member.position or ""))
    except ValueError:
        return (1, 0.0)


def _olid(value: Any) -> str:
    """Normalise ``OL…`` / ``/authors/OL…`` / ``/works/OL…`` into a path."""

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        return text
    if text.startswith("OL"):
        return text
    return text


def _payload_dict(payload: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.PARSE, f"{what} response was not a JSON object")
    return payload


def _docs(payload: Any, what: str) -> list[Mapping[str, Any]]:
    body = _payload_dict(payload, what)
    docs = body.get("docs")
    if docs is None:
        return []
    if not isinstance(docs, list):
        raise ProviderError(ErrorKind.PARSE, f"{what} response had a non-list 'docs'")
    return [doc for doc in docs if isinstance(doc, Mapping)]


def _normalise_search_doc(raw: Mapping[str, Any]) -> BookRecord:
    """Normalise one ``search.json`` work document."""

    isbn10, isbn13 = _isbn_lists(raw.get("isbn") or [])
    author_ids = ["/authors/" + _digits_or_text(key) for key in raw.get("author_key") or []]
    return BookRecord(
        provider_id=str(raw.get("key") or ""),
        title=_text(raw.get("title")) or "",
        subtitle=_text(raw.get("subtitle")),
        authors=[name for name in (_text(n) for n in raw.get("author_name") or []) if name],
        isbn10=isbn10,
        isbn13=isbn13,
        publisher=_first(raw.get("publisher")),
        published=_first(raw.get("publish_date")),
        pages=raw.get("number_of_pages_median") if isinstance(raw.get("number_of_pages_median"), int) else None,
        language=_first(raw.get("language")),
        cover=bool(raw.get("cover_i")),
        description=None,
        identifiers={"openlibrary": str(raw.get("key") or "")},
        series=_series_refs(raw.get("series")),
        author_ids=[key for key in author_ids if key != "/authors/"],
        work_type="unknown",  # Open Library does not type a work-type in these endpoints
        format="unknown",
        record_level="work",
        first_published=str(raw["first_publish_year"])
        if isinstance(raw.get("first_publish_year"), int)
        else None,
        source_url=f"{BASE_URL}{raw.get('key') or ''}" if raw.get("key") else None,
    )


def _digits_or_text(value: Any) -> str:
    return str(value or "").strip().lstrip("/")


def _normalise_edition(
    raw: Mapping[str, Any], author_names: Mapping[str, str] | None = None
) -> BookRecord:
    """Normalise one edition document (``/isbn/<isbn>.json``, ``/books/<id>.json``)."""

    names = author_names or {}
    author_keys = [
        str(entry.get("key") or "") for entry in raw.get("authors") or [] if isinstance(entry, Mapping)
    ]
    authors: list[str] = []
    for key in author_keys:
        name = names.get(key.lstrip("/"))
        if name:
            authors.append(name)

    identifiers: dict[str, str] = {}
    raw_identifiers = raw.get("identifiers")
    if isinstance(raw_identifiers, Mapping):
        for name, values in raw_identifiers.items():
            text = _first(values)
            if text:
                identifiers[str(name)] = text
    identifiers["openlibrary"] = str(raw.get("key") or "")

    covers = [value for value in raw.get("covers") or [] if isinstance(value, int)]
    works = raw.get("works") or []
    work_key = str(works[0].get("key") or "") if works and isinstance(works[0], Mapping) else ""

    return BookRecord(
        provider_id=str(raw.get("key") or work_key),
        title=_text(raw.get("title")) or "",
        subtitle=_text(raw.get("subtitle")),
        authors=authors,
        isbn10=[_digits(value) for value in raw.get("isbn_10") or []],
        isbn13=[_digits(value) for value in raw.get("isbn_13") or []],
        publisher=_first(raw.get("publishers")),
        published=_text(raw.get("publish_date")),
        pages=raw.get("number_of_pages") if isinstance(raw.get("number_of_pages"), int) else None,
        language=_language(raw.get("languages")),
        cover=any(value > 0 for value in covers),
        description=_text(raw.get("description")),
        identifiers=identifiers,
        series=_series_refs(raw.get("series")),
        author_ids=[key for key in author_keys if key],
        work_type="unknown",  # editions carry no typed work classification
        format=_format_from_edition(raw),
        record_level="edition",
        first_published=None,  # an edition date is not a first-publication date
        source_url=f"{BASE_URL}{raw.get('key') or ''}" if raw.get("key") else None,
    )


class OpenLibraryAdapter(ProviderAdapter):
    id: ClassVar[str] = "openlibrary"
    display_name: ClassVar[str] = "Open Library"
    adapter_version: ClassVar[str] = "3"
    credential_env: ClassVar[str | None] = None  # keyless
    #: Verified by the live Fourth Wing run on 2026-11-14 (six operations answered).
    VERIFIED_LIVE: ClassVar[bool] = True
    #: Published limit: 1 req/s unidentified, 3 req/s with an identifying
    #: User-Agent (https://openlibrary.org/developers/api). 2 s is the research
    #: doc's engineering margin between the two, not an Open Library quota.
    min_interval_s: ClassVar[float] = 2.0

    #: Nothing is declared unsupported. ``author_series`` has no dedicated endpoint; it is
    #: derived from the author-filtered ``search.json`` (one paginated list, no per-work calls)
    #: and labelled as derived in the row notes.
    unsupported: ClassVar[dict[Operation, Unsupported]] = {}

    def check_connection(self, ctx: OpContext) -> str:
        payload = ctx.get_json(
            f"{BASE_URL}/search.json",
            params={"q": "the lord of the rings", "limit": 1, "fields": "key,title"},
        )
        docs = _docs(payload, "search")
        return f"{self.display_name} search responded ({len(docs)} result(s))"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        if not q.title:
            return []
        params: dict[str, Any] = {
            "title": q.title,
            "limit": SEARCH_LIMIT,
            "fields": SEARCH_FIELDS,
        }
        if q.author:
            params["author"] = q.author
        payload = ctx.get_json(f"{BASE_URL}/search.json", params=params)
        return [_normalise_search_doc(doc) for doc in _docs(payload, "search")]

    def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        isbn = _digits(q.isbn)
        if not isbn:
            return []
        try:
            payload = ctx.get_json(f"{BASE_URL}/isbn/{isbn}.json")
        except ProviderError as exc:
            # An unknown ISBN is "nothing found", not a provider failure.
            if exc.kind is ErrorKind.HTTP and "HTTP 404" in exc.message:
                return []
            raise
        raw = _payload_dict(payload, "edition")
        return [_normalise_edition(raw, self._author_names(ctx, raw))]

    def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        if q.provider_id:
            # An explicit id is one direct read of that edition's free-text ``series``.
            path = str(q.provider_id)
            if not path.startswith("/"):
                path = f"/books/{path}" if path.startswith("OL") else path
            raw = _payload_dict(ctx.get_json(f"{BASE_URL}{path}.json"), "edition")
            refs = _series_refs(raw.get("series"))
            authors = [
                str(entry.get("key") or "")
                for entry in raw.get("authors") or []
                if isinstance(entry, Mapping)
            ]
            return [
                SeriesRecord(provider_id=ref.name, name=ref.name, authors=authors, members=[])
                for ref in refs
            ]
        params: dict[str, Any] = {"limit": 1, "fields": SERIES_FIELDS}
        if q.isbn:
            params["q"] = f"isbn:{_digits(q.isbn)}"
        elif q.title:
            params["title"] = q.title
            if q.author:
                params["author"] = q.author
        else:
            return []
        docs = _docs(ctx.get_json(f"{BASE_URL}/search.json", params=params), "series search")
        if not docs:
            return []
        doc = docs[0]
        authors = [f"/authors/{_digits_or_text(key)}" for key in doc.get("author_key") or []]
        records = [
            SeriesRecord(
                provider_id=key,
                name=name,
                authors=authors,
                members=[],
                source_url=f"{BASE_URL}/series/{key}",
            )
            for key, name, _position in _doc_series(doc)
        ]
        if not records:
            ctx.note("the top search hit carries no series data")
        return records

    def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
        handle = (q.provider_id or "").strip()
        keyed = bool(_SERIES_OLID.match(handle))
        name = q.series or (None if keyed else handle) or ""
        if not keyed and not name:
            return []
        query = f"series_key:{handle}" if keyed else f'series_name:"{name}"'
        wanted = name.casefold()
        groups: dict[str, tuple[str, list[SeriesMember]]] = {}
        complete = False
        retrieved = 0
        for page in range(1, ctx.max_pages + 1):
            payload = ctx.get_json(
                f"{BASE_URL}/search.json",
                params={
                    "q": query,
                    "page": page,
                    "limit": SERIES_PAGE_LIMIT,
                    "fields": SERIES_FIELDS,
                },
            )
            docs = _docs(payload, "series search")
            retrieved += len(docs)
            for doc in docs:
                for key, series_name, position in _doc_series(doc):
                    # The phrase query can match a longer series name; keep exact matches.
                    if (keyed and key != handle) or (not keyed and series_name.casefold() != wanted):
                        continue
                    members = groups.setdefault(key, (series_name, []))[1]
                    members.append(
                        SeriesMember(
                            title=_text(doc.get("title")) or "",
                            position=position,
                            provider_id=str(doc.get("key") or ""),
                            authors=[
                                n for n in (_text(a) for a in doc.get("author_name") or []) if n
                            ],
                            # No typed classification or date in this response.
                            work_type="unknown",
                            source_url=f"{BASE_URL}{doc.get('key')}" if doc.get("key") else None,
                        )
                    )
            num_found = payload.get("numFound") if isinstance(payload, Mapping) else None
            ctx.note_pagination(
                page,
                size=SERIES_PAGE_LIMIT,
                total=num_found if isinstance(num_found, int) else None,
                retrieved=retrieved,
            )
            if len(docs) < SERIES_PAGE_LIMIT or (
                isinstance(num_found, int) and page * SERIES_PAGE_LIMIT >= num_found
            ):
                complete = True
                break
        if not complete and groups:
            ctx.mark_truncated(f"stopped after {ctx.max_pages} page(s) of series {handle or name!r}")
        return [
            SeriesRecord(
                provider_id=key,
                name=series_name,
                members=sorted(members, key=_position_sort),
                source_url=f"{BASE_URL}/series/{key}",
            )
            for key, (series_name, members) in groups.items()
        ]

    def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        if not q.author:
            return []
        payload = ctx.get_json(
            f"{BASE_URL}/search/authors.json",
            params={"q": q.author, "limit": SEARCH_LIMIT},
        )
        records: list[AuthorRecord] = []
        for doc in _docs(payload, "author search"):
            key = _digits_or_text(doc.get("key"))
            if not key:
                continue
            aliases = [_text(name) for name in doc.get("alternate_names") or []]
            records.append(
                AuthorRecord(
                    provider_id=f"/authors/{key}",
                    name=_text(doc.get("name")) or "",
                    aliases=[alias for alias in aliases if alias],
                    source_url=f"{BASE_URL}/authors/{key}",
                )
            )
        return records

    def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        author_path = ""
        if q.provider_id:
            author_path = self._author_path(q.provider_id)
        else:
            resolved = self.author_resolve(ctx, q)
            if resolved:
                author_path = resolved[0].provider_id
        if not author_path:
            return []

        works: list[str] = []
        total: int | None = None
        complete = False
        for page in range(ctx.max_pages):
            payload = ctx.get_json(
                f"{BASE_URL}{author_path}/works.json",
                params={"limit": WORKS_PAGE_LIMIT, "offset": page * WORKS_PAGE_LIMIT},
            )
            body = _payload_dict(payload, "author works")
            entries = body.get("entries")
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise ProviderError(ErrorKind.PARSE, "author works response had non-list 'entries'")
            works.extend(
                title
                for title in (
                    _text(entry.get("title")) for entry in entries if isinstance(entry, Mapping)
                )
                if title
            )
            total = body.get("size") if isinstance(body.get("size"), int) else None
            ctx.note_pagination(
                page + 1, size=WORKS_PAGE_LIMIT, total=total, retrieved=len(works)
            )
            if len(entries) < WORKS_PAGE_LIMIT or (total is not None and len(works) >= total):
                complete = True
                break
        if not complete and works:
            ctx.mark_truncated(f"stopped after {ctx.max_pages} page(s) of works for {author_path}")
        if not works:
            return []
        return [
            AuthorRecord(
                provider_id=author_path,
                name=q.author or "",
                works=works,
                source_url=f"{BASE_URL}{author_path}/works.json",
            )
        ]

    def author_series(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
        """Series of an author, **derived** from the author-filtered work search.

        Open Library has no author-to-series endpoint. ``search.json?author=`` returns each
        work with its ``series_key``/``series_name`` fields, so one bounded, paginated list is
        read and the series are de-duplicated by their Open Library series key (``OL...L``),
        never by name. Only works whose ``author_key`` contains the resolved author count, so a
        namesake's works are ignored. A work without series fields is not evidence that it has
        no series, so absence from this list proves nothing.
        """

        author_path = self._author_path(q.provider_id) if q.provider_id else ""
        name = q.author or ""
        if not author_path:
            resolved = self.author_resolve(ctx, q)
            if resolved:
                author_path = resolved[0].provider_id
                name = name or resolved[0].name
        if not author_path or not name:
            return []
        author_olid = author_path.rsplit("/", 1)[-1]

        series: dict[str, str] = {}
        retrieved = 0
        total: int | None = None
        complete = False
        for page in range(1, ctx.max_pages + 1):
            payload = ctx.get_json(
                f"{BASE_URL}/search.json",
                params={
                    "author": name,
                    "fields": SERIES_FIELDS,
                    "limit": SERIES_PAGE_LIMIT,
                    "page": page,
                },
            )
            docs = _docs(payload, "author series search")
            body = _payload_dict(payload, "author series search")
            total = body.get("numFound") if isinstance(body.get("numFound"), int) else None
            retrieved += len(docs)
            for doc in docs:
                if author_olid not in [str(key) for key in doc.get("author_key") or []]:
                    continue
                for key, series_name, _position in _doc_series(doc):
                    series.setdefault(key, series_name)
            ctx.note_pagination(page, size=SERIES_PAGE_LIMIT, total=total, retrieved=retrieved)
            if len(docs) < SERIES_PAGE_LIMIT or (total is not None and retrieved >= total):
                complete = True
                break
        if not complete:
            ctx.mark_truncated(f"stopped after {ctx.max_pages} page(s) of works for {author_path}")
        ctx.notes.append(
            "derived: series collected from the author-filtered work search and de-duplicated by "
            "Open Library series key; works without series fields say nothing about absence"
        )
        if not series:
            return []
        return [
            AuthorRecord(
                provider_id=author_path,
                name=name,
                series=[
                    SeriesRef(name=series_name, provider_id=key) for key, series_name in series.items()
                ],
                source_url=f"{BASE_URL}/search.json?author={name}",
            )
        ]

    # -- helpers -----------------------------------------------------------------

    def _author_path(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("/authors/"):
            return text
        if text.startswith("OL"):
            return f"/authors/{text}"
        return text

    def _author_names(self, ctx: OpContext, edition: Mapping[str, Any]) -> dict[str, str]:
        """Edition documents carry author *keys* only; resolve up to three names."""

        names: dict[str, str] = {}
        keys = [
            str(entry.get("key") or "")
            for entry in edition.get("authors") or []
            if isinstance(entry, Mapping)
        ][:3]
        for key in keys:
            path = self._author_path(key)
            if not path:
                continue
            try:
                payload = ctx.get_json(f"{BASE_URL}{path}.json")
            except ProviderError as exc:
                # Enrichment only: a failed name lookup must not lose the record.
                ctx.note(f"author lookup failed for {path}: {exc.kind.value}")
                continue
            name = _text(_payload_dict(payload, "author").get("name"))
            if name:
                names[path.lstrip("/")] = name
        return names


__all__ = ["OpenLibraryAdapter"]
