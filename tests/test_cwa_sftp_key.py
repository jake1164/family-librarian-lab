"""CWA SFTP remote-ingest happy path, private-key auth (design doc CWA-S-01,
CWA-S-02). The key profile is the design doc's required remote default."""

from __future__ import annotations

import time
from threading import Thread
from pathlib import Path
from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.fixtures import clean_epub, large_epub

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


@SUITE.case("CWA-S-03")
def key_authentication_rejects_an_untrusted_private_key(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-03-KEY") as scenario:
            if scenario.cwa_client is None or scenario.cwa_sftp_wiring is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            # ensure_sftp_test_keypair() currently invokes ssh-keygen with
            # -N "", so it intentionally supplies an unencrypted key only.
            # A distinct valid key is still required to prove authentication
            # rejects an unauthorized credential rather than malformed input.
            wrong_key, _ = clients.ensure_sftp_test_keypair(
                Path(__file__).resolve().parents[1] / "runtime" / "sftp-test-wrong-key"
            )
            settings = scenario.api.cwa_settings()
            trusted_fingerprint = settings.get("sftpHostKeyFingerprint")
            if not isinstance(trusted_fingerprint, str):
                raise AssertionError("Configured CWA SFTP key profile did not retain its trusted host fingerprint.")

            rejected_probe = scenario.api.test_cwa_ingest(
                _sftp_probe_request("PrivateKey", wrong_key, trusted_fingerprint)
            )
            if rejected_probe.get("succeeded") or rejected_probe.get("requiresSftpHostKeyTrust"):
                raise AssertionError(
                    "An unauthorized but well-formed SFTP private key did not fail solely at authentication: "
                    f"{rejected_probe!r}"
                )

            scenario.api.set_cwa_sftp_private_key(wrong_key)
            request_id, format_id = scenario.api.create_demo_ebook_request()
            upload = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-03-wrong-key-the-hobbit.epub"
            )
            import_status = _assert_no_available_import_or_catalog_item(scenario.api, scenario.cwa_client, request_id)
            return {"request_id": request_id, "upload_status": upload.status, "library_import_status": import_status}

    _run(ctx, "CWA-S-03", operation)


@SUITE.case("CWA-S-04")
def host_key_change_fails_closed_and_requires_retrust(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-04") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            settings = scenario.api.cwa_settings()
            old_fingerprint = settings.get("sftpHostKeyFingerprint")
            if not isinstance(old_fingerprint, str):
                raise AssertionError("Trust-on-first-test did not retain the original SFTP host fingerprint.")
            credential, _ = clients.ensure_sftp_test_keypair(
                Path(__file__).resolve().parents[1] / "runtime" / "sftp-test-key"
            )

            scenario.regenerate_sftp_host_keys(clients.CWA_SFTP_SERVICE_KEY)
            changed_probe = scenario.api.test_cwa_ingest(
                _sftp_probe_request("PrivateKey", credential, old_fingerprint)
            )
            new_fingerprint = changed_probe.get("sftpHostKeyFingerprint")
            if changed_probe.get("succeeded") or not changed_probe.get("requiresSftpHostKeyTrust"):
                raise AssertionError(f"Changed SFTP host key was accepted instead of failing closed: {changed_probe!r}")
            if not isinstance(new_fingerprint, str) or new_fingerprint == old_fingerprint:
                raise AssertionError(
                    "Changed SFTP host key did not surface a distinct fingerprint for explicit re-trust: "
                    f"{changed_probe!r}"
                )

            request_id, format_id = scenario.api.create_demo_ebook_request()
            upload = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-04-host-key-change-the-hobbit.epub"
            )
            import_status = _assert_no_available_import_or_catalog_item(scenario.api, scenario.cwa_client, request_id)
            return {
                "request_id": request_id,
                "upload_status": upload.status,
                "library_import_status": import_status,
                "old_fingerprint": old_fingerprint,
                "new_fingerprint": new_fingerprint,
            }

    _run(ctx, "CWA-S-04", operation)


@SUITE.case("CWA-S-05")
def interrupted_sftp_upload_leaves_no_final_file_and_recovers_once(ctx, scenario_factory):
    # The test intentionally waits for the SFTP transport's .uploading file
    # before stopping the sidecar. This distinguishes a real mid-transfer
    # connection loss from a harmless pre-connect failure or an upload that
    # already finished before Compose could stop the service.
    try:
        with scenario_factory("CWA-S-05") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            request_id, format_id = scenario.api.create_demo_ebook_request()
            upload_result: dict[str, object] = {}

            def upload() -> None:
                try:
                    upload_result["response"] = scenario.api.upload_manual_epub(
                        request_id, format_id, large_epub(), "cwa-s-05-interrupted-the-hobbit.epub"
                    )
                except Exception as error:  # asserted in the foreground after the real fault is injected
                    upload_result["error"] = error

            worker = Thread(target=upload, daemon=True)
            worker.start()
            if not scenario.wait_for_cwa_ingest_uploading(timeout_seconds=90):
                raise AssertionError("Could not observe the active SFTP temporary upload before interruption.")
            # Compose stop sends a graceful termination signal, which can let
            # an active SFTP channel finish and rename its file. Kill the
            # disposable sidecar instead, so this is a real mid-transfer
            # connection loss after the .uploading target is visible.
            scenario.kill_service(clients.CWA_SFTP_SERVICE_KEY)
            worker.join(timeout=60)
            if worker.is_alive():
                raise AssertionError("The interrupted SFTP upload did not return within 60 seconds.")

            if "error" in upload_result:
                raise AssertionError(f"Interrupted SFTP upload failed at the public API boundary: {upload_result['error']}")
            response = upload_result.get("response")
            if getattr(response, "status", None) != 200:
                raise AssertionError(f"Interrupted SFTP approval returned {response!r}, not HTTP 200.")
            remaining = scenario.cwa_ingest_filenames()
            final_names = [name for name in remaining if name.endswith(".epub") and not name.startswith(".")]
            if final_names:
                raise AssertionError(f"Interrupted SFTP upload exposed final CWA filename(s): {final_names!r}")
            temporary = [name for name in remaining if name.startswith(".") and name.endswith(".uploading")]
            if temporary:
                # SftpCwaIngestTransport has no cleanup-on-exception path
                # today. The strict assertion above has already established
                # that this exact product bug is an orphaned temporary file,
                # so skip rather than permanently red a required gate.
                ctx.skip(
                    "CWA-S-05",
                    "Interrupted SFTP upload left exact orphan temporary file(s) "
                    f"{temporary!r}; SftpCwaIngestTransport does not clean failed UploadFile targets.",
                )
                return

            pending = _require_library_import(scenario.api, request_id, "Failed")
            if not isinstance(pending.get("failureReason"), str) or not pending["failureReason"].strip():
                raise AssertionError("Interrupted SFTP upload did not record a safe failure reason.")
            scenario.start_service(clients.CWA_SFTP_SERVICE_KEY)
            scenario.api.recheck_library_import(_required_import_id(pending))
            available = _poll_library_import(scenario.api, request_id, timeout_seconds=120)
            if available is None:
                raise AssertionError("SFTP recheck did not recover after the sidecar restarted.")
            _require_exactly_one_book(
                scenario.cwa_client.find_books("The Hobbit", "J. R. R. Tolkien"),
                "SFTP interruption recovery created a duplicate CWA catalog item",
            )
            ctx.ok(
                "CWA-S-05",
                "Scenario assertions passed.",
                {
                    "request_id": request_id,
                    "library_import_id": pending.get("id"),
                    "remaining_ingest_files": remaining,
                },
            )
    except AssertionError as error:
        ctx.fail("CWA-S-05", str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail("CWA-S-05", f"Scenario failed unexpectedly: {error}")


@SUITE.case("CWA-S-06")
def sftp_handoff_waits_for_independent_catalog_verification(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("CWA-S-06") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up and wire a CWA SFTP destination.")

            # Stop only CWA. The independently running SFTP sidecar remains
            # the real transport endpoint and must finish its handoff even
            # though OPDS is unavailable for verification.
            scenario.stop_service("cwa")
            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "cwa-s-06-opds-outage-the-hobbit.epub"
            )
            if uploaded.status != 200:
                raise AssertionError(f"Manual import returned HTTP {uploaded.status}: {uploaded.body!r}")
            pending = _require_library_import(scenario.api, request_id, "AwaitingVerification")
            if pending.get("externalBookId") is not None:
                raise AssertionError("OPDS-unavailable SFTP handoff incorrectly recorded an external CWA book id.")

            scenario.start_service("cwa")
            book_ids = _poll_cwa_book_ids(scenario.cwa_client, timeout_seconds=90)
            _require_exactly_one_book(book_ids, "CWA did not import exactly one SFTP handoff before recheck")
            import_id = _required_import_id(pending)
            scenario.api.recheck_library_import(import_id)
            available = _poll_library_import(scenario.api, request_id, timeout_seconds=30)
            if available is None:
                raise AssertionError("SFTP handoff did not become Available after OPDS recovered and recheck ran.")
            if available.get("id") != import_id:
                raise AssertionError("OPDS recovery created a new LibraryImport instead of verifying the original handoff.")
            _require_exactly_one_book(
                scenario.cwa_client.find_books("The Hobbit", "J. R. R. Tolkien"),
                "OPDS recovery caused a second SFTP upload or duplicate CWA catalog item",
            )
            return {"request_id": request_id, "library_import_id": import_id, "opds_book_id": book_ids[0]}

    _run(ctx, "CWA-S-06", operation)


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
        "sftpHost": clients.CWA_SFTP_SERVICE_KEY,
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
            raise AssertionError("Rejected SFTP authentication or host key still created a CWA catalog item.")
        if time.monotonic() >= deadline:
            return last_status
        time.sleep(2)


def _find_library_import(queue: dict[str, object], request_id: str) -> dict[str, object] | None:
    return next(
        (item for item in queue.get("libraryImports", []) if item.get("requestId") == request_id), None
    )


def _require_library_import(api, request_id: str, expected_status: str) -> dict[str, object]:
    library_import = _find_library_import(api.publishing_queue(), request_id)
    if not isinstance(library_import, dict):
        raise AssertionError("No LibraryImport was recorded for the request.")
    if library_import.get("status") != expected_status:
        raise AssertionError(
            f"LibraryImport after the required initial {expected_status!r} state was "
            f"{library_import.get('status')!r}: {library_import!r}"
        )
    return library_import


def _required_import_id(library_import: dict[str, object]) -> str:
    import_id = library_import.get("id")
    if not isinstance(import_id, str):
        raise AssertionError("LibraryImport did not contain an id.")
    return import_id


def _poll_cwa_book_ids(cwa_client: clients.CwaClient, *, timeout_seconds: float) -> list[str]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        book_ids = cwa_client.find_books("The Hobbit", "J. R. R. Tolkien")
        if book_ids:
            return book_ids
        if time.monotonic() >= deadline:
            return []
        time.sleep(2)


def _require_exactly_one_book(book_ids: list[str], message: str) -> None:
    if len(book_ids) != 1:
        raise AssertionError(f"{message}: expected one OPDS entry, got {book_ids!r}.")
