"""What the local UI may offer, and what it may ask for.

Three jobs, all plain data: the suite catalogue, strict parsing of a browser
request (typed fields and provider ids only - never a URL), and the runs
directory. Credentials are never touched here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .cases import CaseValidationError, Suite, load_suite_file
from .compare import compare
from .engine import DEFAULT_MAX_CALLS, build_oneoff_suite
from .model import Operation

SUITES_DIR = Path(__file__).resolve().parent / "suites"
QUERY_TYPES = ("book", "isbn", "author", "series")
INPUT_KEYS = ("title", "author", "isbn", "series")
ONE_OFF_OPERATIONS = {
    "book": Operation.BOOK_BY_TITLE,
    "isbn": Operation.BOOK_BY_ISBN,
    "author": Operation.AUTHOR_RESOLVE,
    "series": Operation.SERIES_MEMBERS,
}
#: The input field each one-off query type cannot run without.
ONE_OFF_OPERATION_INPUT = {
    "book": "title",
    "isbn": "isbn",
    "author": "author",
    "series": "series",
}
RESULT_FILES = ("result.json", "ai.json", "report.html")
RUN_ID = re.compile(r"^[0-9A-Za-z._-]+$")
AS_OF = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT = 200
MAX_IDS = 64
MAX_RUNS = 500
RUN_BODY_KEYS = {
    "mode",
    "providers",
    "kind",
    "fields",
    "suite",
    "case_ids",
    "tags",
    "as_of",
    "traversal",
    "confirmed",
    "ignore_cooldown",
}
CASE_BODY_KEYS = {"kind", "fields"}
CHECK_BODY_KEYS = {"providers"}


class RequestError(ValueError):
    """A request the server refuses; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RunRequest:
    """A validated run: a suite to execute and the providers to execute it on."""

    suite: Suite
    providers: tuple[str, ...]
    traversal: bool | None = None
    as_of: str | None = None
    ignore_cooldown: bool = False


# --- suites -----------------------------------------------------------------------


def suite_catalogue() -> tuple[dict[str, Suite], list[dict[str, str]]]:
    """Named suites (keyed by file stem, as the CLI resolves them) plus broken files."""

    found: dict[str, Suite] = {}
    broken: list[dict[str, str]] = []
    for path in sorted(SUITES_DIR.glob("*.yaml")):
        try:
            found[path.stem] = load_suite_file(path)
        except CaseValidationError as exc:
            broken.append({"name": path.stem, "error": str(exc)})
    return found, broken


def suites_payload() -> dict[str, Any]:
    suites, broken = suite_catalogue()
    return {
        "suites": [
            {
                "name": name,
                "title": suite.name,
                "version": suite.version,
                "as_of": suite.as_of,
                "providers": suite.provider_label,
                "cases": [
                    {
                        "id": case.id,
                        "description": case.description,
                        "tags": list(case.tags),
                        "operations": [operation.value for operation in case.operations],
                        "traversal": case.traversal,
                        "max_calls": int(case.options.get("max_calls", DEFAULT_MAX_CALLS)),
                    }
                    for case in suite.cases
                ],
            }
            for name, suite in suites.items()
        ],
        "broken": broken,
        "query_types": list(QUERY_TYPES),
    }


# --- request parsing --------------------------------------------------------------


def _reject_unknown(body: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise RequestError(f"unknown field(s): {', '.join(unknown)}")


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{what} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise RequestError(f"{what} must not be empty")
    if len(cleaned) > MAX_TEXT:
        raise RequestError(f"{what} is longer than {MAX_TEXT} characters")
    if any(character < " " for character in cleaned):
        raise RequestError(f"{what} must be a single line")
    return cleaned


def _id_list(value: Any, what: str, known: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RequestError(f"{what} must be a non-empty list")
    if len(value) > MAX_IDS:
        raise RequestError(f"{what} lists more than {MAX_IDS} entries")
    cleaned = tuple(dict.fromkeys(_text(item, f"{what} entry") for item in value))
    unknown = [item for item in cleaned if item not in known]
    if unknown:
        raise RequestError(f"unknown {what}: {', '.join(unknown)}")
    return cleaned


def _fields(value: Any, kind: str) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise RequestError("fields must be an object")
    unknown = sorted(set(value) - set(INPUT_KEYS))
    if unknown:
        raise RequestError(f"unknown field(s): {', '.join(unknown)}")
    fields = {key: _text(item, f"fields.{key}") for key, item in value.items()}
    required = ONE_OFF_OPERATION_INPUT[kind]
    if required not in fields:
        raise RequestError(f"query type {kind!r} needs {required}")
    return fields


def _tags(value: Any) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or len(value) > MAX_IDS:
        raise RequestError(f"tags must be a list of at most {MAX_IDS} strings")
    return tuple(dict.fromkeys(_text(tag, "tag") for tag in value))


def _as_of(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not AS_OF.match(value):
        raise RequestError("as_of must be a date written YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise RequestError(f"as_of {value!r} is not a real date") from None
    return value


def _flag(value: Any, what: str) -> bool:
    if value in (None, False):
        return False
    if value is True:
        return True
    raise RequestError(f"{what} must be a boolean")


def parse_run(
    body: Mapping[str, Any], *, adapters: Mapping[str, type], catalogue: Mapping[str, Suite]
) -> RunRequest:
    """Validate a ``POST /api/run`` body. Nothing here can reach a provider yet."""

    _reject_unknown(body, RUN_BODY_KEYS)
    providers = _id_list(body.get("providers"), "provider", sorted(adapters))
    ignore_cooldown = _flag(body.get("ignore_cooldown"), "ignore_cooldown")
    mode = body.get("mode")
    if mode == "oneoff":
        kind = body.get("kind")
        if kind not in QUERY_TYPES:
            raise RequestError(f"kind must be one of {', '.join(QUERY_TYPES)}")
        try:
            suite = build_oneoff_suite(kind, _fields(body.get("fields"), kind))
        except ValueError as exc:
            raise RequestError(str(exc)) from None
        return RunRequest(
            suite=suite,
            providers=providers,
            as_of=_as_of(body.get("as_of")),
            ignore_cooldown=ignore_cooldown,
        )
    if mode != "suite":
        raise RequestError("mode must be 'oneoff' or 'suite'")

    name = body.get("suite")
    if not isinstance(name, str) or name not in catalogue:
        raise RequestError(f"unknown suite: {name!r}")
    if body.get("confirmed") is not True:
        # A suite spends many calls; the page asks for a second click and says so.
        raise RequestError("a suite run must be confirmed: true", status=409)
    suite = catalogue[name]
    cases = list(suite.cases)
    if body.get("case_ids") not in (None, []):
        wanted = _id_list(body.get("case_ids"), "case", [case.id for case in cases])
        cases = [case for case in cases if case.id in wanted]
    wanted_tags = _tags(body.get("tags"))
    if wanted_tags:
        cases = [case for case in cases if set(wanted_tags) & set(case.tags)]
    if not cases:
        raise RequestError("no case in this suite matches the filter")
    traversal = body.get("traversal")
    if traversal is not None and not isinstance(traversal, bool):
        raise RequestError("traversal must be a boolean")
    return RunRequest(
        suite=replace(suite, cases=tuple(cases)),
        providers=providers,
        traversal=traversal,
        as_of=_as_of(body.get("as_of")),
        ignore_cooldown=ignore_cooldown,
    )


def parse_check(body: Mapping[str, Any], *, adapters: Mapping[str, type]) -> tuple[str, ...]:
    _reject_unknown(body, CHECK_BODY_KEYS)
    return _id_list(body.get("providers"), "provider", sorted(adapters))


def parse_case(body: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """Validate a "save as case" request and return ``(kind, fields)``."""

    _reject_unknown(body, CASE_BODY_KEYS)
    kind = body.get("kind")
    if kind not in QUERY_TYPES:
        raise RequestError(f"kind must be one of {', '.join(QUERY_TYPES)}")
    return kind, _fields(body.get("fields"), kind)


def case_yaml(kind: str, fields: Mapping[str, str]) -> str:
    """A one-case suite file for a query the reader wants to keep.

    It is downloaded, never written into the repository, and it loads through the
    same validator as every tracked suite.
    """

    case_id = "-".join([kind, *fields.values()])
    document = {
        "suite": f"saved-{kind}",
        "version": 1,
        "providers": "all",
        "cases": [
            {
                "id": re.sub(r"[^0-9A-Za-z._-]+", "-", case_id)[:60].strip("-").lower(),
                "description": f"saved {kind} query from the metadata lab UI",
                "reference": {
                    "source": "saved from the local metadata lab UI; add a real source and a "
                    "verified_on date before trusting any outcome",
                    "verified_on": None,
                },
                "input": dict(fields),
                "operations": [ONE_OFF_OPERATIONS[kind].value],
                "traversal": False,
            }
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


# --- the runs directory -----------------------------------------------------------


def _summary(run_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
    run = document.get("run") or {}
    suite = document.get("suite") or {}
    rows = [
        row for case in document.get("cases") or [] for row in case.get("results") or []
    ]
    return {
        "id": run_id,
        "suite": suite.get("name"),
        "suite_version": suite.get("version"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "mode": run.get("mode"),
        "status": run.get("status"),
        "harness": (run.get("harness") or {}).get("status"),
        "as_of": run.get("as_of"),
        "providers": [entry.get("id") for entry in document.get("providers") or []],
        "rows": len(rows),
        # "partial" is about the run, not a row: it was interrupted or fully rate-limited.
        "partial": run.get("status") != "complete"
        or any(row.get("reason") == "cancelled" for row in rows),
        "partial_rows": sum(1 for row in rows if row.get("outcome") == "partial"),
        "resumed": bool(run.get("resumed_from"))
        or any(row.get("resumed") for row in rows),
    }


def _result_path(root: Path, run_id: str) -> Path:
    if not RUN_ID.match(run_id):
        raise RequestError("unknown run", status=404)
    path = Path(root) / run_id / "result.json"
    if not path.is_file():
        raise RequestError("unknown run", status=404)
    return path


def load_result(root: Path, run_id: str) -> dict[str, Any]:
    path = _result_path(root, run_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RequestError("run file is not readable JSON", status=404) from None
    if not isinstance(document, dict):
        raise RequestError("run file is not a result document", status=404)
    return document


def list_runs(root: Path) -> list[dict[str, Any]]:
    """Every run directory under the results root, newest first."""

    base = Path(root)
    if not base.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or not RUN_ID.match(path.name):
            continue
        document = path / "result.json"
        if not document.is_file():
            continue
        try:
            entries.append(_summary(path.name, json.loads(document.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, AttributeError):
            entries.append({"id": path.name, "status": "unreadable"})
    entries.sort(key=lambda entry: (entry.get("started_at") or "", entry["id"]), reverse=True)
    return entries[:MAX_RUNS]


def compare_runs(root: Path, first: str, second: str) -> dict[str, Any]:
    before = load_result(root, first)
    after = load_result(root, second)
    payload = compare(before, after)
    return {"a": _summary(first, before), "b": _summary(second, after), **payload}


__all__ = [
    "MAX_RUNS",
    "QUERY_TYPES",
    "RESULT_FILES",
    "RUN_ID",
    "RequestError",
    "RunRequest",
    "case_yaml",
    "compare_runs",
    "list_runs",
    "load_result",
    "parse_case",
    "parse_check",
    "parse_run",
    "suite_catalogue",
    "suites_payload",
]
