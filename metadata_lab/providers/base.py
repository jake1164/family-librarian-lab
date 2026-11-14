"""The provider adapter contract.

An adapter declares what the *provider* can do. It never imports ``requests``
and never reads credentials itself: it receives an ``OpContext`` and asks it for
JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..model import AuthorRecord, BookRecord, Operation, Query, SeriesRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..http import OpContext


@dataclass(frozen=True)
class Unsupported:
    """Why a provider genuinely cannot do an operation. A citation is mandatory."""

    reason: str
    source_url: str


class ProviderAdapter:
    """Base class. Override only the operations the provider really supports."""

    id: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    adapter_version: ClassVar[str] = "1"
    #: Environment variable holding the credential, or ``None`` for keyless providers.
    credential_env: ClassVar[str | None] = None
    #: Extra environment variable names that may hold the same credential.
    credential_env_aliases: ClassVar[tuple[str, ...]] = ()
    #: True only after a real call against the live provider succeeded.
    VERIFIED_LIVE: ClassVar[bool] = False
    #: Operations the *provider* cannot do at all. Documented, with a source URL.
    unsupported: ClassVar[dict[Operation, Unsupported]] = {}
    #: Minimum seconds between two calls to this provider.
    min_interval_s: ClassVar[float] = 1.0

    def check_connection(self, ctx: "OpContext") -> str:
        """Make one cheap call and describe the result. Raises ``ProviderError``."""

        raise NotImplementedError

    def book_by_title(self, ctx: "OpContext", q: Query) -> list[BookRecord]:
        raise NotImplementedError

    def book_by_isbn(self, ctx: "OpContext", q: Query) -> list[BookRecord]:
        raise NotImplementedError

    def series_for_book(self, ctx: "OpContext", q: Query) -> list[SeriesRecord]:
        raise NotImplementedError

    def series_members(self, ctx: "OpContext", q: Query) -> list[SeriesRecord]:
        raise NotImplementedError

    def author_resolve(self, ctx: "OpContext", q: Query) -> list[AuthorRecord]:
        raise NotImplementedError

    def author_works(self, ctx: "OpContext", q: Query) -> list[AuthorRecord]:
        raise NotImplementedError

    def author_series(self, ctx: "OpContext", q: Query) -> list[AuthorRecord]:
        raise NotImplementedError

    def implements(self, operation: Operation) -> bool:
        """True when this class overrides the operation's method.

        This is the single test for "our adapter has not built it yet"
        (``not_implemented``), which is a gap in this tool, not a verdict on the
        provider.
        """

        return getattr(type(self), operation.value) is not getattr(ProviderAdapter, operation.value)

    def declared_support(self, operation: Operation) -> str:
        if operation in self.unsupported:
            return "unsupported"
        if self.implements(operation):
            return "supported"
        return "not_implemented"


__all__ = ["ProviderAdapter", "Unsupported"]
