"""Semantic comparison of two result documents.

Two runs of the same scripted batch must look identical here even though their
run ids, timestamps and timings differ. Raw bodies are excluded because they are
evidence, not a semantic result, and they would make every capture unique.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

#: Fields that describe the *run*, not its results. A replay must compare equal
#: to the run it replays (Evaluation Plan H17), so provenance differences such
#: as mode/replay_of/harness are excluded too.
_VOLATILE_RUN_KEYS = (
    "id",
    "started_at",
    "finished_at",
    "git_revision",
    "mode",
    "replay_of",
    "harness",
)


def semantic_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return the comparable core of a result document."""

    view = copy.deepcopy(dict(result))
    run = view.get("run")
    if isinstance(run, dict):
        for key in _VOLATILE_RUN_KEYS:
            run.pop(key, None)
    for case in view.get("cases", []) or []:
        for row in case.get("results", []) or []:
            row.pop("raw", None)
            row["elapsed_ms"] = 0
            stats = row.get("stats")
            if isinstance(stats, dict):
                stats["elapsed_ms"] = 0
    # The measurements block is derived, so its timing must be neutralised too.
    for profile in (view.get("measurements") or {}).get("by_provider", {}).values():
        performance = profile.get("performance")
        if isinstance(performance, dict):
            performance["elapsed_ms"] = 0
    for per_provider in (view.get("measurements") or {}).get("by_case", {}).values():
        for profile in per_provider.values():
            performance = profile.get("performance")
            if isinstance(performance, dict):
                performance["elapsed_ms"] = 0
    return view


def semantic_digest(result: Mapping[str, Any]) -> str:
    """Stable sha256 over the semantic view (sorted keys, UTF-8)."""

    payload = json.dumps(semantic_view(result), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def semantic_diff(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Report the rows whose semantic content changed between two documents."""

    def index(document: Mapping[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
        rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for case in document.get("cases", []) or []:
            for row in case.get("results", []) or []:
                key = (case.get("id", ""), row.get("provider", ""), row.get("operation", ""))
                rows[key] = {
                    "outcome": row.get("outcome"),
                    "checks": row.get("checks"),
                    "records": row.get("records"),
                    "truncated": row.get("truncated"),
                }
        return rows

    left_rows = index(left)
    right_rows = index(right)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(left_rows) | set(right_rows)):
        before = left_rows.get(key)
        after = right_rows.get(key)
        if before != after:
            changes.append(
                {
                    "case": key[0],
                    "provider": key[1],
                    "operation": key[2],
                    "before_outcome": (before or {}).get("outcome"),
                    "after_outcome": (after or {}).get("outcome"),
                }
            )
    return changes


__all__ = ["semantic_diff", "semantic_digest", "semantic_view"]
