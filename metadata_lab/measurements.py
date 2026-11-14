"""Matrix, check tallies and the per-dimension measurements.

Separate measurements, never summed: coverage, accuracy, classification,
ordering, freshness, capability and performance are reported side by side. A
``needs_review`` check is excluded from every denominator and counted on its own,
so an unscoreable fact never looks like a provider failure.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .model import Operation, Outcome

SCORED_EXCLUDED = frozenset(
    {
        Outcome.UNSUPPORTED.value,
        Outcome.NOT_IMPLEMENTED.value,
        Outcome.NOT_CONFIGURED.value,
        Outcome.NOT_RUN.value,
        Outcome.NEEDS_REVIEW.value,
    }
)

DIMENSIONS = ("coverage", "accuracy", "classification", "ordering", "freshness")
_CHECK_INDEX = re.compile(r"\[\d+\]")


def _rows(cases: Sequence[Mapping[str, Any]], provider_id: str, operation: str | None = None):
    for case in cases:
        for row in case.get("results", []):
            if row.get("provider") != provider_id:
                continue
            if operation is not None and row.get("operation") != operation:
                continue
            yield case, row


def op_matrix(cases: Sequence[Mapping[str, Any]], provider_ids: Sequence[str]) -> dict[str, Any]:
    """Outcome counts per operation (the backward-compatible ``matrix`` block)."""

    matrix: dict[str, Any] = {}
    for provider_id in provider_ids:
        per_operation: dict[str, Any] = {}
        for operation in Operation:
            counts: dict[str, int] = {}
            for _case, row in _rows(cases, provider_id, operation.value):
                counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
            if counts:
                counts["total"] = sum(counts.values())
                per_operation[operation.value] = counts
        matrix[provider_id] = per_operation
    return matrix


def check_tallies(
    cases: Sequence[Mapping[str, Any]], provider_ids: Sequence[str]
) -> dict[str, Any]:
    """Per-operation aspect tallies (``series.member[].present 3/4 ok``).

    ``total`` counts scored checks only; ``needs_review`` is listed as its own
    key so the denominator stays honest.
    """

    tallies: dict[str, Any] = {}
    for provider_id in provider_ids:
        per_operation: dict[str, Any] = {}
        for operation in Operation:
            aspects: dict[str, dict[str, int]] = {}
            for _case, row in _rows(cases, provider_id, operation.value):
                for item in row.get("checks", []):
                    aspect = _CHECK_INDEX.sub("[]", str(item["name"]))
                    counts = aspects.setdefault(aspect, {})
                    outcome = str(item["outcome"])
                    counts[outcome] = counts.get(outcome, 0) + 1
            if aspects:
                for counts in aspects.values():
                    counts["total"] = sum(
                        value
                        for name, value in counts.items()
                        if name != "total" and name not in SCORED_EXCLUDED
                    )
                per_operation[operation.value] = aspects
        tallies[provider_id] = per_operation
    return tallies


def _empty_bucket() -> dict[str, Any]:
    return {
        "ok": 0,
        "differs": 0,
        "ambiguous": 0,
        "omitted": 0,
        "reported": 0,
        "expected": 0,
        "needs_review": 0,
        "total": 0,
    }


def _count_into(bucket: dict[str, Any], item: Mapping[str, Any]) -> None:
    """Count one check, keeping omission out of the accuracy denominator."""

    outcome = str(item["outcome"])
    if outcome == Outcome.NEEDS_REVIEW.value:
        bucket["needs_review"] += 1
        return
    if outcome in SCORED_EXCLUDED:
        return
    if outcome == Outcome.NOT_FOUND.value:
        bucket["omitted"] += 1
    elif outcome == Outcome.OK.value:
        bucket["ok"] += 1
    elif outcome == Outcome.DIFFERS.value:
        bucket["differs"] += 1
    elif outcome == Outcome.AMBIGUOUS.value:
        bucket["ambiguous"] += 1
    elif outcome == Outcome.INCOMPLETE.value:
        # A cut-off chain is a coverage gap of the tool, kept visible but out of
        # every accuracy denominator.
        bucket["incomplete"] = bucket.get("incomplete", 0) + 1
    else:
        return
    bucket["expected"] += 1


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    bucket["reported"] = bucket["ok"] + bucket["differs"]
    bucket["total"] = bucket["expected"]
    return bucket


def _dimension_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-dimension counts where **omission is not inaccuracy**.

    ``not_found`` (the provider did not supply the value) is ``omitted``: it
    counts toward that dimension's coverage (``expected``) and is excluded from
    ``reported`` (the denominator of accuracy). ``ambiguous`` is reported
    separately. ``needs_review`` is scored by neither.
    """

    counts: dict[str, Any] = {dimension: _empty_bucket() for dimension in DIMENSIONS}
    for row in rows:
        for item in row.get("checks", []):
            dimension = item.get("dimension")
            if dimension not in counts:
                raise ValueError(f"check {item.get('name')!r} has no valid dimension")
            _count_into(counts[dimension], item)
    return {dimension: _finish(bucket) for dimension, bucket in counts.items()}


def _aspect_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The same semantics per check aspect ("member present", "positions")."""

    aspects: dict[str, Any] = {}
    for row in rows:
        for item in row.get("checks", []):
            aspect = _CHECK_INDEX.sub("[]", str(item.get("name", "")))
            bucket = aspects.setdefault(aspect, _empty_bucket())
            bucket["dimension"] = item.get("dimension")
            _count_into(bucket, item)
    return {aspect: _finish(bucket) for aspect, bucket in aspects.items()}


def _performance(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {"calls": 0, "retries": 0, "throttled": 0, "elapsed_ms": 0, "pages_fetched": 0, "truncated": 0}
    for row in rows:
        stats = row.get("stats") or {}
        for key in ("calls", "retries", "throttled", "elapsed_ms"):
            value = stats.get(key)
            if isinstance(value, int):
                totals[key] += value
        pagination = row.get("pagination") or {}
        if isinstance(pagination.get("pages_fetched"), int):
            totals["pages_fetched"] += pagination["pages_fetched"]
        if row.get("truncated"):
            totals["truncated"] += 1
    return totals


def _profile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    capability: dict[str, Any] = {}
    for row in sorted(rows, key=lambda item: item.get("operation", "")):
        bucket = capability.setdefault(row["operation"], {})
        bucket[row["outcome"]] = bucket.get(row["outcome"], 0) + 1
    return {
        **_dimension_counts(rows),
        "aspects": _aspect_counts(rows),
        "capability": capability,
        "performance": _performance(rows),
    }


def dimension_display(bucket: Mapping[str, Any]) -> str:
    """Data-only sentence for one dimension, never a bare ``0/N`` for omission."""

    ok = int(bucket.get("ok", 0) or 0)
    reported = int(bucket.get("reported", 0) or 0)
    expected = int(bucket.get("expected", 0) or 0)
    omitted = int(bucket.get("omitted", 0) or 0)
    accuracy = f"{ok}/{reported}" if reported else "n/a (nothing reported)"
    parts = [f"{accuracy} agree", f"coverage {reported}/{expected}"]
    if omitted:
        parts.append(f"omitted {omitted}")
    if bucket.get("ambiguous"):
        parts.append(f"ambiguous {bucket['ambiguous']}")
    if bucket.get("differs"):
        parts.append(f"differs {bucket['differs']}")
    if bucket.get("incomplete"):
        parts.append(f"incomplete {bucket['incomplete']}")
    if bucket.get("needs_review"):
        parts.append(f"needs_review {bucket['needs_review']}")
    return "; ".join(parts)


def measurements(
    cases: Sequence[Mapping[str, Any]], provider_ids: Sequence[str]
) -> dict[str, Any]:
    """Per-provider and per-case measurement profiles (Evaluation Plan 4.1)."""

    by_provider: dict[str, Any] = {}
    for provider_id in provider_ids:
        rows = [row for _case, row in _rows(cases, provider_id)]
        by_provider[provider_id] = _profile(rows)

    by_case: dict[str, Any] = {}
    for case in cases:
        per_provider: dict[str, Any] = {}
        for provider_id in provider_ids:
            rows = [
                row
                for row in case.get("results", [])
                if row.get("provider") == provider_id
            ]
            if rows:
                per_provider[provider_id] = _profile(rows)
        by_case[case["id"]] = per_provider
    return {"by_provider": by_provider, "by_case": by_case}


__all__ = ["SCORED_EXCLUDED", "check_tallies", "dimension_display", "measurements", "op_matrix"]
