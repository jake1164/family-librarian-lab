"""Check computation: expectations in, per-check verdicts out.

Pure functions over normalised records. Every check records the evaluation
*dimension* it contributes to (coverage, accuracy, classification, ordering,
freshness) so the measurements block can be computed without re-reading names,
and so omitted facts count against coverage rather than accuracy.

Entity operations (``author_resolve``, ``series_for_book``,
``series_members``) use the **agreement rule**: every record whose name matches
the expected name is evaluated, and records that disagree make the check
``ambiguous`` with each record id listed. Nothing is ever chosen silently.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .publication import UNKNOWN, classify_date, effective_status
from .matcher import (
    compare_date,
    compare_position,
    isbn_equal,
    match_name,
    norm_author,
    norm_title,
)
from .model import (
    AuthorRecord,
    BookRecord,
    Operation,
    Outcome,
    SeriesMember,
    SeriesRecord,
)

ENTITY_OPERATIONS = {
    Operation.AUTHOR_RESOLVE,
    Operation.AUTHOR_WORKS,
    Operation.AUTHOR_SERIES,
    Operation.SERIES_FOR_BOOK,
    Operation.SERIES_MEMBERS,
}

DIMENSIONS = frozenset({"coverage", "accuracy", "classification", "ordering", "freshness"})

_RANKS = {"exact": 5, "normalized": 4, "alias": 3, "subtitle": 2, "fuzzy": 1, "none": 0}


def check(
    name: str,
    outcome: Outcome,
    expected: Any,
    observed: Any,
    *,
    dimension: str,
    match: Any | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    """One check. ``dimension`` is mandatory: there is no default.

    Omission (``not_found``) belongs to coverage; see
    :mod:`metadata_lab.measurements` for how each dimension is counted.
    """

    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown check dimension {dimension!r}")
    return {
        "name": name,
        "outcome": outcome.value,
        "expected": expected,
        "observed": observed,
        "match_kind": getattr(match, "kind", None),
        "evidence": evidence if evidence is not None else getattr(match, "evidence", None),
        "dimension": dimension,
    }

def _rank(match: Any) -> int:
    return _RANKS.get(getattr(match, "kind", "none"), 0)


def _best_match(
    expected: str,
    observed_values: Sequence[str],
    *,
    aliases: Iterable[str] = (),
    field: str = "title",
    context_terms: Iterable[str] = (),
) -> tuple[Any, str | None]:
    best: Any | None = None
    best_observed: str | None = None
    for value in observed_values:
        match = match_name(
            expected, value, tuple(aliases), field=field, context_terms=tuple(context_terms)
        )
        if best is None or _rank(match) > _rank(best):
            best, best_observed = match, value
        if match.is_match:
            break
    if best is None:
        return match_name(expected, ""), None
    return best, best_observed


def _missing_outcome(truncated: bool) -> Outcome:
    """A missing item is INCOMPLETE when paging stopped early, never DIFFERS."""

    return Outcome.INCOMPLETE if truncated else Outcome.NOT_FOUND


def _name_check(
    name: str,
    expected: str | None,
    observed_values: Sequence[str],
    *,
    dimension: str,
    aliases: Iterable[str] = (),
    field: str = "title",
    context_terms: Iterable[str] = (),
    truncated: bool,
) -> dict[str, Any] | None:
    if expected is None:
        return None
    match, observed = _best_match(
        str(expected),
        observed_values,
        aliases=aliases,
        field=field,
        context_terms=context_terms,
    )
    if match.is_match:
        return check(name, Outcome.OK, expected, observed, match=match, dimension=dimension)
    if match.kind == "fuzzy":
        return check(name, Outcome.AMBIGUOUS, expected, observed, match=match, dimension=dimension)
    return check(
        name, _missing_outcome(truncated), expected, observed, match=match, dimension=dimension
    )


#: Outcomes that say nothing about the value itself: the record simply does not contain the item.
_NO_EVIDENCE = frozenset({Outcome.NOT_FOUND.value, Outcome.INCOMPLETE.value})


def _merge(name: str, per_record: list[tuple[str, dict[str, Any]]], dimension: str) -> dict[str, Any]:
    """Apply the agreement rule to one check evaluated against several records."""

    outcomes = {item["outcome"] for _, item in per_record}
    detail = "; ".join(f"{record_id or '(no id)'}: {item['outcome']}" for record_id, item in per_record)
    if len(outcomes) == 1:
        merged = dict(per_record[0][1])
        if len(per_record) > 1:
            merged["evidence"] = f"{merged.get('evidence') or ''} [records agree: {detail}]".strip()
        return merged
    merged = check(
        name,
        Outcome.AMBIGUOUS,
        per_record[0][1]["expected"],
        [item["observed"] for _, item in per_record],
        evidence=f"records disagree, nothing chosen: {detail}",
        dimension=dimension,
    )
    # The rule above never chooses. This only records *why* it was ambiguous, so a
    # reader can tell a real conflict (ok against differs) from several same-named
    # series where just one holds the item and the rest merely lack it.
    informative = outcomes - _NO_EVIDENCE
    conflicting = not (
        len(informative) <= 1 or informative == {Outcome.OK.value, Outcome.AMBIGUOUS.value}
    )
    # ``resolved`` is the one non-conflicting observation about the field itself (the
    # member is present, its position is right or wrong). It never says which series is
    # the right one: that stays ambiguous, and the measurement reports the two apart.
    resolved: str | None = None
    if not conflicting:
        if informative == {Outcome.OK.value, Outcome.AMBIGUOUS.value}:
            resolved = Outcome.OK.value
        elif informative in ({Outcome.OK.value}, {Outcome.DIFFERS.value}):
            resolved = next(iter(informative))
    merged["merge"] = {
        "records": len(per_record),
        "holding": sum(1 for _, item in per_record if item["outcome"] not in _NO_EVIDENCE),
        "conflicting": conflicting,
        "resolved": resolved,
    }
    return merged


def _expired(expectation: Mapping[str, Any], as_of: str | None) -> bool:
    valid_until = expectation.get("valid_until")
    return bool(valid_until and as_of and str(valid_until) < str(as_of))


def _review_reason(expectation: Mapping[str, Any], as_of: str | None) -> str | None:
    """Why this expectation must not be scored automatically, or ``None``."""

    if _expired(expectation, as_of):
        return f"reference expired (valid_until {expectation['valid_until']} < as_of {as_of})"
    if expectation.get("manual_review") is True:
        return "manual_review: observed value shown, not scored"
    return None


def _is_number(value: Any) -> bool:
    try:
        float(str(value))
    except (TypeError, ValueError):
        return False
    return True


# --- book -----------------------------------------------------------------------


def _field_present(record: BookRecord, field_name: str) -> bool:
    value = getattr(record, field_name, None)
    if field_name == "cover":
        return bool(value)
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return value is not None and str(value).strip() != ""


def _best_book(records: Sequence[BookRecord], expect: Mapping[str, Any]) -> BookRecord | None:
    if not records:
        return None
    title = expect.get("title")
    if title:
        best: BookRecord | None = None
        best_rank = -1
        for record in records:
            rank = _rank(match_name(str(title), record.title))
            if rank > best_rank:
                best, best_rank = record, rank
        return best
    return records[0]


def _work_type_check(
    name: str,
    expected: str,
    observed: str | None,
    *,
    dimension: str,
    missing_evidence: str = "no classification supplied",
) -> dict[str, Any]:
    if not observed or observed == "unknown":
        return check(
            name,
            Outcome.NOT_FOUND,
            expected,
            observed or "unknown",
            evidence=missing_evidence,
            dimension=dimension,
        )
    if observed == expected:
        return check(name, Outcome.OK, expected, observed, dimension=dimension)
    return check(
        name,
        Outcome.DIFFERS,
        expected,
        observed,
        evidence=f"classification {observed!r} != {expected!r}",
        dimension=dimension,
    )


def _date_check(
    name: str,
    expected: Any,
    observed: Any,
    *,
    dimension: str,
    missing_evidence: str = "provider supplied no date",
    alternatives: Sequence[Any] = (),
) -> dict[str, Any]:
    """Compare a date; ``alternatives`` are further dates the references legitimately allow."""

    if observed in (None, ""):
        return check(
            name,
            Outcome.NOT_FOUND,
            [expected, *alternatives] if alternatives else expected,
            None,
            evidence=missing_evidence,
            dimension=dimension,
        )
    result = compare_date(expected, observed)
    if not result.equal and alternatives:
        for candidate in alternatives:
            other = compare_date(candidate, observed)
            if other.equal:
                return check(
                    name,
                    Outcome.OK,
                    [expected, *alternatives],
                    observed,
                    evidence=f"matches accepted alternative {candidate}: {other.note}",
                    dimension=dimension,
                )
        return check(
            name,
            Outcome.DIFFERS,
            [expected, *alternatives],
            observed,
            evidence=f"{observed} matches none of {[expected, *alternatives]}",
            dimension=dimension,
        )
    return check(
        name,
        Outcome.OK if result.equal else Outcome.DIFFERS,
        [expected, *alternatives] if alternatives else expected,
        observed,
        evidence=result.note,
        dimension=dimension,
    )


def _book_checks(
    expect: Mapping[str, Any],
    records: Sequence[BookRecord],
    truncated: bool,
    context_terms: Iterable[str],
    as_of: str | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    titles = [record.title for record in records]

    title_check = _name_check(
        "book.title",
        expect.get("title"),
        titles,
        context_terms=context_terms,
        truncated=truncated,
        dimension="coverage",
    )
    if title_check:
        checks.append(title_check)

    for index, author in enumerate(str(a) for a in expect.get("authors") or []):
        observed_authors = [name for record in records for name in record.authors]
        found = _name_check(
            f"book.authors[{index}]",
            author,
            observed_authors,
            field="author",
            truncated=truncated,
            dimension="accuracy",
        )
        if found:
            checks.append(found)

    if "isbn13" in expect:
        expected_isbn = str(expect["isbn13"])
        observed_isbns = [isbn for record in records for isbn in (record.isbn13 + record.isbn10)]
        found = any(isbn_equal(expected_isbn, isbn) for isbn in observed_isbns)
        checks.append(
            check(
                "book.isbn13",
                Outcome.OK if found else _missing_outcome(truncated),
                expected_isbn,
                observed_isbns,
                evidence=f'isbn "{expected_isbn}" present'
                if found
                else f'isbn "{expected_isbn}" not returned (or its check digit is invalid)',
                dimension="accuracy",
            )
        )

    if "min_fields" in expect:
        record = _best_book(records, expect)
        missing = [
            name for name in expect["min_fields"] if record is None or not _field_present(record, name)
        ]
        # Omission is not inaccuracy: a missing field is coverage, so the check is
        # not_found (the op becomes partial), never differs_from_reference.
        checks.append(
            check(
                "book.min_fields",
                Outcome.NOT_FOUND if missing else Outcome.OK,
                list(expect["min_fields"]),
                {"record": record.title if record else None, "missing": missing},
                evidence="all expected fields present"
                if not missing
                else f"omitted field(s): {', '.join(missing)}",
                dimension="coverage",
            )
        )

    if "work_type" in expect:
        record = _best_book(records, expect)
        checks.append(
            _work_type_check(
                "book.work_type",
                str(expect["work_type"]),
                record.work_type if record else None,
                dimension="classification",
            )
        )

    if "format" in expect:
        record = _best_book(records, expect)
        observed = record.format if record else None
        checks.append(
            check(
                "book.format",
                Outcome.NOT_FOUND
                if not observed or observed == "unknown"
                else (Outcome.OK if observed == str(expect["format"]) else Outcome.DIFFERS),
                str(expect["format"]),
                observed or "unknown",
                evidence="provider supplied no format"
                if not observed or observed == "unknown"
                else f"format {observed!r}",
                dimension="accuracy",
            )
        )

    if "first_published" in expect:
        record = _best_book(records, expect)
        checks.append(
            _date_check(
                "book.first_published",
                expect["first_published"],
                record.first_published if record else None,
                dimension="freshness",
            )
        )

    if "published" in expect:
        record = _best_book(records, expect)
        checks.append(
            _date_check(
                "book.published",
                expect["published"],
                record.published if record else None,
                dimension="freshness",
            )
        )
    return checks


# --- series ---------------------------------------------------------------------


def _member_matches(
    member: SeriesMember, wanted: Mapping[str, Any], context_terms: Iterable[str]
) -> Any:
    return match_name(
        str(wanted.get("title", "")),
        member.title,
        tuple(wanted.get("aliases") or ()),
        field="title",
        context_terms=tuple(context_terms),
    )


def _publication_status_check(
    name: str,
    wanted: Mapping[str, Any],
    member: SeriesMember | None,
    as_of: str | None,
) -> dict[str, Any]:
    """Score a provider's release date against a dated publication-status assertion.

    The provider only ever supplies a date, so its status is derived from that date; it can be
    ``published``, ``scheduled`` or ``unknown``, never ``planned``. See :mod:`metadata_lab.publication`
    for the rules that stop a missing date or an uncertain reference from scoring a provider wrongly.
    """

    assertion = wanted["publication_status"]
    confidence = str(assertion.get("confidence") or wanted.get("confidence") or "low")
    expected_status, why = effective_status(assertion, wanted.get("published"), as_of)
    expected = {
        "status": expected_status,
        "as_of": assertion.get("as_of"),
        "confidence": confidence,
        "evidence": assertion.get("evidence"),
    }

    def result(outcome: Outcome, observed: Any, evidence: str) -> dict[str, Any]:
        return check(name, outcome, expected, observed, evidence=evidence, dimension="freshness")

    if member is None:
        return result(
            Outcome.NOT_FOUND, None, "member not present in this record, so no release date was supplied"
        )
    if expected_status == UNKNOWN:
        return result(
            Outcome.NEEDS_REVIEW,
            member.published,
            f"the reference cannot say ({why}); shown, not scored",
        )
    if not member.published:
        return result(
            Outcome.NOT_FOUND,
            None,
            "provider supplied no release date; a missing date is not evidence that the work is "
            "unpublished, so nothing is scored",
        )
    observed_status = classify_date(member.published, as_of)
    observed = {"published": member.published, "status": observed_status}
    if observed_status == UNKNOWN:
        return result(
            Outcome.NEEDS_REVIEW,
            observed,
            f"release date {member.published} cannot be placed before or after {as_of}",
        )
    agrees = observed_status == expected_status
    if confidence == "low":
        return result(
            Outcome.NEEDS_REVIEW,
            observed,
            f"reference confidence is low, so {'agreement' if agrees else 'disagreement'} is "
            f"reviewed, not scored ({why})",
        )
    if agrees:
        return result(Outcome.OK, observed, f"status {observed_status} agrees ({why})")
    if confidence == "high":
        return result(
            Outcome.DIFFERS,
            observed,
            f"provider's date implies {observed_status}; the reference says {expected_status} ({why})",
        )
    return result(
        Outcome.NEEDS_REVIEW,
        observed,
        f"provider's date implies {observed_status}; the reference says {expected_status} but only at "
        f"{confidence} confidence, so this is reviewed, not scored ({why})",
    )


def _member_checks_for_record(
    index: int,
    wanted: Mapping[str, Any],
    record: SeriesRecord,
    truncated: bool,
    context_terms: Iterable[str],
    as_of: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Checks for one expected member against one series record."""

    title = str(wanted.get("title", ""))
    evaluated: dict[str, dict[str, Any]] = {}

    best: Any | None = None
    best_member: SeriesMember | None = None
    for member in record.members:
        match = _member_matches(member, wanted, context_terms)
        if best is None or _rank(match) > _rank(best):
            best, best_member = match, member
        if match.is_match:
            break

    if best is None:
        evaluated[f"series.member[{index}].present"] = check(
            f"series.member[{index}].present",
            _missing_outcome(truncated),
            title,
            None,
            evidence="series record has no members",
            dimension="coverage",
        )
        return evaluated

    if best.is_match:
        evaluated[f"series.member[{index}].present"] = check(
            f"series.member[{index}].present",
            Outcome.OK,
            title,
            best_member.title,
            match=best,
            dimension="coverage",
        )
    elif best.kind == "fuzzy":
        evaluated[f"series.member[{index}].present"] = check(
            f"series.member[{index}].present",
            Outcome.AMBIGUOUS,
            title,
            best_member.title if best_member else None,
            match=best,
            dimension="coverage",
        )
    else:
        evaluated[f"series.member[{index}].present"] = check(
            f"series.member[{index}].present",
            _missing_outcome(truncated),
            title,
            best_member.title if best_member else None,
            match=best,
            dimension="coverage",
        )

    # Every remaining expectation still gets a check, so each dimension's
    # denominator is complete: an absent member yields "omitted" (not_found),
    # never a silently missing row.
    present_ok = best.is_match
    member = best_member if present_ok else None

    accepted = [str(value) for value in wanted.get("accepted_positions") or []]
    expected_position = wanted.get("position")
    if expected_position is not None or accepted:
        name = f"series.member[{index}].position"
        expected_value = expected_position if expected_position is not None else accepted
        if member is None:
            evaluated[name] = check(
                name,
                Outcome.NOT_FOUND,
                expected_value,
                None,
                evidence="member not present in this record, so no position was supplied",
                dimension="ordering",
            )
        else:
            observed_position = member.position
            if observed_position is None or str(observed_position).strip() == "":
                evaluated[name] = check(
                    name,
                    Outcome.NOT_FOUND,
                    expected_value,
                    None,
                    evidence="provider returned no position",
                    dimension="ordering",
                )
            elif accepted:
                agrees = any(compare_position(value, observed_position).equal for value in accepted)
                evaluated[name] = check(
                    name,
                    Outcome.OK if agrees else Outcome.DIFFERS,
                    accepted,
                    observed_position,
                    evidence=(
                        f"position {observed_position} matches an accepted value"
                        if agrees
                        else f"position {observed_position} matches none of {accepted}"
                    ),
                    dimension="ordering",
                )
            else:
                comparison = compare_position(expected_position, observed_position)
                evaluated[name] = check(
                    name,
                    Outcome.OK if comparison.equal else Outcome.DIFFERS,
                    str(expected_position),
                    observed_position,
                    evidence=comparison.note,
                    dimension="ordering",
                )

    if "work_type" in wanted:
        evaluated[f"series.member[{index}].work_type"] = _work_type_check(
            f"series.member[{index}].work_type",
            str(wanted["work_type"]),
            member.work_type if member is not None else None,
            missing_evidence="member not present in this record, so no classification was supplied",
            dimension="classification",
        )

    if "published" in wanted:
        evaluated[f"series.member[{index}].published"] = _date_check(
            f"series.member[{index}].published",
            wanted["published"],
            member.published if member is not None else None,
            missing_evidence="member not present in this record, so no date was supplied",
            dimension="freshness",
            alternatives=wanted.get("accepted_published") or (),
        )

    if "publication_status" in wanted:
        evaluated[f"series.member[{index}].publication_status"] = _publication_status_check(
            f"series.member[{index}].publication_status", wanted, member, as_of
        )

    for author_index, author in enumerate(str(a) for a in wanted.get("authors") or []):
        name = f"series.member[{index}].authors[{author_index}]"
        if member is None or not member.authors:
            evaluated[name] = check(
                name,
                Outcome.NOT_FOUND,
                author,
                [] if member is None else member.authors,
                evidence=(
                    "member not present in this record, so no authors were supplied"
                    if member is None
                    else "provider supplied no member authors"
                ),
                dimension="accuracy",
            )
            continue
        match, observed = _best_match(author, member.authors, field="author")
        evaluated[name] = check(
            name,
            Outcome.OK
            if match.is_match
            else (Outcome.AMBIGUOUS if match.kind == "fuzzy" else _missing_outcome(truncated)),
            author,
            observed,
            match=match,
            dimension="accuracy",
        )
    return evaluated


def _forbid_check(
    index: int,
    entry: Mapping[str, Any],
    records: Sequence[SeriesRecord],
    context_terms: Iterable[str],
) -> dict[str, Any]:
    """A ``forbid`` entry passes when absent and differs when present."""

    title = str(entry.get("title", ""))
    violations: list[dict[str, Any]] = []
    seen = False
    for record in records:
        for member in record.members:
            match = match_name(title, member.title, field="title", context_terms=tuple(context_terms))
            if not match.is_match:
                continue
            seen = True
            record_id = record.provider_id or member.provider_id
            if "work_type" in entry and member.work_type == str(entry["work_type"]):
                violations.append(
                    {
                        "record": record_id,
                        "title": member.title,
                        "attribute": "work_type",
                        "observed": member.work_type,
                    }
                )
            if "kind" in entry and member.kind == str(entry["kind"]):
                violations.append(
                    {
                        "record": record_id,
                        "title": member.title,
                        "attribute": "kind",
                        "observed": member.kind,
                    }
                )
            if "position" in entry:
                comparison = compare_position(entry["position"], member.position)
                if comparison.equal:
                    violations.append(
                        {
                            "record": record_id,
                            "title": member.title,
                            "attribute": "position",
                            "observed": member.position,
                        }
                    )
            if "published" in entry and member.published:
                if compare_date(entry["published"], member.published).equal:
                    violations.append(
                        {
                            "record": record_id,
                            "title": member.title,
                            "attribute": "published",
                            "observed": member.published,
                        }
                    )
            for author in entry.get("authors") or []:
                if any(
                    match_name(str(author), name, field="author").is_match
                    for name in member.authors
                ):
                    violations.append(
                        {
                            "record": record_id,
                            "title": member.title,
                            "attribute": "authors",
                            "observed": author,
                        }
                    )
    dimension = "classification" if "work_type" in entry or "kind" in entry else (
        "ordering" if "position" in entry else ("freshness" if "published" in entry else "accuracy")
    )
    name = f"series.forbid[{index}]"
    if violations:
        return check(
            name,
            Outcome.DIFFERS,
            dict(entry),
            violations,
            evidence="forbidden claim present: "
            + "; ".join(f"{v['title']} {v['attribute']}={v['observed']}" for v in violations),
            dimension=dimension,
        )
    return check(
        name,
        Outcome.OK,
        dict(entry),
        {"found": seen, "violations": 0},
        evidence="forbidden claim absent" if not seen else "found, but not with the forbidden value",
        dimension=dimension,
    )


def _series_checks(
    operation: Operation,
    expect: Mapping[str, Any],
    records: Sequence[SeriesRecord],
    truncated: bool,
    context_terms: Iterable[str],
    as_of: str | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    expected_series = expect.get("series")
    if expected_series is not None:
        matching = [
            record
            for record in records
            if match_name(
                str(expected_series),
                record.name,
                tuple(expect.get("series_aliases") or ()),
                field="series",
            ).is_match
        ]
    else:
        matching = list(records)

    if expected_series is not None:
        if matching:
            checks.append(
                check(
                    "series.name",
                    Outcome.OK,
                    expected_series,
                    [record.name for record in records],
                    evidence="matched: " + "; ".join(record.provider_id or record.name for record in matching),
                    dimension="coverage",
                )
            )
        else:
            checks.append(
                check(
                    "series.name",
                    _missing_outcome(truncated),
                    expected_series,
                    [record.name for record in records],
                    evidence="no returned series matches the expected name",
                    dimension="coverage",
                )
            )

    if matching and operation is Operation.SERIES_MEMBERS:
        for index, wanted in enumerate(expect.get("members") or []):
            reason = _review_reason(wanted, as_of)
            if reason:
                checks.append(
                    check(
                        f"series.member[{index}]",
                        Outcome.NEEDS_REVIEW,
                        wanted.get("title"),
                        [member.title for record in matching for member in record.members],
                        evidence=reason,
                        dimension="freshness",
                    )
                )
                continue
            per_record: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            for record in matching:
                evaluated = _member_checks_for_record(
                    index, wanted, record, truncated, context_terms, as_of
                )
                for name, item in evaluated.items():
                    per_record.setdefault(name, []).append((record.provider_id, item))
            for name, entries in per_record.items():
                dimension = next(iter(entries))[1]["dimension"]
                checks.append(_merge(name, entries, dimension))

        for index, entry in enumerate(expect.get("forbid") or []):
            checks.append(_forbid_check(index, entry, matching, context_terms))

        if "main_count" in expect:
            members = [member for record in matching for member in record.members]
            known = [member for member in members if member.work_type != "unknown"]
            counted = [member for member in known if member.work_type == "novel"]
            if not known:
                checks.append(
                    check(
                        "series.main_count",
                        Outcome.NEEDS_REVIEW,
                        int(expect["main_count"]),
                        {"members": len(members), "classified": 0},
                        evidence="no member classification supplied, so a main count cannot be scored",
                        dimension="classification",
                    )
                )
            else:
                checks.append(
                    check(
                        "series.main_count",
                        Outcome.OK if len(counted) == int(expect["main_count"]) else Outcome.DIFFERS,
                        int(expect["main_count"]),
                        len(counted),
                        evidence=(
                            f"counted {len(counted)} of {len(known)} classified member(s) as novels"
                            + (
                                f"; {len(members) - len(known)} unclassified member(s) ignored"
                                if len(members) != len(known)
                                else ""
                            )
                        ),
                        dimension="classification",
                    )
                )
    return checks


# --- author ---------------------------------------------------------------------


def _forbid_author_title(
    index: int, forbidden: str, matching: Sequence[AuthorRecord]
) -> dict[str, Any]:
    """A work that must not be attributed to the resolved author (a namesake's book).

    An author that came back with no works at all cannot be said to have avoided the
    forbidden title, so that case is ``not_found`` (omitted), never a vacuous pass.
    """

    name = f"author.forbid_titles[{index}]"
    works = [work for record in matching for work in record.works]
    if not works:
        return check(
            name,
            Outcome.NOT_FOUND,
            forbidden,
            [],
            evidence="the matched author record lists no works, so the absence proves nothing",
            dimension="accuracy",
        )
    hits = [work for work in works if match_name(forbidden, work, field="title").is_match]
    if hits:
        return check(
            name,
            Outcome.DIFFERS,
            forbidden,
            hits,
            evidence="forbidden work attributed to the resolved author: " + "; ".join(hits),
            dimension="accuracy",
        )
    return check(
        name,
        Outcome.OK,
        forbidden,
        {"violations": 0},
        evidence="forbidden work absent from the resolved author's works",
        dimension="accuracy",
    )


def _author_checks(
    expect: Mapping[str, Any],
    records: Sequence[AuthorRecord],
    truncated: bool,
    as_of: str | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_name = expect.get("name")
    matching = list(records)
    if expected_name:
        matching = [
            record
            for record in records
            if match_name(
                str(expected_name), record.name, tuple(expect.get("aliases") or ()), field="author"
            ).is_match
        ]

    if expected_name:
        if matching:
            checks.append(
                check(
                    "author.name",
                    Outcome.OK,
                    expected_name,
                    [record.name for record in records],
                    evidence="matched: " + "; ".join(record.provider_id or record.name for record in matching),
                    dimension="coverage",
                )
            )
        else:
            checks.append(
                check(
                    "author.name",
                    _missing_outcome(truncated),
                    expected_name,
                    [record.name for record in records],
                    evidence="no returned author matches the expected name",
                    dimension="coverage",
                )
            )

    if not matching:
        return checks

    titles = [work for record in matching for work in record.works]
    for index, title in enumerate(expect.get("titles") or []):
        reason = _review_reason(expect, as_of)
        if reason:
            checks.append(
                check(
                    f"author.titles[{index}]",
                    Outcome.NEEDS_REVIEW,
                    str(title),
                    titles,
                    evidence=reason,
                    dimension="freshness",
                )
            )
            continue
        per_record = []
        for record in matching:
            found = _name_check(
                f"author.titles[{index}]",
                str(title),
                record.works,
                truncated=truncated,
                dimension="coverage",
            )
            per_record.append((record.provider_id, found))
        checks.append(_merge(f"author.titles[{index}]", per_record, "coverage"))

    for index, forbidden in enumerate(expect.get("forbid_titles") or []):
        checks.append(_forbid_author_title(index, str(forbidden), matching))

    series_names = [ref.name for record in matching for ref in record.series]
    for index, series in enumerate(expect.get("series") or []):
        per_record = []
        for record in matching:
            found = _name_check(
                f"author.series[{index}]",
                str(series),
                [ref.name for ref in record.series],
                field="series",
                truncated=truncated,
                dimension="coverage",
            )
            per_record.append((record.provider_id, found))
        checks.append(_merge(f"author.series[{index}]", per_record, "coverage"))
    return checks


def _apply_unresolved(
    checks: list[dict[str, Any]], unresolved: Mapping[str, int]
) -> list[dict[str, Any]]:
    """Turn "the adapter could not follow a reference" into ``incomplete``.

    An unresolved reference is our gap, not the provider's silence: a missing
    author whose URI the adapter never fetched must never read as
    "provider supplied nothing".
    """

    if not unresolved:
        return checks
    stems = {kind.rstrip("s").lower(): count for kind, count in unresolved.items() if kind}
    for item in checks:
        if item["outcome"] != Outcome.NOT_FOUND.value:
            continue
        name = str(item["name"]).lower()
        if any(stem and stem in name for stem in stems):
            item["outcome"] = Outcome.INCOMPLETE.value
            item["evidence"] = "adapter did not resolve"
    return checks


def compute_checks(
    operation: Operation,
    expect: Mapping[str, Any],
    records: Sequence[Any],
    *,
    truncated: bool,
    context_terms: Iterable[str] = (),
    as_of: str | None = None,
    unresolved: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Compute every check an ``expect`` block asks for. No expectations, no checks."""

    if not expect:
        return []
    if operation in (Operation.BOOK_BY_TITLE, Operation.BOOK_BY_ISBN):
        checks = _book_checks(expect, records, truncated, context_terms, as_of)
    elif operation in (Operation.SERIES_FOR_BOOK, Operation.SERIES_MEMBERS):
        checks = _series_checks(operation, expect, records, truncated, context_terms, as_of)
    elif operation in (Operation.AUTHOR_RESOLVE, Operation.AUTHOR_WORKS, Operation.AUTHOR_SERIES):
        checks = _author_checks(expect, records, truncated, as_of)
    else:
        checks = []
    return _apply_unresolved(checks, unresolved or {})


# --- reference facts ------------------------------------------------------------


def _fact(name: str, dimension: str) -> dict[str, str]:
    return {"name": name, "dimension": dimension}


def expected_facts(
    operation: Operation, expect: Mapping[str, Any], *, as_of: str | None = None
) -> list[dict[str, str]]:
    """Every atomic reference fact an ``expect`` block asks for.

    The list depends on the fixture alone, never on what a provider returned, so
    the fact count is identical for every provider. Names and dimensions mirror
    what :func:`compute_checks` emits for a provider that answers everything; a
    unit test builds such a provider for every shipped fixture and fails if the
    two lists ever drift apart. A reference that is expired or marked for manual
    review is one ``needs_review`` fact, exactly as the checks report it.
    """

    if not expect:
        return []
    facts: list[dict[str, str]] = []

    if operation in (Operation.BOOK_BY_TITLE, Operation.BOOK_BY_ISBN):
        if expect.get("title") is not None:
            facts.append(_fact("book.title", "coverage"))
        for index, _author in enumerate(expect.get("authors") or []):
            facts.append(_fact(f"book.authors[{index}]", "accuracy"))
        if "isbn13" in expect:
            facts.append(_fact("book.isbn13", "accuracy"))
        if "min_fields" in expect:
            facts.append(_fact("book.min_fields", "coverage"))
        if "work_type" in expect:
            facts.append(_fact("book.work_type", "classification"))
        if "format" in expect:
            facts.append(_fact("book.format", "accuracy"))
        if "first_published" in expect:
            facts.append(_fact("book.first_published", "freshness"))
        if "published" in expect:
            facts.append(_fact("book.published", "freshness"))
        return facts

    if operation in (Operation.SERIES_FOR_BOOK, Operation.SERIES_MEMBERS):
        if expect.get("series") is not None:
            facts.append(_fact("series.name", "coverage"))
        if operation is not Operation.SERIES_MEMBERS:
            return facts
        for index, wanted in enumerate(expect.get("members") or []):
            if _review_reason(wanted, as_of):
                facts.append(_fact(f"series.member[{index}]", "freshness"))
                continue
            facts.append(_fact(f"series.member[{index}].present", "coverage"))
            if wanted.get("position") is not None or wanted.get("accepted_positions"):
                facts.append(_fact(f"series.member[{index}].position", "ordering"))
            if "work_type" in wanted:
                facts.append(_fact(f"series.member[{index}].work_type", "classification"))
            if "published" in wanted:
                facts.append(_fact(f"series.member[{index}].published", "freshness"))
            if "publication_status" in wanted:
                facts.append(_fact(f"series.member[{index}].publication_status", "freshness"))
            for author_index, _author in enumerate(wanted.get("authors") or []):
                facts.append(_fact(f"series.member[{index}].authors[{author_index}]", "accuracy"))
        for index, entry in enumerate(expect.get("forbid") or []):
            dimension = (
                "classification"
                if "work_type" in entry or "kind" in entry
                else (
                    "ordering"
                    if "position" in entry
                    else ("freshness" if "published" in entry else "accuracy")
                )
            )
            facts.append(_fact(f"series.forbid[{index}]", dimension))
        if "main_count" in expect:
            facts.append(_fact("series.main_count", "classification"))
        return facts

    if operation in (Operation.AUTHOR_RESOLVE, Operation.AUTHOR_WORKS, Operation.AUTHOR_SERIES):
        if expect.get("name"):
            facts.append(_fact("author.name", "coverage"))
        review = bool(_review_reason(expect, as_of))
        for index, _title in enumerate(expect.get("titles") or []):
            facts.append(_fact(f"author.titles[{index}]", "freshness" if review else "coverage"))
        for index, _forbidden in enumerate(expect.get("forbid_titles") or []):
            facts.append(_fact(f"author.forbid_titles[{index}]", "accuracy"))
        for index, _series in enumerate(expect.get("series") or []):
            facts.append(_fact(f"author.series[{index}]", "coverage"))
        return facts
    return facts


# --- duplicates -----------------------------------------------------------------


def duplicates(records: Sequence[Any]) -> list[dict[str, Any]]:
    """Find repeated entities inside one operation's records.

    Duplicates are reported, never silently dropped: a provider listing the same
    work twice is information about that provider's data.
    """

    groups: dict[str, list[str]] = {}
    for record in records:
        if isinstance(record, SeriesRecord):
            for member in record.members:
                key = f"{norm_title(member.title)}|{member.position or ''}"
                groups.setdefault(key, []).append(member.provider_id or record.provider_id or "")
        elif isinstance(record, BookRecord):
            key = f"{norm_title(record.title)}|{(record.isbn13 or record.isbn10 or [''])[0]}"
            groups.setdefault(key, []).append(record.provider_id or "")
        elif isinstance(record, AuthorRecord):
            key = norm_author(record.name)
            groups.setdefault(key, []).append(record.provider_id or "")

    found: list[dict[str, Any]] = []
    for key, provider_ids in groups.items():
        if len(provider_ids) > 1:
            found.append(
                {
                    "key": key,
                    "count": len(provider_ids),
                    "provider_ids": sorted({pid for pid in provider_ids if pid}),
                }
            )
    return found


__all__ = [
    "DIMENSIONS",
    "ENTITY_OPERATIONS",
    "check",
    "compute_checks",
    "duplicates",
    "expected_facts",
]
