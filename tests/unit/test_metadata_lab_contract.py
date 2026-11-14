"""The contract every adapter must satisfy, including future ones.

Parameterised over ``registry.discover()``: a new adapter file is validated
automatically. The dynamic half runs every implemented operation through the
engine against an offline transport, so a crash or a malformed record type fails
here even if the adapter forgot its own test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metadata_lab import registry
from metadata_lab.cases import Case, Reference, Suite
from metadata_lab.engine import run_suite
from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import FORMATS, Operation, Outcome, RECORD_LEVELS, WORK_TYPES

ADAPTERS = registry.discover()
PROVIDERS = sorted(ADAPTERS)
ALL_OPERATIONS = tuple(Operation)
SOURCE_DIR = Path(__file__).resolve().parents[2] / "metadata_lab" / "providers"
OUTCOME_VALUES = {outcome.value for outcome in Outcome}


class EmptyTransport:
    """Answers every request with 200 and an empty JSON object, offline."""

    def send(self, method, url, *, params, headers, body, timeout):
        return TransportResponse(200, {}, "{}")


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_adapter_metadata_is_complete(provider_id: str):
    adapter_cls = ADAPTERS[provider_id]
    adapter = adapter_cls()

    assert adapter.id == provider_id
    assert provider_id == provider_id.lower()
    assert adapter.display_name
    assert adapter.adapter_version


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_every_unsupported_entry_is_cited(provider_id: str):
    adapter = ADAPTERS[provider_id]()

    for operation, declaration in adapter.unsupported.items():
        assert isinstance(operation, Operation)
        assert declaration.reason and declaration.reason.strip()
        assert declaration.source_url.startswith(("http://", "https://"))


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_declared_support_is_consistent(provider_id: str):
    adapter = ADAPTERS[provider_id]()

    for operation in ALL_OPERATIONS:
        declared = adapter.declared_support(operation)
        assert declared in {"supported", "unsupported", "not_implemented"}
        if declared == "unsupported":
            assert operation in adapter.unsupported
            assert adapter.implements(operation) is False
        elif declared == "supported":
            assert adapter.implements(operation) is True
            assert operation not in adapter.unsupported
        else:
            assert adapter.implements(operation) is False
            assert operation not in adapter.unsupported


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_adapter_never_imports_requests_or_lab_packages(provider_id: str):
    source = (SOURCE_DIR / f"{provider_id}.py").read_text(encoding="utf-8")

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import requests", "from requests")):
            pytest.fail(f"{provider_id}.py imports requests directly")
        if stripped.startswith(("import agent", "from agent")):
            pytest.fail(f"{provider_id}.py imports the se-lab agent package")
        if stripped.startswith(("import family_librarian_lab", "from family_librarian_lab")):
            pytest.fail(f"{provider_id}.py imports the product package")


@pytest.mark.parametrize("provider_id", PROVIDERS)
def test_every_implemented_operation_runs_through_the_engine(
    provider_id: str, monkeypatch: pytest.MonkeyPatch
):
    adapter_cls = ADAPTERS[provider_id]
    planted = f"selftest-credential-{provider_id}"
    credential_name = getattr(adapter_cls, "credential_env", None)
    if credential_name:
        # A clearly fake value: the assertion below is that it never leaks.
        monkeypatch.setenv(credential_name, planted)

    case = Case(
        id="contract",
        description="contract case",
        reference=Reference(source="contract test"),
        input={
            "title": "Fourth Wing",
            "author": "Rebecca Yarros",
            "isbn": "9781649374042",
            "series": "The Empyrean",
        },
        operations=ALL_OPERATIONS,
        traversal=False,
        options={},
        expect={},
        verified=False,
    )
    suite = Suite(name="contract", version=1, cases=(case,), source="memory", sha256="0" * 64)
    document = run_suite(
        suite,
        [provider_id],
        None,
        http_factory=lambda pid: HttpClient(
            provider_id=pid, transport=EmptyTransport(), min_interval_s=0.0
        ),
        adapters=ADAPTERS,
        resolve_configured=lambda _cls: True,
    )

    rows = document["cases"][0]["results"]
    assert len(rows) == len(ALL_OPERATIONS)
    for row in rows:
        assert row["outcome"] in OUTCOME_VALUES
        assert isinstance(row["pagination"], dict)
        assert isinstance(row["duplicates"], list)
        assert isinstance(row["stats"], dict)
        for record in row["records"]:
            _assert_record_vocabulary(record)

    assert planted not in json.dumps(document)


def _assert_record_vocabulary(record: dict) -> None:
    kind = record.get("record_type")
    assert kind in {"book", "series", "author", "series_ref", "series_member"}
    if kind == "book":
        assert record["work_type"] in WORK_TYPES
        assert record["format"] in FORMATS
        assert record["record_level"] in RECORD_LEVELS
    if kind == "series_member":
        assert record["work_type"] in WORK_TYPES
        assert record["position"] is None or isinstance(record["position"], str)
