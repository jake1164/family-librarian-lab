"""CWA local/shared-ingest happy-path scenario (design doc CWA-L-02)."""

from __future__ import annotations

import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab.fixtures import clean_epub
from family_librarian_lab import clients

SUITE = suite("cwa-local", group="cwa-local", order=20)


def _run(ctx, test_id: str, operation: Callable[[], dict[str, object]]) -> None:
    try:
        detail = operation()
    except AssertionError as error:
        ctx.fail(test_id, str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail(test_id, f"Scenario failed unexpectedly: {error}")
    else:
        ctx.ok(test_id, "Scenario assertions passed.", detail)


@SUITE.case("CWA-L-01")
def configuration_probes_are_independent(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-L-01") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up a CWA destination.")

            ingest = scenario.api.test_cwa_ingest(
                {
                    "transportMode": "Local",
                    "localIngestPath": clients.CWA_INGEST_CONTAINER_PATH,
                    "sftpHost": None,
                    "sftpPort": None,
                    "sftpUsername": None,
                    "sftpIngestPath": None,
                    "sftpAuthenticationMode": "PrivateKey",
                    "sftpPrivateKey": None,
                    "sftpPassphrase": None,
                    "sftpPassword": None,
                    "trustedSftpHostKeyFingerprint": None,
                }
            )
            opds = scenario.api.test_cwa_opds(
                {
                    "opdsBaseUrl": clients.CWA_INTERNAL_URL,
                    "opdsUsername": clients.CWA_DEFAULT_USERNAME,
                    "opdsPassword": clients.CWA_DEFAULT_PASSWORD,
                }
            )
            if not ingest.get("succeeded"):
                raise AssertionError(f"CWA local-ingest probe did not succeed: {ingest!r}")
            if not opds.get("succeeded"):
                raise AssertionError(f"CWA OPDS probe did not succeed: {opds!r}")
            return {"ingest_probe": ingest, "opds_probe": opds}

    _run(ctx, "CWA-L-01", operation)


@SUITE.case("CWA-L-02")
def clean_ebook_end_to_end(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-L-02") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up a CWA destination.")

            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-l-02-the-hobbit.epub"
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

    _run(ctx, "CWA-L-02", operation)


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
