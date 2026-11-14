"""Enums and dataclasses shared by every part of the tool.

Everything here is plain data so a result document can be written to JSON and
read back without transformation. Types are intentionally small and
comparison-oriented: this is not a canonical metadata model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar


class Operation(str, Enum):
    """A provider-neutral operation an adapter can support."""

    BOOK_BY_TITLE = "book_by_title"
    BOOK_BY_ISBN = "book_by_isbn"
    SERIES_FOR_BOOK = "series_for_book"
    SERIES_MEMBERS = "series_members"
    AUTHOR_RESOLVE = "author_resolve"
    AUTHOR_WORKS = "author_works"
    AUTHOR_SERIES = "author_series"


class Outcome(str, Enum):
    """The honest outcome vocabulary from the short plan.

    ``DIFFERS`` is never "wrong": the fixture may be wrong or the sources may
    legitimately disagree. ``PARTIAL`` means usable data came back but some
    expected item did not, so ``not_found`` is never used for an operation that
    returned data. ``NEEDS_REVIEW`` means the tool cannot score it automatically
    (manual-evaluation fact, expired time-sensitive reference, or nothing to
    classify) and is excluded from measurement denominators.
    """

    OK = "ok"
    DIFFERS = "differs_from_reference"
    UNSUPPORTED = "unsupported"
    NOT_IMPLEMENTED = "not_implemented"
    NOT_CONFIGURED = "not_configured"
    NOT_FOUND = "not_found"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"
    NOT_RUN = "not_run"


#: Controlled vocabulary for work classification. Adapters map **only** from a
#: field the provider itself types; a title is never used to guess a type.
WORK_TYPES = frozenset(
    {
        "novel",
        "novella",
        "short_story",
        "collection",
        "omnibus",
        "boxed_set",
        "play",
        "graphic_novel",
        "adaptation",
        "other",
        "unknown",
    }
)
WORK_TYPE_UNKNOWN = "unknown"

#: Controlled vocabulary for a book's physical/digital format.
FORMATS = frozenset({"hardcover", "paperback", "ebook", "audiobook", "unknown"})
FORMAT_UNKNOWN = "unknown"

#: Whether a record describes a work or a specific edition.
RECORD_LEVELS = frozenset({"work", "edition", "unknown"})
RECORD_LEVEL_UNKNOWN = "unknown"

#: Allowed ``reason`` values on a ``not_run`` row.
NOT_RUN_REASONS = frozenset({"excluded", "cancelled", "chain_broken", "not_requested"})


class ErrorKind(str, Enum):
    """Classification for ``Outcome.ERROR``."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    HTTP = "http"
    PARSE = "parse"
    NETWORK = "network"


class Mode(str, Enum):
    """How a result *row* was produced."""

    INDEPENDENT = "independent"
    TRAVERSAL = "traversal"


class RunMode(str, Enum):
    """How the whole run was produced (``run.mode`` in the result document)."""

    LIVE = "live"
    REPLAY = "replay"


def _json_value(value: Any) -> Any:
    """Convert enums, dataclasses and containers into JSON-safe values."""

    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _as_dict(instance: Any) -> dict[str, Any]:
    return {f.name: _json_value(getattr(instance, f.name)) for f in fields(instance)}


class _Record:
    """Mixin giving every record a kind and a JSON-friendly ``to_dict``."""

    KIND: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        data = _as_dict(self)
        if not self.KIND:
            return data
        # ``SeriesMember.kind`` already means "novel/novella/supplementary", so a
        # single ``kind`` key cannot identify the record type for every record.
        # ``record_type`` is always present and is the reliable discriminator;
        # ``kind`` is the plan's discriminator wherever the name is free.
        payload: dict[str, Any] = {"kind": self.KIND, "record_type": self.KIND}
        payload.update(data)
        return payload


@dataclass
class Query:
    """Provider-neutral input. ``provider_id`` means "use this provider-native id"."""

    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    series: str | None = None
    provider_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _as_dict(self)


@dataclass
class SeriesRef(_Record):
    """A series a book belongs to, with the position as a *string*.

    String positions keep ``"3.5"`` and ``"1-3"`` intact; parsing them is the
    matcher's job.
    """

    KIND: ClassVar[str] = "series_ref"

    name: str
    position: str | None = None
    provider_id: str | None = None


@dataclass
class SeriesMember(_Record):
    KIND: ClassVar[str] = "series_member"

    title: str
    position: str | None = None
    kind: str | None = None
    provider_id: str | None = None
    authors: list[str] = field(default_factory=list)
    work_type: str = WORK_TYPE_UNKNOWN
    published: str | None = None
    position_scheme: str | None = None
    alt_positions: dict[str, str] = field(default_factory=dict)
    source_url: str | None = None


@dataclass
class BookRecord(_Record):
    KIND: ClassVar[str] = "book"

    provider_id: str
    title: str
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    isbn10: list[str] = field(default_factory=list)
    isbn13: list[str] = field(default_factory=list)
    publisher: str | None = None
    published: str | None = None
    pages: int | None = None
    language: str | None = None
    cover: bool = False
    description: str | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    series: list[SeriesRef] = field(default_factory=list)
    author_ids: list[str] = field(default_factory=list)
    work_type: str = WORK_TYPE_UNKNOWN
    format: str = FORMAT_UNKNOWN
    record_level: str = RECORD_LEVEL_UNKNOWN
    first_published: str | None = None
    source_url: str | None = None


@dataclass
class SeriesRecord(_Record):
    KIND: ClassVar[str] = "series"

    provider_id: str
    name: str
    authors: list[str] = field(default_factory=list)
    members: list[SeriesMember] = field(default_factory=list)
    ordering_scheme: str | None = None
    source_url: str | None = None


@dataclass
class AuthorRecord(_Record):
    KIND: ClassVar[str] = "author"

    provider_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    works: list[str] = field(default_factory=list)
    series: list[SeriesRef] = field(default_factory=list)
    source_url: str | None = None


@dataclass
class Pagination(_Record):
    """What paging actually did, so "we stopped" is visible, never implied."""

    KIND: ClassVar[str] = "pagination"

    pages_fetched: int = 0
    page_size: int | None = None
    total_reported: int | None = None
    retrieved: int = 0
    truncated: bool = False
    reason: str | None = None


@dataclass
class RawExchange(_Record):
    """One HTTP exchange, already stripped of secrets.

    ``params`` only ever holds non-secret parameters, and ``request_body`` is the
    request body *as sent* with secret values scrubbed. Secret parameters and
    secret headers are merged at send time in :mod:`metadata_lab.http` and are
    never stored here.

    ``request_body`` is required for replay: Hardcover is GraphQL, so every call
    is a POST to one URL and only the body distinguishes one query from another.
    """

    KIND: ClassVar[str] = "exchange"

    method: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    request_body: str | None = None
    status: int | None = None
    elapsed_ms: int = 0
    body: Any = None
    error: dict[str, str] | None = None


class ProviderError(Exception):
    """A classified provider failure whose ``message`` is already redacted."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - trivial, asserted in tests
        return f"ProviderError(kind={self.kind.value!r}, message={self.message!r})"


__all__ = [
    "AuthorRecord",
    "BookRecord",
    "ErrorKind",
    "FORMAT_UNKNOWN",
    "FORMATS",
    "Mode",
    "NOT_RUN_REASONS",
    "Operation",
    "Outcome",
    "Pagination",
    "ProviderError",
    "Query",
    "RECORD_LEVELS",
    "RECORD_LEVEL_UNKNOWN",
    "RawExchange",
    "RunMode",
    "SeriesMember",
    "SeriesRecord",
    "SeriesRef",
    "WORK_TYPES",
    "WORK_TYPE_UNKNOWN",
]
