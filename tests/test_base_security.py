"""Base-profile deployment and real ClamAV security-gate scenarios."""

from __future__ import annotations

from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab.fixtures import clean_epub, eicar_epub, identity_mismatched_epub, invalid_epub


SUITE = suite("base-security", group="base", order=10)


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
