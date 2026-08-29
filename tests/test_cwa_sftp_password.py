"""CWA SFTP remote-ingest happy path, password auth (design doc CWA-S-02,
run again under the password profile) -- proves the explicitly supported
alternative authentication mode reaches CWA too, not just the key default."""

from __future__ import annotations

import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.fixtures import clean_epub

SUITE = suite("cwa-sftp-password", group="cwa-sftp-password", order=22)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _run(ctx, test_id: str, operation: Callable[[], dict[str, object]]) -> None:
    try:
        detail = operation()
    except AssertionError as error:
        ctx.fail(test_id, str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail(test_id, f"Scenario failed unexpectedly: {error}")
    else:
        ctx.ok(test_id, "Scenario assertions passed.", detail)


@SUITE.case("CWA-S-02")
def remote_happy_path(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-02-PASSWORD") as scenario:
            if scenario.cwa_client is None or scenario.cwa_sftp_wiring is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-02-password-the-hobbit.epub"
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


@SUITE.case("CWA-S-03")
def password_authentication_rejects_wrong_password(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-03-PASSWORD") as scenario:
            if scenario.cwa_client is None or scenario.cwa_sftp_wiring is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            settings = scenario.api.cwa_settings()
            trusted_fingerprint = settings.get("sftpHostKeyFingerprint")
            if not isinstance(trusted_fingerprint, str):
                raise AssertionError("Configured CWA SFTP password profile did not retain its trusted host fingerprint.")
            wrong_password = "family-librarian-lab-wrong-sftp-password"
            rejected_probe = scenario.api.test_cwa_ingest(
                _sftp_probe_request("Password", wrong_password, trusted_fingerprint)
            )
            if rejected_probe.get("succeeded") or rejected_probe.get("requiresSftpHostKeyTrust"):
                raise AssertionError(
                    "An incorrect SFTP password did not fail solely at authentication: "
                    f"{rejected_probe!r}"
                )

            # Create the request while CWA is still ready, *then* break the
            # credential. Product commit acb1ff7 (FormatReadinessService)
            # made any settings/secret mutation reset LastTestSucceeded,
            # which now makes CWA "not ready" until its next successful
            # test -- CwaSettingsService.GetRequestReadinessErrorAsync calls
            # the same GetConfigurationError() SetEnabledAsync uses, and
            # BookRequestService.CreateRequestAsync rejects a new request
            # with 400 while not ready. Calling set_cwa_sftp_password()
            # before create_demo_ebook_request() (the original order) trips
            # that gate before the transport layer this case actually tests
            # ever runs -- confirmed for real, reproducible 100% of the time,
            # not a flake and not a product bug: the gate is doing exactly
            # what it's supposed to. This case's own intent (a credential
            # that breaks *after* a request already exists must still fail
            # safely at upload, not silently succeed) is untouched by the
            # reorder -- if anything it's a more realistic timeline.
            request_id, format_id = scenario.api.create_demo_ebook_request()
            scenario.api.set_cwa_sftp_password(wrong_password)
            upload = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-03-wrong-password-the-hobbit.epub"
            )
            import_status = _assert_no_available_import_or_catalog_item(scenario.api, scenario.cwa_client, request_id)
            return {"request_id": request_id, "upload_status": upload.status, "library_import_status": import_status}

    _run(ctx, "CWA-S-03", operation)


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


def _sftp_probe_request(auth_mode: str, credential: str, trusted_fingerprint: str) -> dict[str, object]:
    return {
        "transportMode": "Sftp",
        "localIngestPath": None,
        "sftpHost": clients.CWA_SFTP_SERVICE_PASSWORD,
        "sftpPort": clients.CWA_SFTP_PORT,
        "sftpUsername": clients.CWA_SFTP_USERNAME,
        "sftpIngestPath": clients.CWA_SFTP_INGEST_PATH,
        "sftpAuthenticationMode": auth_mode,
        "sftpPrivateKey": credential if auth_mode == "PrivateKey" else None,
        "sftpPassphrase": None,
        "sftpPassword": credential if auth_mode == "Password" else None,
        "trustedSftpHostKeyFingerprint": trusted_fingerprint,
    }


def _assert_no_available_import_or_catalog_item(api, cwa_client, request_id: str) -> str | None:
    deadline = time.monotonic() + 20
    last_status: str | None = None
    while True:
        queue = api.publishing_queue()
        library_import = next(
            (item for item in queue.get("libraryImports", []) if item.get("requestId") == request_id), None
        )
        if isinstance(library_import, dict):
            last_status = library_import.get("status")
            if last_status == "Available":
                raise AssertionError("Rejected SFTP authentication created an Available LibraryImport.")
        if cwa_client.find_books("The Hobbit", "J. R. R. Tolkien"):
            raise AssertionError("Rejected SFTP authentication created a CWA catalog item.")
        if time.monotonic() >= deadline:
            return last_status
        time.sleep(2)
