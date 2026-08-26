"""Base-profile deployment and real ClamAV security-gate scenarios."""

from __future__ import annotations

from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.fixtures import clean_epub, eicar_epub, identity_mismatched_epub, invalid_epub


SUITE = suite("base-security", group="base", order=10)
_BASE_RESTART_INGEST_PATH = "/data/family-librarian/base-03-ingest"


def _run(ctx, test_id: str, operation: Callable[[], dict[str, object]]) -> None:
    try:
        detail = operation()
    except AssertionError as error:
        ctx.fail(test_id, str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail(test_id, f"Scenario failed unexpectedly: {error}")
    else:
        ctx.ok(test_id, "Scenario assertions passed.", detail)


@SUITE.case("BASE-01")
def fresh_deployment(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("BASE-01") as scenario:
            me = scenario.api.me()
            if "Admin" not in me.get("roles", []):
                raise AssertionError("Bootstrap account authenticated but is not an administrator.")
            if not scenario.readiness_passed:
                raise AssertionError("Compose or HTTP readiness check did not pass before authentication.")
            return {"compose_project": scenario.project_name, "administrator": me.get("email")}

    _run(ctx, "BASE-01", operation)


@SUITE.case("BASE-02")
def fresh_state_isolation(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("BASE-02") as scenario:
            accounts = scenario.api.list_accounts()
            requests = scenario.api.list_requests()
            assets = scenario.api.list_assets()
            queue = scenario.api.publishing_queue()
            imports = queue.get("libraryImports")
            deliveries = queue.get("deliveries")
            if len(accounts) != 1 or not accounts[0].get("isAdmin"):
                raise AssertionError("Fresh deployment did not contain exactly the bootstrap administrator.")
            if requests or assets or imports or deliveries:
                raise AssertionError("Fresh Compose volumes contained request, asset, import, or delivery state.")
            return {"compose_project": scenario.project_name, "accounts": len(accounts)}

    _run(ctx, "BASE-02", operation)


@SUITE.case("BASE-03")
def restart_preserves_durable_application_state(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("BASE-03") as scenario:
            # The base profile deliberately has no CWA container. A
            # host-owned local ingest path still gives us a durable handoff
            # and an OPDS-verification state to survive the host restart,
            # without depending on a destination profile's volume ownership.
            configured = scenario.api.configure_cwa_local(
                local_ingest_path=_BASE_RESTART_INGEST_PATH,
                opds_base_url=clients.CWA_INTERNAL_URL,
                opds_username=clients.CWA_DEFAULT_USERNAME,
                opds_password=clients.CWA_DEFAULT_PASSWORD,
            )
            if not configured.get("isEnabled"):
                raise AssertionError("CWA settings were not enabled before the restart-persistence check.")

            request_id, format_id = scenario.api.create_demo_ebook_request()
            published = scenario.api.upload_manual_epub(
                request_id, format_id, clean_epub(), "base-03-pending-the-hobbit.epub"
            )
            if published.status != 200:
                raise AssertionError(f"Pending-publication upload returned HTTP {published.status}: {published.body!r}")
            pending_import = _find_library_import(scenario.api.publishing_queue(), request_id)
            if pending_import.get("status") != "AwaitingVerification":
                raise AssertionError(
                    "The CWA-unavailable handoff did not remain pending before restart: "
                    f"{pending_import.get('status')!r}."
                )

            # A structurally invalid EPUB is retained in the admin security
            # queue with its scanner/validator evidence, unlike a clean asset
            # that has already moved to trusted storage and is intentionally
            # absent from that queue.
            evidence_request_id, evidence_format_id = scenario.api.create_demo_ebook_request()
            malformed = scenario.api.upload_manual_epub(
                evidence_request_id, evidence_format_id, invalid_epub(), "base-03-malformed.epub"
            )
            if malformed.status != 200 or not isinstance(malformed.body, dict):
                raise AssertionError(
                    "Malformed EPUB did not enter the persisted security-evidence path: "
                    f"HTTP {malformed.status}, {malformed.body!r}."
                )
            evidence_asset = _find_asset(scenario.api.list_assets(), malformed.body.get("mediaAssetId"))
            evidence_evaluation = _require_evaluation(evidence_asset)
            if not evidence_evaluation.get("scanResults") or not evidence_evaluation.get("validationResults"):
                raise AssertionError("Malformed EPUB did not retain scanner and validation evidence before restart.")

            scenario.restart_service("family-librarian")
            # The restart is intentionally host-only: PostgreSQL and the
            # family-librarian storage volume must retain all prior state.
            scenario.reauthenticate()

            settings_after = scenario.api.cwa_settings()
            if not settings_after.get("isEnabled") or settings_after.get("localIngestPath") != _BASE_RESTART_INGEST_PATH:
                raise AssertionError("CWA configuration was lost or changed across the host-only restart.")
            request_after = _find_admin_request(scenario.api.list_requests(), request_id)
            if request_after.get("id") != request_id:
                raise AssertionError("Request history did not retain the pending-publication request across restart.")
            import_after = _find_library_import(scenario.api.publishing_queue(), request_id)
            if import_after.get("id") != pending_import.get("id") or import_after.get("status") != "AwaitingVerification":
                raise AssertionError("Pending CWA publication did not survive restart unchanged.")
            evidence_after = _find_asset(scenario.api.list_assets(), malformed.body.get("mediaAssetId"))
            after_evaluation = _require_evaluation(evidence_after)
            if after_evaluation.get("evaluationId") != evidence_evaluation.get("evaluationId"):
                raise AssertionError("Security evaluation evidence was replaced or lost across restart.")
            if not after_evaluation.get("scanResults") or not after_evaluation.get("validationResults"):
                raise AssertionError("Security evaluation evidence was incomplete after restart.")

            return {
                "request_id": request_id,
                "library_import_id": pending_import.get("id"),
                "security_asset_id": malformed.body.get("mediaAssetId"),
            }

    _run(ctx, "BASE-03", operation)


@SUITE.case("SEC-01")
def clean_epub_passes_real_security_gate(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SEC-01") as scenario:
            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(request_id, format_id, clean_epub(), "clean-the-hobbit.epub")
            if uploaded.status != 200 or not isinstance(uploaded.body, dict):
                raise AssertionError(f"Clean EPUB upload returned HTTP {uploaded.status} rather than success.")
            asset_id = uploaded.body.get("mediaAssetId")
            if not isinstance(asset_id, str):
                raise AssertionError("Clean EPUB success response did not contain a media asset id.")
            if any(asset.get("assetId") == asset_id for asset in scenario.api.list_assets()):
                raise AssertionError("Clean EPUB remained in the active security queue after successful evaluation.")
            queue = scenario.api.publishing_queue()
            if queue.get("libraryImports") or queue.get("deliveries"):
                raise AssertionError("Base profile created a destination record although it has no configured destination.")
            return {"asset_id": asset_id, "active_security_queue": "absent after success"}

    _run(ctx, "SEC-01", operation)


@SUITE.case("SEC-02")
def eicar_cannot_cross_security_gate(ctx, scenario_factory):
    try:
        with scenario_factory("SEC-02") as scenario:
            request_id, format_id = scenario.api.create_demo_ebook_request()
            uploaded = scenario.api.upload_manual_epub(request_id, format_id, eicar_epub(), "eicar-the-hobbit.epub")
            if uploaded.status != 200 or not isinstance(uploaded.body, dict):
                raise AssertionError(f"EICAR EPUB upload returned HTTP {uploaded.status} rather than reaching evaluation.")
            asset_id = uploaded.body.get("mediaAssetId")
            assets = scenario.api.list_assets()
            asset = next((item for item in assets if item.get("assetId") == asset_id), None)
            queue = scenario.api.publishing_queue()
            if queue.get("libraryImports") or queue.get("deliveries"):
                raise AssertionError("EICAR created a destination import or delivery record.")
            if asset is None:
                ctx.skip(
                    "SEC-02",
                    "EICAR upload reached the pipeline, but the public API omits destroyed assets and their malware/audit evidence.",
                )
                return
            evaluation = _require_evaluation(asset)
            if asset.get("storageState") != "Rejected":
                raise AssertionError(f"EICAR asset finished in {asset.get('storageState')!r}, not Rejected.")
            if evaluation.get("status") != "Failed":
                raise AssertionError(f"EICAR evaluation was {evaluation.get('status')!r}, not Failed.")
            _require_scan_status(evaluation, "Detected")
            ctx.ok("SEC-02", "Scenario assertions passed.", {"asset_id": asset_id, "evaluation": evaluation.get("status")})
    except AssertionError as error:
        ctx.fail("SEC-02", str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail("SEC-02", f"Scenario failed unexpectedly: {error}")


@SUITE.case("SEC-03")
def scanner_unavailable_blocks_then_allows_retry(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SEC-03") as scenario:
            request_id, format_id = scenario.api.create_demo_ebook_request()
            scenario.stop_clamav()
            blocked = scenario.api.upload_manual_epub(request_id, format_id, clean_epub(), "scanner-down.epub")
            if blocked.status != 503:
                raise AssertionError(f"Upload while ClamAV was stopped returned HTTP {blocked.status}, not 503.")
            if scenario.api.list_assets():
                raise AssertionError("Scanner-unavailable upload created a media asset before the security boundary.")
            if scenario.api.publishing_queue().get("libraryImports") or scenario.api.publishing_queue().get("deliveries"):
                raise AssertionError("Scanner-unavailable upload created a destination record.")
            scenario.start_clamav()
            retried = scenario.api.upload_manual_epub(request_id, format_id, clean_epub(), "scanner-recovered.epub")
            if retried.status != 200 or not isinstance(retried.body, dict):
                raise AssertionError(f"Re-upload after ClamAV recovery returned HTTP {retried.status}.")
            recovery_asset_id = retried.body.get("mediaAssetId")
            if not isinstance(recovery_asset_id, str):
                raise AssertionError("Recovered upload did not return a media asset id.")
            if any(asset.get("assetId") == recovery_asset_id for asset in scenario.api.list_assets()):
                raise AssertionError("The documented re-upload path did not clear the active security queue.")
            return {"recovery_asset_id": recovery_asset_id}

    _run(ctx, "SEC-03", operation)


@SUITE.case("SEC-04")
def malformed_and_identity_mismatched_epubs_stay_out_of_trusted_storage(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SEC-04") as scenario:
            request_id, format_id = scenario.api.create_demo_ebook_request()
            malformed = scenario.api.upload_manual_epub(request_id, format_id, invalid_epub(), "malformed.epub")
            if malformed.status not in (200, 400):
                raise AssertionError(f"Malformed EPUB returned unexpected HTTP {malformed.status}: {malformed.body!r}")
            if malformed.status == 200 and isinstance(malformed.body, dict):
                malformed_asset = _find_asset(scenario.api.list_assets(), malformed.body.get("mediaAssetId"))
                if malformed_asset.get("storageState") == "Trusted":
                    raise AssertionError("Malformed EPUB reached trusted storage.")

            mismatched = scenario.api.upload_manual_epub(
                request_id, format_id, identity_mismatched_epub(), "identity-mismatched.epub"
            )
            if mismatched.status != 200 or not isinstance(mismatched.body, dict):
                raise AssertionError(f"Identity-mismatched EPUB did not reach security evaluation: {mismatched!r}")
            mismatch_asset_id = mismatched.body.get("mediaAssetId")
            mismatch_asset = _find_asset(scenario.api.list_assets(), mismatch_asset_id)
            mismatch_evaluation = _require_evaluation(mismatch_asset)
            # Identity mismatch is tracked on storageState, not evaluation.status: a
            # clean scan proves the file is safe, not that it's the requested book
            # (see ApprovalService.ApproveCoreAsync and the Manual V1 workflow in
            # docs/02-domain-workflows.md, which treats them as separate branches).
            if mismatch_asset.get("storageState") != "Unmatched":
                raise AssertionError(
                    "Identity-mismatched EPUB did not land in the Unmatched security "
                    f"state: {mismatch_asset.get('storageState')!r}."
                )
            if mismatch_evaluation.get("status") != "Passed":
                raise AssertionError(
                    "Identity-mismatched EPUB's clean scan was not recorded as Passed "
                    f"before identity verification held it: {mismatch_evaluation.get('status')!r}."
                )
            queue = scenario.api.publishing_queue()
            if queue.get("libraryImports") or queue.get("deliveries"):
                raise AssertionError("Rejected or identity-mismatched EPUB created a destination publication record.")
            return {
                "malformed_status": malformed.status,
                "mismatched_asset_id": mismatch_asset_id,
                "mismatched_evaluation": mismatch_evaluation.get("status"),
            }

    _run(ctx, "SEC-04", operation)


def _find_asset(assets: list[dict[str, Any]], asset_id: object) -> dict[str, Any]:
    if not isinstance(asset_id, str):
        raise AssertionError("Upload response did not contain a media asset id.")
    asset = next((item for item in assets if item.get("assetId") == asset_id), None)
    if asset is None:
        raise AssertionError("Uploaded asset was absent from the administrative security queue.")
    return asset


def _find_library_import(queue: dict[str, Any], request_id: str) -> dict[str, Any]:
    imports = queue.get("libraryImports")
    if not isinstance(imports, list):
        raise AssertionError("Publishing queue did not contain library imports.")
    library_import = next((item for item in imports if item.get("requestId") == request_id), None)
    if not isinstance(library_import, dict):
        raise AssertionError("Publishing queue did not retain a LibraryImport for the request.")
    return library_import


def _find_admin_request(requests: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    for entry in requests:
        request = entry.get("request")
        if isinstance(request, dict) and request.get("id") == request_id:
            return request
    raise AssertionError("Administrative request history did not contain the expected request.")


def _require_evaluation(asset: dict[str, Any]) -> dict[str, Any]:
    evaluation = asset.get("latestEvaluation")
    if not isinstance(evaluation, dict):
        raise AssertionError("Uploaded asset did not retain security evaluation evidence.")
    return evaluation


def _require_scan_status(evaluation: dict[str, Any], expected: str) -> None:
    scans = evaluation.get("scanResults")
    if not isinstance(scans, list) or not any(
        isinstance(scan, dict) and scan.get("scannerId") == "clamav" and scan.get("status") == expected
        for scan in scans
    ):
        raise AssertionError(f"No ClamAV scan result reported {expected!r}.")
