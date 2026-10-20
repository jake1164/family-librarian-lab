"""Exercise EXTPROV-01's destination assertion through the real CWA OPDS reader."""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _feed(*book_ids):
    entries = "".join(
        f'<entry><title>Pride and Prejudice</title><author><name>Jane Austen</name></author>'
        f'<link href="/opds/download/{book_id}/epub/" /></entry>' for book_id in book_ids)
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()


@pytest.fixture
def run_case(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "se-lab"))
    spec = importlib.util.spec_from_file_location("external_provider_suite_under_test", root / "tests/test_external_provider.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", sleep)

    def run(responses, *, imports=None):
        calls = []
        outcomes = []
        pending = iter(responses)
        last = responses[-1]

        def http(url, **kwargs):
            calls.append(url)
            assert url == "http://cwa.example.test/opds/search/Pride%20and%20Prejudice"
            return next(pending, last)

        monkeypatch.setattr(module.clients, "_http", http)
        api = SimpleNamespace(
            create_external_provider=lambda *args: {"id": "provider-row"},
            test_external_provider=lambda *args: {"lastTestSucceeded": True},
            set_external_provider_enabled=lambda *args: SimpleNamespace(status=200),
            create_demo_ebook_request=lambda **kwargs: ("request-1", "format-1"),
            resolve_demo_work=lambda *args: "work-1",
            fulfillment_options=lambda *args: {"ebook": [{
                "providerId": "sample-provider", "providerResultId": "result-1", "matchBasis": "StrictTitleAuthor"}]},
            acquire_direct=lambda *args: SimpleNamespace(status=202, body={"providerAcquisitionJobId": "job-1"}),
            admin_request=lambda *args: {"request": {"status": "Available"}},
            provider_attempts=lambda *args: [{"outcome": "Submitted"}],
            publishing_queue=lambda: {"libraryImports": imports if imports is not None else [
                {"requestId": "request-1", "status": "Available"}]},
        )

        @contextmanager
        def factory(test_id):
            assert test_id == "EXTPROV-01"
            yield SimpleNamespace(api=api, cwa_client=module.clients.CwaClient("http://cwa.example.test"))

        ctx = SimpleNamespace(
            ok=lambda test_id, message, detail: outcomes.append(("pass", detail)),
            fail=lambda test_id, message: outcomes.append(("fail", message)),
        )
        module.happy_path_acquisition_lands_in_cwa(ctx, factory)
        return outcomes, calls, sleeps, clock[0]

    return run


def test_transient_cwa_closed_database_response_does_not_erase_verified_delivery(run_case):
    outcomes, calls, sleeps, _ = run_case([
        (500, b"Cannot operate on a closed database."), (200, _feed("2"))])
    assert outcomes[0][0] == "pass", outcomes
    assert outcomes[0][1]["opds_books"] == ["2"]
    assert len(calls) == 2
    assert sleeps == [1]


def test_healthy_catalog_is_checked_once_without_delay(run_case):
    outcomes, calls, sleeps, _ = run_case([(200, _feed("2"))])
    assert outcomes[0][0] == "pass", outcomes
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("recovering", [False, True])
def test_duplicate_books_fail_without_retrying_them_away(run_case, recovering):
    responses = ([(500, b"temporary")] if recovering else []) + [(200, _feed("2", "3")), (200, _feed("2"))]
    outcomes, calls, _, _ = run_case(responses)
    assert outcomes[0][0] == "fail"
    assert "exactly one acquired copy, found 2" in outcomes[0][1]
    assert len(calls) == (2 if recovering else 1)


@pytest.mark.parametrize("response", [(500, b"still unavailable"), (200, _feed())])
def test_persistent_catalog_failure_or_absent_book_still_fails_with_bounded_wait(run_case, response):
    outcomes, calls, _, elapsed = run_case([response])
    assert outcomes[0][0] == "fail"
    assert "exactly one acquired copy, found 0" in outcomes[0][1]
    assert len(calls) <= 11
    assert elapsed <= 10


@pytest.mark.parametrize("imports", [[], [
    {"requestId": "request-1", "status": "Available"},
    {"requestId": "request-1", "status": "Available"}], [
    {"requestId": "request-1", "status": "Pending"}]])
def test_catalog_success_cannot_bypass_the_single_verified_import_assertion(run_case, imports):
    outcomes, _, _, _ = run_case([(200, _feed("2"))], imports=imports)
    assert outcomes[0][0] == "fail"
    assert "one verified import" in outcomes[0][1]
