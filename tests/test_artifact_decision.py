"""Phase 8a artifact-decision regressions against real FL/gateway/DB/workers and controlled HTTP peers.

One invented, correctly identified EPUB is served through the real scanner, identity pipeline and CWA delivery.
Its headings carry another language's cue word, so the only observation a model may cite is a Weak one. The scripted
model decides what to cite; the assertion is always about what Family Librarian did with it.
"""

from __future__ import annotations

import time

from agent.suites import suite
from family_librarian_lab import clients, question_cases as q
from family_librarian_lab.commands import FL_MODEL_ROLES, ensure_shared_clamav, teardown_shared_clamav

SUITE = suite("artifact-decision", group="artifact-decision", order=39,
              extra_profiles=(clients.SEARCH_REVIEW_PROFILE, clients.CWA_PROFILE, clients.ABS_PROFILE))

ROLE = "ArtifactValidationDecision"
# id, fixture mode, model calls, ledger outcome, whether the file is held
CASES = (
    ("AD-01", "hold", 1, "Succeeded", True),
    ("AD-02", "off", 0, None, False),
    ("AD-03", "no-objection", 1, "Succeeded", False),
    ("AD-04", "positive-hold", 1, "SchemaRejected", False),
    ("AD-05", "forged-id", 1, "SchemaRejected", False),
    ("AD-06", "invalid-json", 1, "InvalidJson", False),
    ("AD-07", "transport", 1, "failure", False),
)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _wait_for_held_asset(api, request_id, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        assets = api.list_assets()
        held = [asset for asset in assets if asset.get("storageState") == "Unmatched"]
        if held:
            return held[0]
        assert not any(asset.get("storageState") == "Trusted" for asset in assets), assets
        if time.monotonic() >= deadline:
            raise AssertionError(f"The model-held EPUB never reached Unmatched: {q.describe(api, request_id)}; assets={assets!r}")
        time.sleep(1)


def _exercise(scenario, mode, calls, outcome, held):
    api = scenario.api
    control = scenario.search_review_control()
    control.configure("artifact-" + mode)
    row = api.create_external_provider("source-a", "Controlled artifact source", clients.SEARCH_REVIEW_INTERNAL_URL + "/source-a")
    provider_id = row["id"]
    assert api.test_external_provider(provider_id)["lastTestSucceeded"] is True
    assert api.set_external_provider_confidence(provider_id, "Standard", None, None).status == 200
    assert api.set_external_provider_enabled(provider_id, True).status == 200
    api.configure_model_gateway(endpoint_url=clients.SEARCH_REVIEW_INTERNAL_URL + "/v1", api_key=None, roles=[
        {"role": role, "isEnabled": role == ROLE and mode != "off", "placement": "Local",
         "localModel": "lab/artifact-decision", "cloudModel": None, "reasoning": "Disabled"} for role in FL_MODEL_ROLES
    ])
    request_id, _ = api.create_demo_request("Ebook", slug="cinder-gate")
    if held:
        asset = _wait_for_held_asset(api, request_id)
        assert asset.get("identityHoldCode") == "identity.hold.model_concern", asset
        assert str(asset.get("identityMismatchReason", "")).startswith("An automated reviewer raised a concern:"), asset
        finding = asset.get("semanticFinding") or {}
        assert finding.get("isDecision") is True and finding.get("verdict") == "hold", asset
        time.sleep(5)  # the retry loop may look again, but the held candidate must not be fetched twice
        assert not scenario.cwa_client.find_books("Cinder Gate", None), "A model-held file must not be published."
        assert q.request_view(api, request_id)["status"] != "Available"
        assert len(control.state()["acquisitions"]) == 1, control.state()["acquisitions"]
    else:
        # A model that cannot ground a concern, or is absent, leaves the deterministic approval exactly as it was.
        q.wait_for_cwa_book(scenario.cwa_client, "Cinder Gate", "Mara Quillon")
        q.wait_for_status(api, request_id, "Available")
        assert not any(asset.get("identityHoldCode") for asset in api.list_assets()), api.list_assets()
    state = control.state()
    assert len(state["modelCalls"]) == calls, {"mode": mode, "calls": len(state["modelCalls"])}
    assert state["searches"], "An idle fixture cannot prove a decision."
    usage = api._request("GET", "/api/v1/admin/model-gateway/usage")
    assert usage.status == 200, usage
    rows = [row for row in usage.body["rows"] if row["role"] == ROLE]
    assert sum(row["calls"] for row in rows) == calls, rows
    assert all(row["role"] == ROLE for row in usage.body["rows"]), usage.body
    if outcome == "failure":
        assert rows and all(row["outcome"] != "Succeeded" for row in rows), rows
    elif outcome:
        assert len(rows) == 1 and rows[0]["outcome"] == outcome, rows
    if calls:
        # The prompt carries untrusted file facts; it must show the Weak fact that a hold may legitimately cite.
        shown = "\n".join(str(m.get("content", "")) for m in state["modelCalls"][0]["messages"])
        assert "idv.ev-compare-heading-language | Weak" in shown, shown[:2000]
    return {"request_id": request_id, "mode": mode, "model_calls": calls, "outcome": outcome, "held": held}


def _make_case(case_id, mode, calls, outcome, held):
    def run(ctx, scenario_factory):
        try:
            with scenario_factory(case_id) as scenario:
                detail = _exercise(scenario, mode, calls, outcome, held)
        except Exception as error:
            ctx.fail(case_id, f"Artifact decision regression: {error}")
        else:
            ctx.ok(case_id, "Controlled artifact-decision assertions passed.", detail)
    run.__name__ = mode.replace("-", "_")
    return run


for _spec in CASES:
    SUITE.case(_spec[0])(_make_case(*_spec))
