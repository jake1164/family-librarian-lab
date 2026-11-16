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


def operation_fixture_counts(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many fixtures exercise each operation, including operations at zero.

    A fixture exercises an operation when the case declares it or when any row
    from the run performed it, so a traversal step (a book lookup that derives
    ``author_works``) counts as well. Nothing here is provider-specific.
    """

    counts = {operation.value: 0 for operation in Operation}
    for case in cases:
        exercised = {str(name) for name in case.get("operations") or []}
        exercised.update(
            str(row["operation"]) for row in case.get("results") or [] if row.get("operation")
        )
        for name in exercised:
            if name in counts:
                counts[name] += 1
    return counts


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


# --- fixed-denominator fact tables -----------------------------------------------
#
# The tallies above count only the checks that executed, so a provider that could
# not run an operation (or whose series name was not found, which skips the member
# checks) silently gets a smaller denominator. The fact tables below fix that: the
# reference facts come from the fixtures alone, so N is the same for every
# provider, and each fact lands in exactly one status.

METRIC_VERSION = 2
MODES = ("independent", "traversal")
FACT_STATUSES = ("correct", "differs", "ambiguous", "needs_review", "omitted", "incomplete")
_STATUS_OF_OUTCOME = {
    Outcome.OK.value: "correct",
    Outcome.DIFFERS.value: "differs",
    Outcome.AMBIGUOUS.value: "ambiguous",
    Outcome.NEEDS_REVIEW.value: "needs_review",
    Outcome.NOT_FOUND.value: "omitted",
    Outcome.INCOMPLETE.value: "incomplete",
}
#: Who owns a blocked fact. ``unsupported`` is the provider's gap, ``not_implemented``
#: is ours; everything else is a run condition. They are never merged.
BLOCKED_CLASSES = {
    "unsupported": "provider",
    "not_implemented": "tool",
}
_PARENT_CHECKS = ("series.name", "author.name")
#: Minimum number of returned values before a percentage is printed.
MIN_RETURNED_FOR_PERCENT = 5


def _empty_fact_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {status: 0 for status in FACT_STATUSES}
    bucket["blocked"] = 0
    bucket["blocked_by"] = {}
    bucket["facts"] = 0
    #: ambiguous facts where no two records conflicted and no single field value was observed.
    bucket["ambiguous_no_conflict"] = 0
    #: facts scored from the one non-conflicting observation although several same-named
    #: series were returned. The field is scored (correct/differs); *which series* it came
    #: from stays ambiguous, so identity confidence is reported apart from field presence.
    bucket["identity_ambiguous"] = 0
    return bucket


def _add_fact(bucket: dict[str, Any], status: str, reason: str | None = None) -> None:
    bucket["facts"] += 1
    if status == "blocked":
        bucket["blocked"] += 1
        key = reason or "unknown"
        bucket["blocked_by"][key] = bucket["blocked_by"].get(key, 0) + 1
    else:
        bucket[status] += 1
        if status == "ambiguous" and reason == "no_conflict":
            bucket["ambiguous_no_conflict"] += 1
        if reason == "identity_ambiguous":
            bucket["identity_ambiguous"] += 1


def _finish_fact_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    bucket["blocked_by"] = dict(sorted(bucket["blocked_by"].items()))
    bucket["reachable"] = bucket["facts"] - bucket["blocked"]
    # "returned" = the provider gave a value that was compared and either agreed
    # or disagreed. Accuracy is measured over these only; omission never enters it.
    bucket["returned"] = bucket["correct"] + bucket["differs"]
    return bucket


def _blocked_reason(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "no_row"
    outcome = str(row.get("outcome"))
    if outcome == Outcome.NOT_RUN.value:
        return str(row.get("reason") or "not_run")
    if outcome == Outcome.ERROR.value:
        kind = (row.get("error") or {}).get("kind")
        return f"error:{kind}" if kind else "error"
    return outcome


def _row_is_blocked(row: Mapping[str, Any] | None) -> bool:
    if row is None:
        return True
    return str(row.get("outcome")) in {
        Outcome.UNSUPPORTED.value,
        Outcome.NOT_IMPLEMENTED.value,
        Outcome.NOT_CONFIGURED.value,
        Outcome.NOT_RUN.value,
        Outcome.ERROR.value,
    }


def _fact_statuses(
    facts: Sequence[Mapping[str, str]], row: Mapping[str, Any] | None
) -> tuple[list[tuple[Mapping[str, str], str, str | None]], int]:
    """Status of every fact for one provider row, plus the count of unlisted checks."""

    if _row_is_blocked(row):
        reason = _blocked_reason(row)
        return [(fact, "blocked", reason) for fact in facts], 0

    assert row is not None
    checks = {str(item["name"]): item for item in row.get("checks") or []}
    parent_status: str | None = None
    for parent in _PARENT_CHECKS:
        if parent in checks:
            parent_status = _STATUS_OF_OUTCOME.get(str(checks[parent]["outcome"]))
    listed = {fact["name"] for fact in facts}
    unlisted = sum(1 for name in checks if name not in listed)

    result: list[tuple[Mapping[str, str], str, str | None]] = []
    for fact in facts:
        item = checks.get(fact["name"])
        if item is not None:
            status = _STATUS_OF_OUTCOME.get(str(item["outcome"]))
            if status is None:
                raise ValueError(f"check {fact['name']!r} has outcome {item['outcome']!r} with no fact status")
            merge = item.get("merge") or {}
            note = None
            if status == "ambiguous" and merge and not merge.get("conflicting"):
                resolved = _STATUS_OF_OUTCOME.get(str(merge.get("resolved")))
                if resolved in ("correct", "differs"):
                    status, note = resolved, "identity_ambiguous"
                else:
                    note = "no_conflict"
            result.append((fact, status, note))
            continue
        # The operation ran but this check was never emitted: the series or author
        # it hangs off was not matched, so the provider supplied nothing for it.
        result.append((fact, parent_status if parent_status in ("omitted", "incomplete") else "omitted", None))
    return result, unlisted


def fact_tables(
    cases: Sequence[Mapping[str, Any]],
    provider_ids: Sequence[str],
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Fixed-denominator tables per mode, provider and dimension.

    ``N`` (``reference_facts``) is derived from the fixtures; every provider gets
    exactly N facts, each in one status, so ``correct + differs + ambiguous +
    needs_review + omitted + incomplete + blocked == N`` always holds.
    """

    from .checks import expected_facts  # local import: checks needs no measurements

    modes: dict[str, Any] = {}
    for mode in MODES:
        # A (case, operation) pair belongs to this mode when any provider has a row for it.
        pairs: list[tuple[Mapping[str, Any], Operation, list[Mapping[str, str]]]] = []
        for case in cases:
            expect = case.get("expect") or {}
            for operation in Operation:
                facts = expected_facts(operation, expect.get(operation.value) or {}, as_of=as_of)
                if not facts:
                    continue
                if any(
                    row.get("operation") == operation.value and row.get("mode") == mode
                    for row in case.get("results", [])
                ):
                    pairs.append((case, operation, facts))
        if not pairs:
            continue

        reference: dict[str, Any] = {"total": 0, "by_dimension": {d: 0 for d in DIMENSIONS}}
        for _case, _operation, facts in pairs:
            reference["total"] += len(facts)
            for fact in facts:
                reference["by_dimension"][fact["dimension"]] += 1

        providers: dict[str, Any] = {}
        for provider_id in provider_ids:
            total = _empty_fact_bucket()
            by_dimension = {dimension: _empty_fact_bucket() for dimension in DIMENSIONS}
            by_case: dict[str, Any] = {}
            unlisted = 0
            evidence: dict[str, int] = {}
            for case, operation, facts in pairs:
                row = next(
                    (
                        item
                        for item in case.get("results", [])
                        if item.get("provider") == provider_id
                        and item.get("operation") == operation.value
                        and item.get("mode") == mode
                    ),
                    None,
                )
                statuses, extra = _fact_statuses(facts, row)
                unlisted += extra
                source = (row or {}).get("data_source")
                if source:
                    evidence[source] = evidence.get(source, 0) + 1
                case_bucket = by_case.setdefault(str(case["id"]), _empty_fact_bucket())
                for fact, status, reason in statuses:
                    _add_fact(total, status, reason)
                    _add_fact(by_dimension[fact["dimension"]], status, reason)
                    _add_fact(case_bucket, status, reason)
            providers[provider_id] = {
                "total": _finish_fact_bucket(total),
                "by_dimension": {d: _finish_fact_bucket(b) for d, b in by_dimension.items()},
                "by_case": {cid: _finish_fact_bucket(b) for cid, b in by_case.items()},
                "unlisted_checks": unlisted,
                #: rows by how their evidence was obtained: live, cache, mixed, replay.
                "evidence": dict(sorted(evidence.items())),
            }
        modes[mode] = {"reference_facts": reference, "providers": providers}
    return {"metric_version": METRIC_VERSION, "modes": modes}


def fact_invariant_errors(tables: Mapping[str, Any]) -> list[str]:
    """Return every way the fixed-denominator rule is broken (empty means sound)."""

    errors: list[str] = []
    for mode, block in (tables.get("modes") or {}).items():
        reference = block["reference_facts"]
        for provider_id, profile in block["providers"].items():
            for label, bucket, expected in (
                ("total", profile["total"], reference["total"]),
                *(
                    (f"dimension {d}", profile["by_dimension"][d], reference["by_dimension"][d])
                    for d in DIMENSIONS
                ),
            ):
                counted = sum(bucket[status] for status in FACT_STATUSES) + bucket["blocked"]
                if counted != expected or bucket["facts"] != expected:
                    errors.append(
                        f"{mode}/{provider_id}/{label}: statuses sum to {counted}, "
                        f"facts {bucket['facts']}, reference {expected}"
                    )
            if profile.get("unlisted_checks"):
                errors.append(
                    f"{mode}/{provider_id}: {profile['unlisted_checks']} executed check(s) are not "
                    "in the fact list"
                )
    return errors


def blocked_class(reason: str) -> str:
    """``provider`` (unsupported), ``tool`` (not_implemented) or ``run`` (anything else)."""

    return BLOCKED_CLASSES.get(reason, "run")


def fact_display(bucket: Mapping[str, Any]) -> str:
    """Data-only sentence for one fixed-denominator bucket."""

    total = int(bucket.get("facts", 0))
    returned = int(bucket.get("returned", 0))
    correct = int(bucket.get("correct", 0))
    reachable = int(bucket.get("reachable", 0))
    parts = [f"{returned}/{total} returned"]
    if returned >= MIN_RETURNED_FOR_PERCENT:
        parts.append(f"{correct}/{returned} of returned agree")
    else:
        parts.append(f"{correct} of {returned} returned agree (n<{MIN_RETURNED_FOR_PERCENT})")
    parts.append(f"reachable {reachable}/{total}")
    if bucket.get("identity_ambiguous"):
        parts.append(
            f"{bucket['identity_ambiguous']} of the returned scored from one of several same-named "
            "series (series identity ambiguous)"
        )
    for key in ("ambiguous", "omitted", "incomplete", "needs_review"):
        if bucket.get(key):
            parts.append(f"{key} {bucket[key]}")
            if key == "ambiguous" and bucket.get("ambiguous_no_conflict"):
                parts[-1] += f" ({bucket['ambiguous_no_conflict']} with no conflicting record, no single value)"
    if bucket.get("blocked_by"):
        parts.append(
            "blocked "
            + ", ".join(f"{reason}={count}" for reason, count in bucket["blocked_by"].items())
        )
    return "; ".join(parts)


def measurements(
    cases: Sequence[Mapping[str, Any]],
    provider_ids: Sequence[str],
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Per-provider and per-case measurement profiles (Evaluation Plan 4.1).

    ``by_provider`` and ``by_case`` are the legacy executed-check tallies (their
    denominators vary with what a provider could run); ``facts`` is the
    fixed-denominator view and the one to compare providers with.
    """

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
    return {
        "by_provider": by_provider,
        "by_case": by_case,
        "facts": fact_tables(cases, provider_ids, as_of=as_of),
    }


__all__ = [
    "METRIC_VERSION",
    "SCORED_EXCLUDED",
    "blocked_class",
    "check_tallies",
    "dimension_display",
    "fact_display",
    "fact_invariant_errors",
    "fact_tables",
    "measurements",
    "op_matrix",
    "operation_fixture_counts",
]
