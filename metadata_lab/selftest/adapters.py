"""Scripted adapters that talk to :class:`ScriptedServer` through the real client.

They are built for the selftest only: each factory call returns a
``ProviderAdapter`` subclass bound to one loopback base URL, so the engine,
HTTP helper and report code are exercised unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from ..http import OpContext
from ..model import (
    AuthorRecord,
    BookRecord,
    Query,
    SeriesMember,
    SeriesRecord,
    SeriesRef,
)
from ..providers.base import ProviderAdapter, Unsupported


def _book(raw: dict[str, Any], *, provider_id: str) -> BookRecord:
    return BookRecord(
        provider_id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        authors=[str(name) for name in raw.get("authors") or []],
        isbn13=[str(value) for value in raw.get("isbn13") or []],
        publisher=raw.get("publisher"),
        published=raw.get("published"),
        pages=raw.get("pages"),
        language=raw.get("language"),
        cover=bool(raw.get("cover")),
        series=[
            SeriesRef(
                name=str(ref.get("name") or ""),
                position=str(ref["position"]) if ref.get("position") is not None else None,
                provider_id=ref.get("id"),
            )
            for ref in raw.get("series") or []
        ],
        work_type=str(raw.get("work_type") or "unknown"),
        source_url=f"{provider_id}",
    )


def make_adapter(
    base_url: str,
    provider_id: str,
    *,
    unsupported: dict[Any, Unsupported] | None = None,
    min_interval_s: float = 0.0,
    credential_env: str | None = None,
    on_call: Callable[[OpContext, Query], None] | None = None,
    delay: float = 0.0,
    implement: tuple[str, ...] | None = None,
    path_prefix: str = "",
) -> type[ProviderAdapter]:
    """Build a scripted adapter class for one provider id."""

    class ScriptedBase(ProviderAdapter):
        adapter_version: ClassVar[str] = "1"
        VERIFIED_LIVE: ClassVar[bool] = True

        def __init__(self) -> None:
            self.base_url = base_url
            if delay:
                import time as _time

                _time.sleep(delay)

        def check_connection(self, ctx: OpContext) -> str:
            payload = ctx.get_json(f"{self.base_url}/books", params={"title": "ping"})
            return f"scripted books responded ({len(payload.get('books') or [])} item(s))"

        def _call(self, ctx: OpContext, path: str, **params: Any) -> Any:
            if on_call is not None:
                on_call(ctx, Query())
            return ctx.get_json(f"{self.base_url}{path_prefix}{path}", params=params)

        def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
            payload = self._call(ctx, "/books", title=q.title or "")
            return [_book(raw, provider_id=self.id) for raw in payload.get("books") or []]

        def book_by_isbn(self, ctx: OpContext, q: Query) -> list[BookRecord]:
            payload = self._call(ctx, "/books", isbn=q.isbn or "")
            return [_book(raw, provider_id=self.id) for raw in payload.get("books") or []]

        def series_for_book(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
            payload = self._call(ctx, "/series", title=q.title or "")
            return [
                SeriesRecord(
                    provider_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    source_url=self.base_url,
                )
                for raw in payload.get("series") or []
            ]

        def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
            from ..model import ErrorKind, ProviderError

            records: list[SeriesRecord] = []
            complete = False
            for page in range(1, ctx.max_pages + 1):
                payload = self._call(ctx, "/series", name=q.series or "", page=page)
                if not isinstance(payload, dict) or "series" not in payload:
                    # A scripted provider that answers the wrong shape must be a
                    # parse error, exactly like a real adapter would report.
                    raise ProviderError(ErrorKind.PARSE, "series response had no series list")
                for raw in payload.get("series") or []:
                    members = [
                        SeriesMember(
                            title=str(member.get("title") or ""),
                            position=str(member["position"])
                            if member.get("position") is not None
                            else None,
                            provider_id=member.get("id"),
                            work_type=str(member.get("work_type") or "unknown"),
                            authors=[str(name) for name in member.get("authors") or []],
                            published=member.get("published"),
                            source_url=self.base_url,
                        )
                        for member in raw.get("members") or []
                    ]
                    records.append(
                        SeriesRecord(
                            provider_id=str(raw.get("id") or ""),
                            name=str(raw.get("name") or ""),
                            members=members,
                            source_url=self.base_url,
                        )
                    )
                total = payload.get("total")
                ctx.note_pagination(
                    page,
                    size=payload.get("page_size"),
                    total=total if isinstance(total, int) else None,
                    retrieved=sum(len(record.members) for record in records),
                )
                if not payload.get("next"):
                    complete = True
                    break
            if not complete and records:
                ctx.mark_truncated(f"stopped after {ctx.max_pages} page(s)")
            return records

        def author_resolve(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
            payload = self._call(ctx, "/authors", name=q.author or "")
            return [
                AuthorRecord(
                    provider_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    aliases=[str(name) for name in raw.get("aliases") or []],
                    source_url=self.base_url,
                )
                for raw in payload.get("authors") or []
            ]

        def author_works(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
            payload = self._call(ctx, "/authors", name=q.author or "", works=1)
            return [
                AuthorRecord(
                    provider_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    works=[str(title) for title in raw.get("works") or []],
                    source_url=self.base_url,
                )
                for raw in payload.get("authors") or []
            ]

        def author_series(self, ctx: OpContext, q: Query) -> list[AuthorRecord]:
            payload = self._call(ctx, "/authors", name=q.author or "", series=1)
            return [
                AuthorRecord(
                    provider_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    series=[
                        SeriesRef(name=str(ref.get("name") or ""), provider_id=ref.get("id"))
                        for ref in raw.get("series") or []
                    ],
                    source_url=self.base_url,
                )
                for raw in payload.get("authors") or []
            ]

    if NotImplementedAdapter := None:  # pragma: no cover - documentation only
        del NotImplementedAdapter
    return type(
        f"Scripted_{provider_id.replace('-', '_')}",
        (ScriptedBase,),
        {
            "id": provider_id,
            "display_name": f"Scripted {provider_id}",
            "credential_env": credential_env,
            "unsupported": dict(unsupported or {}),
            "min_interval_s": min_interval_s,
        },
    )


def failing_adapter(provider_id: str, error: Exception) -> type[ProviderAdapter]:
    """An adapter whose operations raise, for isolation tests."""

    class Failing(ProviderAdapter):
        id: ClassVar[str] = provider_id
        display_name: ClassVar[str] = f"Failing {provider_id}"

        def book_by_title(self, ctx: OpContext, q: Query) -> list[BookRecord]:
            raise error

        def series_members(self, ctx: OpContext, q: Query) -> list[SeriesRecord]:
            raise error

    return Failing


__all__ = ["failing_adapter", "make_adapter"]
