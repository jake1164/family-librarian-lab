"""Diff two result documents.

Compares what a reader cares about — which providers took part, what changed in
the outcomes, and whether a timing changed enough to be worth a look. Semantic
equality uses the same canonical view as the replay tests, so a re-run of the
same code with new timings reports "no semantic change".
"""

from __future__ import annotations

from typing import Any, Mapping

from .semantic import semantic_diff, semantic_digest

ELAPSED_CHANGE_THRESHOLD = 0.5


def _rows(document: Mapping[str, Any]) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    rows: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for case in document.get("cases", []) or []:
        for row in case.get("results", []) or []:
            rows[(str(case.get("id")), str(row.get("provider")), str(row.get("operation")))] = row
    return rows


def _case_definitions(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(case.get("id")): {
            "reference": case.get("reference"),
            "verified": case.get("verified"),
            "expect": case.get("expect"),
            "operations": case.get("operations"),
        }
        for case in document.get("cases", []) or []
    }


def compare(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    """Return a change list keyed by (case, provider, operation)."""

    first_rows = _rows(first)
    second_rows = _rows(second)
    changes: list[dict[str, Any]] = []

    first_providers = {str(entry.get("id")) for entry in first.get("providers", []) or []}
    second_providers = {str(entry.get("id")) for entry in second.get("providers", []) or []}
    for provider in sorted(second_providers - first_providers):
        changes.append({"kind": "new_provider", "provider": provider})
    for provider in sorted(first_providers - second_providers):
        changes.append({"kind": "removed_provider", "provider": provider})

    for case_id, before in _case_definitions(first).items():
        after = _case_definitions(second).get(case_id)
        if after is not None and after != before:
            changes.append({"kind": "reference_changed", "case": case_id})

    for key in sorted(set(first_rows) | set(second_rows)):
        case_id, provider, operation = key
        before = first_rows.get(key)
        after = second_rows.get(key)
        base = {"case": case_id, "provider": provider, "operation": operation}
        if before is None:
            changes.append(
                {"kind": "new_row", **base, "after_outcome": (after or {}).get("outcome")}
            )
            continue
        if after is None:
            changes.append(
                {"kind": "removed_row", **base, "before_outcome": before.get("outcome")}
            )
            continue
        if before.get("outcome") != after.get("outcome"):
            changes.append(
                {
                    "kind": "outcome_changed",
                    **base,
                    "before_outcome": before.get("outcome"),
                    "after_outcome": after.get("outcome"),
                }
            )
        before_ms = before.get("elapsed_ms") or 0
        after_ms = after.get("elapsed_ms") or 0
        if before_ms > 0 and abs(after_ms - before_ms) / before_ms > ELAPSED_CHANGE_THRESHOLD:
            changes.append(
                {
                    "kind": "elapsed_changed",
                    **base,
                    "before_ms": before_ms,
                    "after_ms": after_ms,
                }
            )

    return {
        "semantic_equal": semantic_digest(first) == semantic_digest(second),
        "semantic_changes": semantic_diff(first, second),
        "changes": changes,
    }


__all__ = ["ELAPSED_CHANGE_THRESHOLD", "compare"]
