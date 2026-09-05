"""Base-profile host-restart persistence, exercised against a real (then-
stopped) CWA destination.

This used to live in test_base_security.py as BASE-03, configured through a
hand-rolled `configure_cwa_local()` call against `cwa:8083` with no `cwa`
container ever brought up. That can never work:
`CwaPublishingService.PublishAsync` no-ops entirely when CWA isn't enabled,
and enabling requires a real, currently-passing OPDS test (see CWA-L-08's
enablement invariant, docs/01 SS12.1.1) -- there is no way to reach the
AwaitingVerification state this case asserts on without a CWA container that
was, at some point, actually reachable.

This suite therefore declares `extra_profiles=(clients.CWA_PROFILE,)` so its
own scenario factory brings up a real `cwa` container and auto-wires it
(`_wire_destinations()`), then immediately stops it -- the same
enable-then-stop pattern CWA-L-03/CWA-L-05 already use -- before doing
anything that needs CWA to be durably *unavailable*. `group="base"` is kept
so this still runs in the default `--test-group base` sweep; `extra_profiles`
is what actually gets `cwa` running, independent of that group.

It's a separate suite from base-security (which now also declares
`extra_profiles=(clients.CWA_PROFILE,)`, for an unrelated reason -- see that
module's own docstring) rather than a case there because the two need CWA in
opposite states: base-security needs it left running, this needs it stopped.
`handle_run()`'s `_scoped_for_run()` points the shared scenario factory at
each suite's own required profile(s) only while that specific suite's own
setup/case/teardown is running, which is what makes sharing one run (and one
dashboard) across suites needing different destination states safe --
sharing one *factory pointed at a single fixed profile set for the whole
run* is what broke SEC-01/SEC-02 once before, back when base-security had no
destination requirement of its own at all.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.fixtures import clean_epub, invalid_epub

SUITE = suite("base-restart", group="base", order=11, extra_profiles=(clients.CWA_PROFILE,))


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


@SUITE.case("BASE-03")
def restart_preserves_durable_application_state(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("BASE-03") as scenario:
            if scenario.cwa_client is None:
                raise AssertionError("Scenario did not bring up and enable a CWA destination.")

            # CWA is enabled (via the scenario's normal auto-wiring) but
            # stopped before anything is uploaded: the host can still
            # complete the local atomic handoff, but its immediate OPDS
            # lookup must miss, giving us a durable AwaitingVerification
            # record to check across the restart below -- the same
            # enable-then-stop pattern CWA-L-03/CWA-L-05 use.
            scenario.stop_service("cwa")

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
            evidence_request_id, evidence_format_id = scenario.api.create_demo_ebook_request(slug="project-hail-mary")
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
            if not settings_after.get("isEnabled") or settings_after.get("localIngestPath") != clients.CWA_INGEST_CONTAINER_PATH:
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
