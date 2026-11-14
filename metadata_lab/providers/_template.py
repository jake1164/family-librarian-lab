"""Copy-me skeleton for a new provider.

The leading underscore keeps this module out of ``metadata_lab.registry.discover``
so it is never registered. Copy it to ``metadata_lab/providers/<id>.py``, then:

1. set ``id`` (lowercase, unique), ``display_name`` and ``credential_env``;
2. put every operation the provider genuinely cannot do into ``unsupported``,
   each with a ``reason`` and the ``source_url`` you actually read;
3. implement only the operations you can, normalising into the record types;
4. add one sanitized sample per operation under ``metadata_lab/samples/<id>/``
   and make the contract test pass;
5. set ``VERIFIED_LIVE = True`` only after one successful real call.

Nothing else in the tool needs editing: suites default to "all registered
providers", so this adapter joins them automatically.
"""

from __future__ import annotations

from typing import ClassVar

from ..http import OpContext
from ..model import (
    AuthorRecord,
    BookRecord,
    Operation,
    Query,
    SeriesRecord,
)
from .base import ProviderAdapter, Unsupported


class ExampleProvider(ProviderAdapter):
    id: ClassVar[str] = "example-provider"
    display_name: ClassVar[str] = "Example Provider"
    adapter_version: ClassVar[str] = "1"
    # None for a keyless provider. Otherwise an env var *name* (never a value).
    credential_env: ClassVar[str | None] = None
    VERIFIED_LIVE: ClassVar[bool] = False

    unsupported: ClassVar[dict[Operation, Unsupported]] = {
        Operation.SERIES_MEMBERS: Unsupported(
            reason="Documented restriction, quoted from the provider's API reference.",
            source_url="https://example.org/api-reference",
        ),
    }

    base_url: ClassVar[str] = "https://api.example.org/v1"

    def check_connection(self, ctx: OpContext) -> str:
        payload = ctx.get_json(f"{self.base_url}/ping")
        return f"reached {payload.get('name', 'service')}"

    def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
        # Provider-neutral input in, normalised records out. Pass credentials only
        # through secret_params / secret_headers so they are never recorded.
        payload = ctx.get_json(
            f"{self.base_url}/search",
            params={"title": q.title or ""},
            secret_headers={"Authorization": f"Bearer {ctx.credential}"} if ctx.credential else None,
        )
        return [
            BookRecord(
                provider_id=str(item.get("id", "")),
                title=str(item.get("title", "")),
                authors=list(item.get("authors") or []),
            )
            for item in payload.get("results", [])
        ]

    # Override the remaining methods only when the provider really supports them:
    #
    # def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]: ...
    # def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]: ...
    # def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]: ...
    # def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]: ...
    # def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]: ...
    # def author_series(self, ctx: OpContext, q: Query) -> list[AuthorRecord]: ...
