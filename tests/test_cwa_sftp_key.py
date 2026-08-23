"""CWA SFTP remote-ingest happy path, private-key auth (design doc CWA-S-01,
CWA-S-02). The key profile is the design doc's required remote default."""

from __future__ import annotations

import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab.fixtures import clean_epub

SUITE = suite("cwa-sftp-key", group="cwa-sftp-key", order=21)


def _run(ctx, test_id: str, operation: Callable[[], dict[str, object]]) -> None:
    try:
        detail = operation()
    except AssertionError as error:
        ctx.fail(test_id, str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail(test_id, f"Scenario failed unexpectedly: {error}")
    else:
        ctx.ok(test_id, "Scenario assertions passed.", detail)


@SUITE.case("CWA-S-01")
def trust_on_first_test(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-01") as scenario:
            if scenario.cwa_sftp_wiring is None:
                raise AssertionError("Scenario did not wire a CWA SFTP destination.")

            untrusted = scenario.cwa_sftp_wiring["untrusted_probe"]
            if not untrusted.get("requiresSftpHostKeyTrust") or not untrusted.get("sftpHostKeyFingerprint"):
                raise AssertionError(f"Expected an untrusted-host-key probe response, got {untrusted!r}.")
            if untrusted.get("succeeded"):
                raise AssertionError("The untrusted-host-key probe reported success; expected a rejection.")

            trusted = scenario.cwa_sftp_wiring["trusted_probe"]
            if not trusted.get("succeeded"):
                raise AssertionError(f"Expected the post-trust probe to succeed, got {trusted!r}.")

            return {"untrusted_probe": untrusted, "trusted_probe": trusted}

    _run(ctx, "CWA-S-01", operation)


@SUITE.case("CWA-S-02")
def remote_happy_path(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-02-KEY") as scenario:
            if scenario.cwa_client is None or scenario.cwa_sftp_wiring is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-02-key-the-hobbit.epub"
            )
            if uploaded.status != 200:
                raise AssertionError(f"Manual import returned HTTP {uploaded.status}: {uploaded.body!r}")

            library_import = _poll_library_import(scenario.api, request_id, timeout_seconds=90)
            if library_import is None:
                raise AssertionError("No LibraryImport record reached status Available for the request.")

            external_id = library_import.get("externalBookId")
            if not external_id:
                raise AssertionError("LibraryImport is Available but has no externalBookId.")

            opds_book_id = scenario.cwa_client.find_book("The Hobbit", "J. R. R. Tolkien")
            if opds_book_id is None:
                raise AssertionError("CWA's own OPDS catalog does not show the imported book.")

            return {
                "request_id": request_id,
                "library_import_id": library_import.get("id"),
                "external_book_id": external_id,
                "opds_book_id": opds_book_id,
            }

    _run(ctx, "CWA-S-02", operation)


def _poll_library_import(api, request_id: str, *, timeout_seconds: float) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        queue = api.publishing_queue()
        library_import = next(
            (item for item in queue.get("libraryImports", []) if item.get("requestId") == request_id), None
        )
        if library_import is not None and library_import.get("status") == "Available":
            return library_import
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)
