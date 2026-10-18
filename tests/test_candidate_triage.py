"""Phase 7 decisions against real FL/gateway/DB/workers and controlled HTTP peers.

Most cases end at provider job submission. CT-27/28 serve controlled EPUBs
through the real scanner/identity pipeline to prove delivery or an author hold.
"""

from __future__ import annotations

import time

from agent.suites import suite
from family_librarian_lab import clients, question_cases as q
from family_librarian_lab.commands import FL_MODEL_ROLES, ensure_shared_clamav, teardown_shared_clamav

SUITE = suite("candidate-triage", group="candidate-triage", order=38,
              extra_profiles=(clients.SEARCH_REVIEW_PROFILE, clients.CWA_PROFILE, clients.ABS_PROFILE))

# id, fixture mode, effective confidence, expected submitted references, model calls, ledger outcome
CASES = (
    ("CT-01", "keep", "Standard", ("copy-a",), 1, "Succeeded"),
    ("CT-02", "veto", "Strict", ("copy-b",), 1, "Succeeded"),
    ("CT-03", "prefer", "Standard", ("copy-b",), 1, "Succeeded"),
    ("CT-04", "author", "Standard", ("copy-b",), 1, "Succeeded"),
    ("CT-05", "author-strict", "Strict", (), 1, "SchemaRejected"),
    ("CT-06", "author-title-only", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-07", "author-language", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-08", "author-title", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-09", "author-series", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-10", "author-uncorroborated", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-11", "invented-candidate", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-12", "invented-evidence", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-13", "cross-evidence", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-14", "positive-veto", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-15", "prefer-ranked", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-16", "invalid-json", "Standard", ("copy-a",), 1, "InvalidJson"),
    ("CT-17", "transport", "Standard", ("copy-a",), 1, "failure"),
    ("CT-18", "off", "Standard", ("copy-a",), 0, None),
    ("CT-19", "suggest-only", "SuggestOnly", (), 0, None),
    ("CT-20", "changed", "Standard", (), 1, "Succeeded"),
    ("CT-21", "prefer-strict", "Strict", ("copy-a",), 1, "SchemaRejected"),
    ("CT-22", "parts", "Strict", ("part-1", "part-2"), 1, "Succeeded"),
    ("CT-23", "parts-veto", "Strict", (), 1, "Succeeded"),
    ("CT-24", "veto-only", "Standard", (), 1, "Succeeded"),
    ("CT-25", "injection", "Standard", ("copy-a",), 1, "SchemaRejected"),
    # The shared gateway ledger records caller-token cancellation as TransportFailure;
    # triage records its own linked deadline as a Timeout finding and keeps the deterministic pick.
    ("CT-26", "timeout", "Standard", ("copy-a",), 1, "TransportFailure"),
    ("CT-27", "author-delivery", "Standard", ("copy-b",), 1, "Succeeded"),
    ("CT-28", "author-held", "Standard", ("copy-b",), 1, "Succeeded"),
    ("CT-29", "author-with-strong", "Standard", ("copy-a",), 1, "SchemaRejected"),
    ("CT-30", "author-fresh-strong", "Standard", ("copy-a",), 2, "Succeeded"),
)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _wait_for_cycle(api, request_id, *, beyond=0, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = [row for row in api.provider_attempts(request_id) if row.get("providerId") == "source-a"]
        if len(attempts) > beyond:
            return attempts
        time.sleep(1)
    raise AssertionError(f"No completed triage cycle: {q.describe(api, request_id)}")


def _exercise(scenario, mode, level, targets, calls, outcome):
    api = scenario.api
    control = scenario.search_review_control()
    control.configure("triage-" + mode)
    row = api.create_external_provider("source-a", "Controlled triage source", clients.SEARCH_REVIEW_INTERNAL_URL + "/source-a")
    provider_id = row["id"]
    assert api.test_external_provider(provider_id)["lastTestSucceeded"] is True
    assert api.set_external_provider_confidence(provider_id, level, None, None).status == 200
    assert api.set_external_provider_enabled(provider_id, True).status == 200
    api.configure_model_gateway(endpoint_url=clients.SEARCH_REVIEW_INTERNAL_URL + "/v1", api_key=None, roles=[
        {"role": role, "isEnabled": role == "CandidateTriage" and mode != "off", "placement": "Local",
         "localModel": "lab/triage", "cloudModel": None, "reasoning": "Disabled"} for role in FL_MODEL_ROLES
    ])
    request_id, _ = api.create_demo_request("Audiobook" if mode in ("parts", "parts-veto") else "Ebook", slug="cinder-gate")
    attempts = _wait_for_cycle(api, request_id)
    if mode == "veto-only":
        assert any("Automated candidate review" in row["summary"] for row in attempts), attempts
        # The provider's daily interval is intentionally honored even by bulk
        # recheck. A fresh request for identical facts exercises durable reuse
        # without bypassing that interval or changing the test clock/database.
        current = q.request_view(api, request_id)
        cancelled = api.admin_transition_request(request_id, "Cancelled", expected_version=current["version"])
        assert cancelled.status == 200, cancelled
        prior_request = request_id
        request_id, _ = api.create_demo_request("Ebook", slug="cinder-gate")
        assert request_id != prior_request
        attempts = _wait_for_cycle(api, request_id)
    if mode == "author-fresh-strong":
        # The changed pool refuses the first proposal, then the normal retry
        # worker immediately selects the new strong copy in a second pass.
        deadline = time.monotonic() + 180
        while not control.state()["acquisitions"]:
            assert time.monotonic() < deadline, q.describe(api, request_id)
            time.sleep(0.5)
    state = control.state()
    submitted = [row["body"]["candidateReference"] for row in state["acquisitions"]]
    assert sorted(submitted) == sorted(targets), {"mode": mode, "submitted": submitted, "attempts": attempts}
    assert len(state["modelCalls"]) == calls, {"mode": mode, "calls": len(state["modelCalls"])}
    assert state["searches"], "An idle fixture cannot prove selection or refusal."
    usage = api._request("GET", "/api/v1/admin/model-gateway/usage")
    assert usage.status == 200, usage
    rows = [row for row in usage.body["rows"] if row["role"] == "CandidateTriage"]
    assert sum(row["calls"] for row in rows) == calls, rows
    assert all(row["role"] == "CandidateTriage" for row in usage.body["rows"]), usage.body
    if outcome == "failure":
        assert rows and all(row["outcome"] != "Succeeded" for row in rows), rows
    elif outcome:
        assert len(rows) == 1 and rows[0]["outcome"] == outcome, rows
    if not targets:
        assert not api.list_assets(), "Refused triage must not produce an asset."
        assert not api.admin_request(request_id)["providerJobs"], "Refused triage must not leave a provider job."
    if mode == "injection":
        messages = state["modelCalls"][0]["messages"]
        shown = messages[1]["content"]
        assert len(shown) < 9000 and not any(c in shown for c in "<>\u202e\u200b"), shown
        assert "Ignore all previous" not in messages[0]["content"], messages[0]
    if mode == "author-delivery":
        q.wait_for_cwa_book(scenario.cwa_client, "Cinder Gate", "Mara Quillon")
        q.wait_for_status(api, request_id, "Available")
    if mode == "author-held":
        deadline = time.monotonic() + 300
        while True:
            assets = api.list_assets()
            if any(asset.get("storageState") == "Unmatched" for asset in assets):
                break
            assert not any(asset.get("storageState") == "Trusted" for asset in assets), assets
            if time.monotonic() >= deadline:
                raise AssertionError(f"The conflicting-author EPUB was not held: {q.describe(api, request_id)}; assets={assets!r}")
            time.sleep(1)
        assert not scenario.cwa_client.find_books("Cinder Gate", None), "An AI author variant must not publish an identity-mismatched file."
        assert q.request_view(api, request_id)["status"] != "Available"
    return {"request_id": request_id, "mode": mode, "confidence": level,
            "submitted": submitted, "model_calls": calls, "outcome": outcome}


def _make_case(case_id, mode, level, targets, calls, outcome):
    def run(ctx, scenario_factory):
        try:
            with scenario_factory(case_id) as scenario:
                detail = _exercise(scenario, mode, level, targets, calls, outcome)
        except Exception as error:
            ctx.fail(case_id, f"Candidate triage regression: {error}")
        else:
            ctx.ok(case_id, "Controlled triage assertions passed.", detail)
    run.__name__ = mode.replace("-", "_")
    return run


for _spec in CASES:
    SUITE.case(_spec[0])(_make_case(*_spec))
