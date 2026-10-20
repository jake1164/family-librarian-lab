"""Phase 8b: real FL release policy/gateway/approval/storage/delivery with invented HTTP fixture content."""
from __future__ import annotations

import time
import hashlib

from agent.suites import suite
from family_librarian_lab import clients, question_cases as q
from family_librarian_lab.commands import FL_MODEL_ROLES, ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.fixtures import artifact_release_epub

SUITE = suite("artifact-release", group="artifact-release", order=40,
              extra_profiles=(clients.SEARCH_REVIEW_PROFILE, clients.CWA_PROFILE, clients.ABS_PROFILE))
ROLE = "ArtifactValidationDecision"
CASES = (
    ("AR-01", "author", 1, True, "Succeeded"),
    ("AR-02", "off", 0, False, None),
    ("AR-03", "inconclusive", 1, True, "Succeeded"),
    ("AR-04", "title", 0, False, None),
    ("AR-05", "language", 0, False, None),
    ("AR-06", "concern", 1, False, "Succeeded"),
    ("AR-07", "forged-id", 1, False, "SchemaRejected"),
    ("AR-08", "unknown", 1, False, "SchemaRejected"),
    ("AR-09", "other-dimension", 1, False, "SchemaRejected"),
    ("AR-10", "invalid-json", 1, False, "InvalidJson"),
    ("AR-11", "transport", 1, False, "failure"),
    ("AR-12", "role-off", 0, False, None),
    ("AR-13", "non-admin", 0, False, None),
)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _recent(api):
    response = api._request("GET", "/api/v1/admin/media-assets/recent?limit=100")
    assert response.status == 200, response
    return response.body["assets"]


def _held(api, request_id):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        assets = _recent(api)
        assert not any(a.get("storageState") == "Trusted" for a in assets), assets
        if any(a.get("storageState") == "Unmatched" for a in assets):
            return next(a for a in assets if a.get("storageState") == "Unmatched")
        time.sleep(1)
    raise AssertionError(f"No held asset: {q.describe(api, request_id)}")


def _exercise(scenario, mode, calls, released, outcome):
    api, control = scenario.api, scenario.search_review_control()
    control.configure("release-" + mode)
    provider = api.create_external_provider("source-a", "Controlled release source", clients.SEARCH_REVIEW_INTERNAL_URL + "/source-a")
    assert api.test_external_provider(provider["id"])["lastTestSucceeded"] is True
    assert api.set_external_provider_confidence(provider["id"], "Standard", None, None).status == 200
    assert api.set_external_provider_enabled(provider["id"], True).status == 200
    api.configure_model_gateway(endpoint_url=clients.SEARCH_REVIEW_INTERNAL_URL + "/v1", api_key=None,
        allow_model_hold_release=mode not in ("off", "non-admin"), roles=[
            {"role": role, "isEnabled": role == ROLE and mode != "role-off", "placement": "Local",
             "localModel": "lab/artifact-release", "cloudModel": None, "reasoning": "Disabled"} for role in FL_MODEL_ROLES])
    if mode == "non-admin":
        reader = api.create_reader("release-reader@example.test", "Lab-Reader-Password1!")
        denied = reader._request("PUT", "/api/v1/admin/model-gateway/", json_body={
            "endpointUrl": clients.SEARCH_REVIEW_INTERNAL_URL + "/v1", "allowCloud": False,
            "roles": [], "allowModelHoldRelease": True})
        assert denied.status == 403, denied
        assert api._request("GET", "/api/v1/admin/model-gateway/").body["allowModelHoldRelease"] is False
    request_id, _ = api.create_demo_request("Ebook", slug="cinder-gate")
    if released:
        books = q.wait_for_cwa_book(scenario.cwa_client, "Cinder Gate", None)
        assert len(books) == 1, f"Expected one CWA copy of the fictional fixture, got {books}."
        expected = artifact_release_epub(mode)
        actual = scenario.cwa_client.download_epub(books[0])
        assert actual == expected, (
            "CWA changed the approved fictional fixture before destination verification: "
            f"expected size={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}, "
            f"downloaded size={len(actual)} sha256={hashlib.sha256(actual).hexdigest()}"
        )
        q.wait_for_status(api, request_id, "Available")
        assets = _recent(api)
        # CWA verification archives Trusted assets immediately after marking the request Available.
        assert len(assets) == 1 and assets[0]["storageState"] in ("Trusted", "Archived"), assets
        asset = assets[0]
        finding = asset.get("semanticFinding") or {}
        assert finding.get("isDecision") and finding.get("verdict") == "release", asset
        evidence = (asset.get("evidence") or {}).get("items", [])
        assert any(e.get("source") == "model" and e.get("code") == "artifact.identity_attestation.model_release" for e in evidence), asset
        assert finding.get("claims") and all(c.get("evidenceIds") for c in finding["claims"]), finding
    else:
        asset = _held(api, request_id)
        if mode == "concern":
            assert asset.get("identityHoldCode") == "identity.hold.model_concern", asset
            retry = api._request("POST", f"/api/v1/admin/media-assets/{asset['assetId']}/retry-identity", data=b"")
            assert retry.status == 400, retry  # the existing approval endpoint reports a retained identity hold as validation failure
        # Let the synchronous model review and automatic retry accounting finish before asserting final counters.
        time.sleep(5)
        assert not scenario.cwa_client.find_books("Cinder Gate", None), "A held file reached CWA."
        assert q.request_view(api, request_id)["status"] != "Available"
        assert all(a["storageState"] != "Trusted" for a in _recent(api)), _recent(api)
        assert not any(e.get("source") == "model" for a in _recent(api) for e in (a.get("evidence") or {}).get("items", []))
    state = control.state()
    assert state["searches"] and len(state["acquisitions"]) == 1, state
    assert len(state["modelCalls"]) == calls, {"mode": mode, "calls": state["modelCalls"]}
    usage = api._request("GET", "/api/v1/admin/model-gateway/usage")
    assert usage.status == 200, usage
    rows = [r for r in usage.body["rows"] if r["role"] == ROLE]
    assert sum(r["calls"] for r in rows) == calls, rows
    if outcome == "failure":
        assert rows and all(r["outcome"] != "Succeeded" for r in rows), rows
    elif outcome:
        assert len(rows) == 1 and rows[0]["outcome"] == outcome, rows
    return {"request_id": request_id, "mode": mode, "model_calls": calls, "trusted": released, "outcome": outcome}


def _make_case(case_id, mode, calls, released, outcome):
    def run(ctx, scenario_factory):
        try:
            with scenario_factory(case_id) as scenario:
                detail = _exercise(scenario, mode, calls, released, outcome)
        except Exception as error:
            ctx.fail(case_id, f"Artifact release regression: {error}")
        else:
            ctx.ok(case_id, "Controlled artifact-release assertions passed.", detail)
    run.__name__ = mode.replace("-", "_")
    return run


for _spec in CASES:
    SUITE.case(_spec[0])(_make_case(*_spec))
