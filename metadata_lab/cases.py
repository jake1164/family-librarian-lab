"""Suite YAML loading and validation.

Suites are versioned test definitions, so the same suite run months apart stays
comparable. Validation is strict and names the file, case and key path: a typo
in a case file must never silently become a not-yet-supported test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import yaml

from .model import WORK_TYPES, Operation

SUITE_KEYS = {"suite", "version", "providers", "cases", "as_of"}
CASE_KEYS = {
    "id",
    "description",
    "reference",
    "input",
    "operations",
    "traversal",
    "options",
    "expect",
    "tier",
    "tags",
    "notes",
}
REFERENCE_KEYS = {"source", "verified_on", "confidence"}
INPUT_KEYS = {"title", "author", "isbn", "series"}
OPTION_KEYS = {"max_pages"}

CONFIDENCES = frozenset({"high", "medium", "low"})
TIERS = frozenset({"easy", "medium", "hard"})
FACT_TYPES = frozenset({"fixed", "time_sensitive", "definition_dependent", "coverage"})
POSITION_SCHEMES = frozenset({"publication", "story", "provider_default"})

#: Provenance keys allowed on any expectation or member.
PROVENANCE_KEYS = {
    "source",
    "verified_on",
    "confidence",
    "fact_type",
    "valid_until",
    "manual_review",
}

#: A book-record completeness check may name any of these record fields.
MIN_FIELD_NAMES = {
    "subtitle",
    "authors",
    "isbn10",
    "isbn13",
    "publisher",
    "published",
    "pages",
    "language",
    "cover",
    "description",
    "series",
    "author_ids",
    "first_published",
    "format",
    "work_type",
}

EXPECT_KEYS: dict[Operation, set[str]] = {
    Operation.BOOK_BY_TITLE: {
        "title",
        "authors",
        "isbn13",
        "min_fields",
        "work_type",
        "first_published",
        "format",
    }
    | PROVENANCE_KEYS,
    Operation.BOOK_BY_ISBN: {
        "title",
        "authors",
        "isbn13",
        "min_fields",
        "work_type",
        "first_published",
        "format",
        "published",
    }
    | PROVENANCE_KEYS,
    Operation.SERIES_FOR_BOOK: {"series", "series_aliases", "exhaustive"} | PROVENANCE_KEYS,
    Operation.SERIES_MEMBERS: {
        "series",
        "series_aliases",
        "members",
        "main_count",
        "exhaustive",
        "forbid",
    }
    | PROVENANCE_KEYS,
    Operation.AUTHOR_RESOLVE: {"name", "aliases"} | PROVENANCE_KEYS,
    Operation.AUTHOR_WORKS: {"titles", "exhaustive"} | PROVENANCE_KEYS,
    Operation.AUTHOR_SERIES: {"series"} | PROVENANCE_KEYS,
}

REQUIRED_INPUT: dict[Operation, tuple[str, ...]] = {
    Operation.BOOK_BY_TITLE: ("title",),
    Operation.BOOK_BY_ISBN: ("isbn",),
    Operation.SERIES_FOR_BOOK: ("title",),
    Operation.SERIES_MEMBERS: ("series",),
    Operation.AUTHOR_RESOLVE: ("author",),
    Operation.AUTHOR_WORKS: ("author",),
    Operation.AUTHOR_SERIES: ("author",),
}

MEMBER_KEYS = {
    "title",
    "position",
    "aliases",
    "kind",
    "authors",
    "work_type",
    "published",
    "accepted_positions",
    "position_scheme",
} | PROVENANCE_KEYS

FORBID_KEYS = {"title", "work_type", "position", "authors", "published", "kind", "reason"}


class CaseValidationError(ValueError):
    """A suite file does not match the schema; the message names the location."""


def _fail(where: str, message: str) -> NoReturn:
    raise CaseValidationError(f"{where}: {message}")


@dataclass(frozen=True)
class Reference:
    """Where an expected value came from. ``verified_on is None`` => UNVERIFIED."""

    source: str
    verified_on: str | None = None
    confidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "verified_on": self.verified_on,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Case:
    id: str
    description: str
    reference: Reference
    input: dict[str, str]
    operations: tuple[Operation, ...]
    traversal: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    tier: str | None = None
    tags: tuple[str, ...] = ()
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "reference": self.reference.to_dict(),
            "verified": self.verified,
            "tier": self.tier,
            "tags": list(self.tags),
            "notes": self.notes,
            "input": dict(self.input),
            "operations": [operation.value for operation in self.operations],
            "traversal": self.traversal,
            "options": dict(self.options),
            "expect": self.expect,
        }


@dataclass(frozen=True)
class Suite:
    name: str
    version: int
    cases: tuple[Case, ...]
    source: str
    sha256: str
    providers: tuple[str, ...] | None = None  # None means "all registered providers"
    as_of: str | None = None

    @property
    def provider_label(self) -> str:
        return "all" if self.providers is None else ",".join(self.providers)

    def case(self, case_id: str) -> Case:
        for item in self.cases:
            if item.id == case_id:
                return item
        raise KeyError(case_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "sha256": self.sha256,
            "providers": self.provider_label,
            "as_of": self.as_of,
            "cases": [case.to_dict() for case in self.cases],
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_mapping(value: Any, where: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(where, f"{what} must be a mapping")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: Iterable[str], where: str) -> None:
    for key in mapping:
        if key not in allowed:
            _fail(where, f"unknown key {key!r} (allowed: {', '.join(sorted(allowed))})")


def _parse_operations(value: Any, where: str) -> tuple[Operation, ...]:
    if not isinstance(value, list) or not value:
        _fail(where, "operations must be a non-empty list")
    parsed: list[Operation] = []
    for raw in value:
        try:
            operation = Operation(raw)
        except ValueError:
            _fail(f"{where}.operations", f"unknown operation {raw!r}")
        if operation not in parsed:
            parsed.append(operation)
    return tuple(parsed)


def _parse_iso_date(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        _fail(where, "must be a YYYY-MM-DD string or null")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(where, f"{value!r} is not YYYY-MM-DD")
    return value


def _parse_reference(value: Any, where: str) -> Reference:
    mapping = _require_mapping(value, where, "reference")
    _reject_unknown(mapping, REFERENCE_KEYS, where)
    source = mapping.get("source")
    if not isinstance(source, str) or not source.strip():
        _fail(f"{where}.reference", "'source' is required and must be a non-empty string")
    verified_on = _parse_iso_date(mapping.get("verified_on"), f"{where}.reference")
    confidence = mapping.get("confidence")
    if confidence is not None and confidence not in CONFIDENCES:
        _fail(
            f"{where}.reference",
            f"'confidence' must be one of {', '.join(sorted(CONFIDENCES))}",
        )
    return Reference(source=source.strip(), verified_on=verified_on, confidence=confidence)


def _validate_provenance(mapping: Mapping[str, Any], where: str) -> None:
    """Validate the expectation-level provenance keys, if present."""

    if "source" in mapping and not isinstance(mapping["source"], str):
        _fail(f"{where}.source", "must be a string")
    if "verified_on" in mapping:
        _parse_iso_date(mapping["verified_on"], f"{where}.verified_on")
    if "confidence" in mapping and mapping["confidence"] not in CONFIDENCES:
        _fail(f"{where}.confidence", f"must be one of {', '.join(sorted(CONFIDENCES))}")
    if "fact_type" in mapping and mapping["fact_type"] not in FACT_TYPES:
        _fail(f"{where}.fact_type", f"must be one of {', '.join(sorted(FACT_TYPES))}")
    if "valid_until" in mapping:
        _parse_iso_date(mapping["valid_until"], f"{where}.valid_until")
    if "manual_review" in mapping and not isinstance(mapping["manual_review"], bool):
        _fail(f"{where}.manual_review", "must be a boolean")


def _parse_input(value: Any, where: str) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _require_mapping(value, where, "input")
    _reject_unknown(mapping, INPUT_KEYS, where)
    parsed: dict[str, str] = {}
    for key, item in mapping.items():
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            _fail(f"{where}.{key}", "input values must be strings")
        text = str(item).strip()
        if text:
            parsed[key] = text
    return parsed


def _parse_options(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    mapping = _require_mapping(value, where, "options")
    _reject_unknown(mapping, OPTION_KEYS, where)
    options: dict[str, Any] = {}
    if "max_pages" in mapping:
        max_pages = mapping["max_pages"]
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            _fail(f"{where}.max_pages", "must be a positive integer")
        options["max_pages"] = max_pages
    return options


def _check_work_type(mapping: Mapping[str, Any], where: str) -> None:
    if "work_type" in mapping and mapping["work_type"] not in WORK_TYPES:
        _fail(f"{where}.work_type", f"must be a known work type ({', '.join(sorted(WORK_TYPES))})")


def _check_string_list(mapping: Mapping[str, Any], key: str, where: str) -> None:
    if key in mapping:
        value = mapping[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            _fail(f"{where}.{key}", "must be a list of strings")


def _parse_expect(value: Any, operations: tuple[Operation, ...], where: str) -> dict[str, Any]:
    if value is None:
        return {}
    mapping = _require_mapping(value, where, "expect")
    for key, entry in mapping.items():
        try:
            operation = Operation(key)
        except ValueError:
            _fail(where, f"unknown expect operation {key!r}")
        if operation not in operations:
            _fail(where, f"expect.{key} is not listed in operations")
        allowed = EXPECT_KEYS[operation]
        entry_where = f"{where}.{key}"
        entry_mapping = _require_mapping(entry, entry_where, "expect entry")
        _reject_unknown(entry_mapping, allowed, entry_where)
        _validate_provenance(entry_mapping, entry_where)
        _check_work_type(entry_mapping, entry_where)
        _check_string_list(entry_mapping, "authors", entry_where)
        _check_string_list(entry_mapping, "titles", entry_where)
        if operation is Operation.AUTHOR_SERIES:
            _check_string_list(entry_mapping, "series", entry_where)
        if "exhaustive" in entry_mapping and not isinstance(entry_mapping["exhaustive"], bool):
            _fail(f"{entry_where}.exhaustive", "must be a boolean")
        if "min_fields" in entry_mapping:
            names = entry_mapping["min_fields"]
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                _fail(f"{entry_where}.min_fields", "must be a list of field names")
            unknown = [n for n in names if n not in MIN_FIELD_NAMES]
            if unknown:
                _fail(
                    f"{entry_where}.min_fields",
                    f"unknown field(s) {', '.join(sorted(unknown))}",
                )
        if "main_count" in entry_mapping:
            main_count = entry_mapping["main_count"]
            if isinstance(main_count, bool) or not isinstance(main_count, int) or main_count < 0:
                _fail(f"{entry_where}.main_count", "must be a non-negative integer")
        forbid = entry_mapping.get("forbid")
        if forbid is not None:
            if not isinstance(forbid, list):
                _fail(f"{entry_where}.forbid", "must be a list")
            for index, item in enumerate(forbid):
                forbid_where = f"{entry_where}.forbid[{index}]"
                forbid_mapping = _require_mapping(item, forbid_where, "forbid entry")
                _reject_unknown(forbid_mapping, FORBID_KEYS, forbid_where)
                if not isinstance(forbid_mapping.get("title"), str):
                    _fail(forbid_where, "'title' is required")
                _check_work_type(forbid_mapping, forbid_where)
        members = entry_mapping.get("members")
        if members is not None:
            if not isinstance(members, list):
                _fail(f"{entry_where}.members", "must be a list")
            for index, member in enumerate(members):
                member_where = f"{entry_where}.members[{index}]"
                member_mapping = _require_mapping(member, member_where, "member")
                _reject_unknown(member_mapping, MEMBER_KEYS, member_where)
                if not isinstance(member_mapping.get("title"), str):
                    _fail(member_where, "'title' is required")
                _validate_provenance(member_mapping, member_where)
                _check_work_type(member_mapping, member_where)
                _check_string_list(member_mapping, "authors", member_where)
                _check_string_list(member_mapping, "accepted_positions", member_where)
                if "published" in member_mapping and not isinstance(
                    member_mapping["published"], (str, date)
                ):
                    _fail(f"{member_where}.published", "must be a date string")
                scheme = member_mapping.get("position_scheme")
                if scheme is not None and scheme not in POSITION_SCHEMES:
                    _fail(
                        f"{member_where}.position_scheme",
                        f"must be one of {', '.join(sorted(POSITION_SCHEMES))}",
                    )
    return mapping


def _validate_case(raw: Any, where: str, seen_ids: set[str]) -> Case:
    mapping = _require_mapping(raw, where, "case")
    _reject_unknown(mapping, CASE_KEYS, where)
    case_id = mapping.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        _fail(where, "'id' is required and must be a non-empty string")
    if case_id in seen_ids:
        _fail(where, f"duplicate case id {case_id!r}")
    seen_ids.add(case_id)
    where = f"{where} [{case_id}]"

    if "reference" not in mapping:
        _fail(f"{where}.reference", "'source' is required")
    reference = _parse_reference(mapping["reference"], f"{where}.reference")

    if "operations" not in mapping:
        _fail(where, "'operations' is required")
    operations = _parse_operations(mapping["operations"], where)
    case_input = _parse_input(mapping.get("input"), f"{where}.input")

    for operation in operations:
        for required in REQUIRED_INPUT[operation]:
            if required not in case_input:
                _fail(
                    f"{where}.input",
                    f"missing {required!r} required by operation {operation.value}",
                )

    traversal = mapping.get("traversal", False)
    if not isinstance(traversal, bool):
        _fail(f"{where}.traversal", "must be a boolean")

    description = mapping.get("description", "")
    if not isinstance(description, str):
        _fail(f"{where}.description", "must be a string")

    expect = _parse_expect(mapping.get("expect"), operations, f"{where}.expect")

    tier = mapping.get("tier")
    if tier is not None and tier not in TIERS:
        _fail(f"{where}.tier", f"must be one of {', '.join(sorted(TIERS))}")

    tags_raw = mapping.get("tags") or []
    if not isinstance(tags_raw, list) or not all(isinstance(tag, str) for tag in tags_raw):
        _fail(f"{where}.tags", "must be a list of strings")

    notes = mapping.get("notes")
    if notes is not None and not isinstance(notes, (str, list)):
        _fail(f"{where}.notes", "must be a string or a list of strings")

    return Case(
        id=case_id,
        description=description,
        reference=reference,
        input=case_input,
        operations=operations,
        traversal=traversal,
        options=_parse_options(mapping.get("options"), f"{where}.options"),
        expect=expect,
        verified=reference.verified_on is not None,
        tier=tier,
        tags=tuple(tags_raw),
        notes=notes,
    )


def load_suite_file(path: Path | str) -> Suite:
    """Load and validate one suite file."""

    target = Path(path)
    where = str(target)
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseValidationError(f"{where}: cannot read suite ({exc})") from exc
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CaseValidationError(f"{where}: invalid YAML ({exc})") from exc
    if not isinstance(document, dict):
        _fail(where, "suite file must contain a mapping")

    _reject_unknown(document, SUITE_KEYS, where)
    name = document.get("suite")
    if not isinstance(name, str) or not name.strip():
        _fail(where, "'suite' is required and must be a non-empty string")
    version = document.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        _fail(where, "'version' is required and must be an integer")

    providers_raw = document.get("providers", "all")
    providers: tuple[str, ...] | None
    if providers_raw == "all":
        providers = None
    elif isinstance(providers_raw, list) and all(isinstance(p, str) for p in providers_raw):
        providers = tuple(providers_raw)
    else:
        _fail(where, "'providers' must be \"all\" or a list of provider ids")

    cases_raw = document.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        _fail(where, "'cases' is required and must be a non-empty list")

    seen: set[str] = set()
    cases = tuple(
        _validate_case(raw, f"{where}: cases[{index}]", seen)
        for index, raw in enumerate(cases_raw)
    )
    return Suite(
        name=name.strip(),
        version=version,
        cases=cases,
        source=where,
        sha256=file_sha256(target),
        providers=providers,
        as_of=_parse_iso_date(document.get("as_of"), f"{where}.as_of"),
    )


def load_suites(paths: Iterable[Path | str]) -> list[Suite]:
    return [load_suite_file(path) for path in paths]


def suite_from_result(document: Mapping[str, Any]) -> Suite:
    """Rebuild a suite from a result document's embedded case definitions.

    Replay needs the definitions, not the YAML: a result file must be enough on
    its own. Older result files without ``operations`` fall back to the
    operations that actually ran.
    """

    if not isinstance(document, Mapping):
        raise CaseValidationError("result document must be a JSON object")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseValidationError("result document has no cases")

    cases: list[Case] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, Mapping):
            raise CaseValidationError(f"cases[{index}]: must be an object")
        case_id = str(raw.get("id") or f"case-{index}")
        reference_raw = raw.get("reference") or {}
        source = str(reference_raw.get("source") or "unknown reference")
        verified_on = reference_raw.get("verified_on")
        operations_raw = raw.get("operations")
        if operations_raw:
            operations = tuple(Operation(name) for name in operations_raw)
        else:
            seen = {
                row.get("operation")
                for row in raw.get("results") or []
                if isinstance(row, Mapping) and row.get("mode", "independent") == "independent"
            }
            operations = tuple(op for op in Operation if op.value in seen)
        if not operations:
            raise CaseValidationError(f"{case_id}: no operations to replay")
        cases.append(
            Case(
                id=case_id,
                description=str(raw.get("description") or ""),
                reference=Reference(
                    source=source,
                    verified_on=verified_on,
                    confidence=(reference_raw or {}).get("confidence")
                    if isinstance(reference_raw, Mapping)
                    else None,
                ),
                input=dict(raw.get("input") or {}),
                operations=operations,
                traversal=bool(raw.get("traversal", False)),
                options=dict(raw.get("options") or {}),
                expect=dict(raw.get("expect") or {}),
                verified=bool(raw.get("verified", verified_on is not None)),
                tier=raw.get("tier"),
                tags=tuple(raw.get("tags") or ()),
                notes=raw.get("notes"),
            )
        )

    suite_meta = document.get("suite") or {}
    providers = tuple(
        str(entry.get("id"))
        for entry in document.get("providers") or []
        if isinstance(entry, Mapping) and entry.get("id")
    )
    return Suite(
        name=str(suite_meta.get("name") or "replayed-suite"),
        version=int(suite_meta.get("version") or 0),
        cases=tuple(cases),
        source=str(suite_meta.get("source") or "replayed result"),
        sha256=str(suite_meta.get("sha256") or ""),
        providers=providers or None,
        as_of=(suite_meta.get("as_of") or None),
    )


__all__ = [
    "Case",
    "CaseValidationError",
    "Reference",
    "Suite",
    "file_sha256",
    "load_suite_file",
    "load_suites",
    "suite_from_result",
]
