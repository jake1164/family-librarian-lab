"""The one execution engine.

Every entry point (CLI, one-off query, batch suite, replay, UI) calls
:func:`run_suite` or :func:`run_oneoff`. Each operation is measured on its own,
so a provider that cannot do one thing is still fully evaluated on the rest:
"one weak operation must never hide the rest".
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import VERSION, config
from .cases import Case, Reference, Suite
from .checks import compute_checks, duplicates
from .http import DEFAULT_MAX_PAGES, BudgetExceeded, OpContext, scrub
from .matcher import MATCHER_VERSION, match_name
from .measurements import check_tallies, measurements, op_matrix
from .model import (
    BookRecord,
    ErrorKind,
    Mode,
    Operation,
    Outcome,
    Pagination,
    ProviderError,
    Query,
    RunMode,
    SeriesRecord,
)
from .providers.base import ProviderAdapter
from .semantic import semantic_digest

SCHEMA = "metadata-lab.result/1"
DEFAULT_MAX_CALLS = 10
UNFINISHED_OUTCOMES = frozenset({Outcome.NOT_RUN.value})

HttpFactory = Callable[[str], Any]
AdapterMap = Mapping[str, type[ProviderAdapter]]
ProgressCallback = Callable[[Mapping[str, Any]], None]


class RunCancelled(Exception):
    """Signals a requested stop; ``run_suite`` handles it exactly like Ctrl-C.

    Raised by the ``should_cancel`` callback so a caller without a keyboard (the
    local UI) reuses the one cancel path instead of a second implementation.
    """


_ONE_OFF_REFERENCE = "one-off query: no reference fixture exists, so nothing is compared"


def _empty_pagination() -> dict[str, Any]:
    return Pagination().to_dict()


def _empty_stats() -> dict[str, int]:
    return {"calls": 0, "retries": 0, "throttled": 0, "elapsed_ms": 0}


def _op_outcome(
    checks: Sequence[Mapping[str, Any]],
    records: Sequence[Any],
    truncated: bool,
    error: Mapping[str, Any] | None,
) -> Outcome:
    """Aggregate in the plan's priority order.

    ``partial`` exists so an operation that returned usable data is never
    reported as ``not_found``. ``needs_review`` outranks ``ok`` because an
    unscoreable expectation must stay visible.
    """

    if error:
        return Outcome.ERROR
    outcomes = {str(check["outcome"]) for check in checks}
    if Outcome.INCOMPLETE.value in outcomes:
        return Outcome.INCOMPLETE
    if Outcome.DIFFERS.value in outcomes:
        return Outcome.DIFFERS
    if Outcome.AMBIGUOUS.value in outcomes:
        return Outcome.AMBIGUOUS
    if Outcome.NEEDS_REVIEW.value in outcomes:
        return Outcome.NEEDS_REVIEW

    if not checks:
        # A one-off query has no expectations: any record is a usable answer.
        return Outcome.OK if records else Outcome.NOT_FOUND

    has_ok = Outcome.OK.value in outcomes
    has_missing = Outcome.NOT_FOUND.value in outcomes
    if records and has_ok and has_missing:
        return Outcome.PARTIAL
    if not records or not has_ok:
        return Outcome.NOT_FOUND
    return Outcome.OK


def _context_terms(case: Case, operation: Operation) -> tuple[str, ...]:
    """Case-declared names a title may legitimately carry as series detail."""

    terms: list[str] = []
    if case.input.get("series"):
        terms.append(case.input["series"])
    for key in (Operation.SERIES_FOR_BOOK.value, Operation.SERIES_MEMBERS.value):
        expect = case.expect.get(key) or {}
        if expect.get("series"):
            terms.append(str(expect["series"]))
        terms.extend(str(alias) for alias in expect.get("series_aliases") or ())
    expect = case.expect.get(Operation.AUTHOR_SERIES.value) or {}
    terms.extend(str(name) for name in expect.get("series") or [])
    return tuple(dict.fromkeys(term for term in terms if term.strip()))


def _query_for(case: Case) -> Query:
    data = case.input
    return Query(
        title=data.get("title"),
        author=data.get("author"),
        isbn=data.get("isbn"),
        series=data.get("series"),
        provider_id=data.get("provider_id"),
    )


def _run_operation(
    *,
    case: Case,
    operation: Operation,
    provider_id: str,
    adapter: ProviderAdapter,
    http: Any,
    mode: Mode,
    configured: bool,
    as_of: str | None,
    query: Query | None = None,
) -> tuple[dict[str, Any], list[Any]]:
    """Run one operation for one provider and return ``(row, records)``."""

    started = time.monotonic()
    notes: list[str] = []
    error: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = []
    records: list[Any] = []
    raw: list[dict[str, Any]] = []
    truncated = False
    pagination = _empty_pagination()
    stats = _empty_stats()
    found_duplicates: list[dict[str, Any]] = []
    secrets: set[str] = set()

    def row(
        outcome: Outcome, elapsed_ms: int | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        payload = {
            "provider": provider_id,
            "operation": operation.value,
            "mode": mode.value,
            "outcome": outcome.value,
            "reference_verified": case.verified,
            "elapsed_ms": elapsed_ms
            if elapsed_ms is not None
            else int((time.monotonic() - started) * 1000),
            "truncated": truncated,
            "notes": scrub(notes, secrets),
            "error": error,
            "checks": scrub(checks, secrets),
            # Provider text can echo anything, including a credential; records are
            # scrubbed like raw bodies so no output can carry a secret value.
            "records": [scrub(record.to_dict(), secrets) for record in records],
            "raw": raw,
            "pagination": pagination,
            "duplicates": found_duplicates,
            "stats": stats,
        }
        if reason:
            payload["reason"] = reason
        return payload

    # Decision order 1-3: never call what we should not call.
    if not configured:
        notes.append(f"credential {adapter.credential_env} not configured; not attempted")
        return row(Outcome.NOT_CONFIGURED), []
    declared = adapter.unsupported.get(operation)
    if declared is not None:
        notes.append(f"unsupported: {declared.reason} ({declared.source_url})")
        return row(Outcome.UNSUPPORTED), []
    if not adapter.implements(operation):
        notes.append("adapter does not implement this operation yet (a gap in this tool)")
        return row(Outcome.NOT_IMPLEMENTED), []

    credential = config.resolve_credential(adapter)
    max_pages = int(case.options.get("max_pages", DEFAULT_MAX_PAGES))
    max_calls = int(case.options.get("max_calls", DEFAULT_MAX_CALLS))
    ctx: OpContext = http.context(
        operation=operation,
        credential=credential,
        max_calls=max_calls,
        max_pages=max_pages,
    )
    method = getattr(adapter, operation.value)

    try:
        records = list(method(ctx, query or _query_for(case)) or [])
    except BudgetExceeded as exc:
        truncated = True
        notes.append(f"call budget reached: {exc}")
    except ProviderError as exc:
        error = {"kind": exc.kind.value, "message": exc.message}
    except Exception as exc:  # adapter bug: the message may carry data, keep only the type
        error = {"kind": ErrorKind.PARSE.value, "message": f"adapter raised {type(exc).__name__}"}
    finally:
        notes.extend(ctx.notes)
        raw = [exchange.to_dict() for exchange in ctx.exchanges]
        truncated = truncated or ctx.truncated
        pagination = ctx.pagination.to_dict()
        pagination["truncated"] = truncated
        stats = {**ctx.stats(), "elapsed_ms": int((time.monotonic() - started) * 1000)}
        secrets = ctx.secret_values()

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if error is None:
        if records:
            found_duplicates = duplicates(records)
            if found_duplicates:
                notes.append(
                    "duplicate records retained: "
                    + ", ".join(f"{item['key']} x{item['count']}" for item in found_duplicates)
                )
        checks = compute_checks(
            operation,
            case.expect.get(operation.value) or {},
            records,
            truncated=truncated,
            context_terms=_context_terms(case, operation),
            as_of=as_of,
            unresolved=ctx.unresolved_counts(),
        )

    outcome = _op_outcome(checks, records, truncated, error)
    return row(outcome, elapsed_ms), records


# ---------------------------------------------------------------------------
# traversal (a separate measurement, never a replacement)
# ---------------------------------------------------------------------------


def _traversal_query(base: Query, **overrides: Any) -> Query:
    return replace(base, **overrides)


def _not_run_row(
    case: Case, provider_id: str, operation: Operation, broken_step: str, reason: str
) -> dict[str, Any]:
    return {
        "provider": provider_id,
        "operation": operation.value,
        "mode": Mode.TRAVERSAL.value,
        "outcome": Outcome.NOT_RUN.value,
        "reason": reason,
        "reference_verified": case.verified,
        "elapsed_ms": 0,
        "truncated": False,
        "notes": [f"not run: the chain broke at {broken_step}"],
        "error": None,
        "checks": [],
        "records": [],
        "raw": [],
        "pagination": _empty_pagination(),
        "duplicates": [],
        "stats": _empty_stats(),
    }


def _run_traversal(
    *,
    case: Case,
    provider_id: str,
    adapter: ProviderAdapter,
    http: Any,
    configured: bool,
    as_of: str | None,
) -> list[dict[str, Any]]:
    """Follow the provider's own ids from a book to the rest of its author."""

    rows: list[dict[str, Any]] = []
    query = _query_for(case)

    book_row, books = _run_operation(
        case=case,
        operation=Operation.BOOK_BY_TITLE,
        provider_id=provider_id,
        adapter=adapter,
        http=http,
        mode=Mode.TRAVERSAL,
        configured=configured,
        as_of=as_of,
        query=query,
    )
    rows.append(book_row)

    book = next(
        (
            record
            for record in books
            if isinstance(record, BookRecord)
            and match_name(
                case.input.get("title", ""),
                record.title,
                context_terms=_context_terms(case, Operation.BOOK_BY_TITLE),
            ).is_match
        ),
        None,
    )
    if book is None:
        rows.extend(
            _not_run_row(case, provider_id, Operation(name), "book_by_title", "chain_broken")
            for name in ("series_for_book", "series_members", "author_works", "author_series")
        )
        return rows

    series_query = _traversal_query(
        query,
        provider_id=next((ref.provider_id for ref in book.series if ref.provider_id), None),
        series=book.series[0].name if book.series else query.series,
        title=book.title,
    )
    series_row, series_records = _run_operation(
        case=case,
        operation=Operation.SERIES_FOR_BOOK,
        provider_id=provider_id,
        adapter=adapter,
        http=http,
        mode=Mode.TRAVERSAL,
        configured=configured,
        as_of=as_of,
        query=series_query,
    )
    rows.append(series_row)

    series_provider_id = next(
        (record.provider_id for record in series_records if getattr(record, "provider_id", None)),
        None,
    )
    if series_provider_id is None:
        broken = (
            "series_for_book" if not series_records else "series_for_book (no series id returned)"
        )
        rows.append(
            _not_run_row(case, provider_id, Operation.SERIES_MEMBERS, broken, "chain_broken")
        )
    else:
        members_row, _members = _run_operation(
            case=case,
            operation=Operation.SERIES_MEMBERS,
            provider_id=provider_id,
            adapter=adapter,
            http=http,
            mode=Mode.TRAVERSAL,
            configured=configured,
            as_of=as_of,
            query=_traversal_query(series_query, provider_id=series_provider_id),
        )
        rows.append(members_row)

    author_id = book.author_ids[0] if book.author_ids else None
    for operation in (Operation.AUTHOR_WORKS, Operation.AUTHOR_SERIES):
        if author_id is None:
            rows.append(
                _not_run_row(
                    case,
                    provider_id,
                    operation,
                    "book_by_title (no author id returned)",
                    "chain_broken",
                )
            )
            continue
        author_row, _works = _run_operation(
            case=case,
            operation=operation,
            provider_id=provider_id,
            adapter=adapter,
            http=http,
            mode=Mode.TRAVERSAL,
            configured=configured,
            as_of=as_of,
            query=_traversal_query(
                query,
                provider_id=author_id,
                author=book.authors[0] if book.authors else query.author,
            ),
        )
        rows.append(author_row)
    return rows


# ---------------------------------------------------------------------------
# document assembly
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_revision() -> str | None:
    """Best-effort short revision; ``None`` when git is unavailable."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(config.repo_root()),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _provider_entry(
    provider_id: str,
    adapter_cls: type[ProviderAdapter],
    configured: bool,
    excluded: bool,
) -> dict[str, Any]:
    adapter = adapter_cls()
    return {
        "id": provider_id,
        "display_name": adapter.display_name,
        "adapter_version": adapter.adapter_version,
        "verified_live": bool(adapter.VERIFIED_LIVE),
        "configured": configured,
        "excluded": excluded,
        "declared": {op.value: adapter.declared_support(op) for op in Operation},
        "unsupported_reasons": {
            op.value: {"reason": item.reason, "source_url": item.source_url}
            for op, item in adapter.unsupported.items()
        },
    }


def _select_providers(
    adapters: AdapterMap, requested: Sequence[str] | None, suite_providers: Sequence[str] | None
) -> list[str]:
    if requested:
        selected = list(dict.fromkeys(requested))
    elif suite_providers:
        selected = list(dict.fromkeys(suite_providers))
    else:
        selected = list(adapters)
    unknown = [provider for provider in selected if provider not in adapters]
    if unknown:
        raise ValueError(
            f"unknown provider(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(adapters)) or 'none'})"
        )
    return selected


def _resume_index(
    resume: Mapping[str, Any] | None,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if not isinstance(resume, Mapping):
        return index
    for case in resume.get("cases", []) or []:
        for row in case.get("results", []) or []:
            if str(row.get("outcome")) in UNFINISHED_OUTCOMES:
                continue
            key = (
                str(case.get("id", "")),
                str(row.get("provider", "")),
                str(row.get("operation", "")),
                str(row.get("mode", Mode.INDEPENDENT.value)),
            )
            index[key] = row
    return index


def _skipped_row(
    case: Case,
    provider_id: str,
    operation: Operation,
    mode: Mode,
    reason: str,
    note: str,
) -> dict[str, Any]:
    return {
        "provider": provider_id,
        "operation": operation.value,
        "mode": mode.value,
        "outcome": Outcome.NOT_RUN.value,
        "reason": reason,
        "reference_verified": case.verified,
        "elapsed_ms": 0,
        "truncated": False,
        "notes": [note],
        "error": None,
        "checks": [],
        "records": [],
        "raw": [],
        "pagination": _empty_pagination(),
        "duplicates": [],
        "stats": _empty_stats(),
    }


def run_suite(
    suite: Suite,
    providers: Sequence[str] | None = None,
    case_ids: Sequence[str] | None = None,
    *,
    http_factory: HttpFactory,
    adapters: AdapterMap,
    traversal: bool | None = None,
    run_mode: RunMode = RunMode.LIVE,
    replay_of: str | None = None,
    resolve_configured: Callable[[type[ProviderAdapter]], bool] | None = None,
    exclude: Iterable[str] = (),
    as_of: str | None = None,
    harness: Mapping[str, Any] | None = None,
    resume: Mapping[str, Any] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run a suite and return the result document. The only way to run a suite.

    ``should_cancel`` and ``progress`` exist for callers with no terminal: the
    first is checked before each planned row and stops the run through the same
    path as ``KeyboardInterrupt``, the second receives a small dict per row so a
    UI can show where the run is without parsing the result document.
    """

    started_at = _utc_now()
    selected = _select_providers(adapters, providers, suite.providers)
    is_configured = resolve_configured or config.is_configured
    excluded = [provider for provider in dict.fromkeys(exclude) if provider in selected]
    active = [provider for provider in selected if provider not in excluded]

    cases = list(suite.cases)
    if case_ids:
        wanted = list(dict.fromkeys(case_ids))
        missing = [case_id for case_id in wanted if case_id not in {c.id for c in cases}]
        if missing:
            raise ValueError(f"unknown case id(s): {', '.join(missing)}")
        cases = [case for case in cases if case.id in wanted]

    resolved_as_of = as_of or suite.as_of or date.today().isoformat()
    harness_block = dict(harness or {"status": "not_run", "selftest_id": None, "digest": None})

    provider_entries = [
        _provider_entry(
            provider_id,
            adapters[provider_id],
            is_configured(adapters[provider_id]),
            provider_id in excluded,
        )
        for provider_id in selected
    ]
    configured = {entry["id"]: entry["configured"] for entry in provider_entries}
    resume_index = _resume_index(resume)

    case_documents: list[dict[str, Any]] = [
        {
            "id": case.id,
            "description": case.description,
            "reference": case.reference.to_dict(),
            "verified": case.verified,
            "tier": case.tier,
            "tags": list(case.tags),
            "notes": case.notes,
            "input": dict(case.input),
            "operations": [operation.value for operation in case.operations],
            "traversal": case.traversal,
            "options": dict(case.options),
            "expect": case.expect,
            "results": [],
        }
        for case in cases
    ]
    by_case = {document["id"]: document for document in case_documents}
    status = "complete"

    # Build the plan up front so a cancellation can mark every pending row.
    plan: list[tuple[Case, str, Operation, Mode]] = []
    for case in cases:
        wants_traversal = case.traversal if traversal is None else traversal
        for provider_id in selected:
            for operation in case.operations:
                plan.append((case, provider_id, operation, Mode.INDEPENDENT))
            if wants_traversal:
                for operation in (
                    Operation.BOOK_BY_TITLE,
                    Operation.SERIES_FOR_BOOK,
                    Operation.SERIES_MEMBERS,
                    Operation.AUTHOR_WORKS,
                    Operation.AUTHOR_SERIES,
                ):
                    plan.append((case, provider_id, operation, Mode.TRAVERSAL))

    done: set[tuple[str, str, str, str]] = set()
    http_by_provider: dict[str, Any] = {}
    total_rows = len({(case.id, pid, op.value, mode.value) for case, pid, op, mode in plan})
    calls_used = 0

    def append_row(case_id: str, row: Mapping[str, Any]) -> None:
        nonlocal calls_used
        by_case[case_id]["results"].append(row)
        calls_used += int((row.get("stats") or {}).get("calls") or 0)

    def report(
        case: Case | None, provider_id: str | None, operation: Operation | None, mode: Mode | None
    ) -> None:
        if progress is None:
            return
        progress(
            {
                "case": case.id if case is not None else None,
                "provider": provider_id,
                "operation": operation.value if operation is not None else None,
                "mode": mode.value if mode is not None else None,
                "rows_done": len(done),
                "rows_total": total_rows,
                "calls": calls_used,
            }
        )

    report(None, None, None, None)
    try:
        for case, provider_id, operation, mode in plan:
            key = (case.id, provider_id, operation.value, mode.value)
            if key in done:
                continue
            if should_cancel is not None and should_cancel():
                raise RunCancelled
            report(case, provider_id, operation, mode)
            if provider_id in excluded:
                append_row(
                    case.id,
                    _skipped_row(
                        case,
                        provider_id,
                        operation,
                        mode,
                        "excluded",
                        "not run: provider excluded for this run",
                    ),
                )
                done.add(key)
                continue
            if key in resume_index:
                reused = dict(resume_index[key])
                reused["resumed"] = True
                append_row(case.id, reused)
                done.add(key)
                continue

            adapter = adapters[provider_id]()
            http = http_by_provider.setdefault(provider_id, http_factory(provider_id))
            if mode is Mode.TRAVERSAL:
                chain = _run_traversal(
                    case=case,
                    provider_id=provider_id,
                    adapter=adapter,
                    http=http,
                    configured=configured[provider_id],
                    as_of=resolved_as_of,
                )
                for row in chain:
                    chain_key = (case.id, provider_id, row["operation"], row["mode"])
                    if chain_key not in done and chain_key not in resume_index:
                        append_row(case.id, row)
                    done.add(chain_key)
                continue

            row, _records = _run_operation(
                case=case,
                operation=operation,
                provider_id=provider_id,
                adapter=adapter,
                http=http,
                mode=mode,
                configured=configured[provider_id],
                as_of=resolved_as_of,
            )
            append_row(case.id, row)
            done.add(key)
    except (KeyboardInterrupt, RunCancelled):
        status = "cancelled"
        for case, provider_id, operation, mode in plan:
            key = (case.id, provider_id, operation.value, mode.value)
            if key in done:
                continue
            by_case[case.id]["results"].append(
                _skipped_row(
                    case,
                    provider_id,
                    operation,
                    mode,
                    "cancelled",
                    "not run: the run was cancelled before this row started",
                )
            )
            done.add(key)
    report(None, None, None, None)

    document = {
        "schema": SCHEMA,
        "run": {
            "id": f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
            "started_at": started_at,
            "finished_at": _utc_now(),
            "mode": run_mode.value,
            "replay_of": replay_of,
            "status": status,
            "as_of": resolved_as_of,
            "exclude": excluded,
            "resumed_from": (resume.get("run") or {}).get("id")
            if isinstance(resume, Mapping)
            else None,
            "harness": harness_block,
            "tool_version": VERSION,
            "matcher_version": MATCHER_VERSION,
            "git_revision": git_revision(),
            "python": sys.version.split()[0],
        },
        "suite": {
            "name": suite.name,
            "version": suite.version,
            "source": suite.source,
            "sha256": suite.sha256,
            "as_of": suite.as_of,
        },
        "providers": provider_entries,
        "cases": case_documents,
        "matrix": op_matrix(case_documents, selected),
        "matrix_checks": check_tallies(case_documents, selected),
        "measurements": measurements(case_documents, selected),
    }
    document["run"]["semantic_digest"] = semantic_digest(document)
    return document


_ONE_OFF_OPERATIONS: dict[str, tuple[Operation, tuple[str, ...]]] = {
    "book": (Operation.BOOK_BY_TITLE, ("title",)),
    "isbn": (Operation.BOOK_BY_ISBN, ("isbn",)),
    "author": (Operation.AUTHOR_RESOLVE, ("author",)),
    "series": (Operation.SERIES_MEMBERS, ("series",)),
}


def build_oneoff_suite(kind: str, fields: Mapping[str, Any]) -> Suite:
    """Build an in-memory one-case suite so one-off queries use the same engine."""

    if kind not in _ONE_OFF_OPERATIONS:
        raise ValueError(
            f"unknown query type {kind!r}; choose from {', '.join(sorted(_ONE_OFF_OPERATIONS))}"
        )
    operation, required = _ONE_OFF_OPERATIONS[kind]
    missing = [name for name in required if not str(fields.get(name) or "").strip()]
    if missing:
        raise ValueError(f"query type {kind!r} needs {', '.join(missing)}")

    case_input = {
        name: str(value).strip()
        for name, value in fields.items()
        if name in {"title", "author", "isbn", "series"} and str(value or "").strip()
    }
    case = Case(
        id=f"one-off-{kind}",
        description=f"one-off {kind} query",
        reference=Reference(source=_ONE_OFF_REFERENCE, verified_on=None),
        input=case_input,
        operations=(operation,),
        traversal=False,
        options={},
        expect={},
        verified=False,
    )
    digest = hashlib.sha256(json.dumps(case.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()
    return Suite(name=f"one-off-{kind}", version=0, cases=(case,), source="one-off", sha256=digest)


def run_oneoff(
    kind: str,
    query_fields: Mapping[str, Any],
    providers: Sequence[str] | None = None,
    *,
    http_factory: HttpFactory,
    adapters: AdapterMap,
    max_pages: int | None = None,
    as_of: str | None = None,
    harness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one ad-hoc query through the same engine as a suite."""

    suite = build_oneoff_suite(kind, query_fields)
    if max_pages:
        case = replace(suite.cases[0], options={"max_pages": int(max_pages)})
        suite = replace(suite, cases=(case,))
    return run_suite(
        suite,
        providers,
        None,
        http_factory=http_factory,
        adapters=adapters,
        as_of=as_of,
        harness=harness,
    )


__all__ = [
    "SCHEMA",
    "ProgressCallback",
    "RunCancelled",
    "build_oneoff_suite",
    "git_revision",
    "run_oneoff",
    "run_suite",
]
