"""Real FL/DB/scheduler with invented books and controlled protocol/model HTTP peers."""

from __future__ import annotations

import time

from agent.suites import suite
from family_librarian_lab import clients, question_cases as q
from family_librarian_lab.commands import FL_MODEL_ROLES, ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.search_review_fixture import ALIAS, TITLE

SUITE = suite("search-review", group="search-review", order=37,
              extra_profiles=(clients.SEARCH_REVIEW_PROFILE, clients.CWA_PROFILE))


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _get(api, path):
    response = api._request("GET", path)
    assert response.status == 200, (path, response.status, response.body)
    return response.body


def _prepare(scenario, mode):
    api = scenario.api
    control = scenario.search_review_control()
    control.configure(mode)
    for source in (("source-a",) if mode == "source-review" else ("source-a", "source-b")):
        row = api.create_external_provider(source, source, f"{clients.SEARCH_REVIEW_INTERNAL_URL}/{source}")
        provider_id = row["id"]
        assert api.test_external_provider(provider_id)["lastTestSucceeded"] is True
        assert api.set_external_provider_confidence(provider_id, "Strict", None, None).status == 200
        assert api.set_external_provider_enabled(provider_id, True).status == 200
    api.configure_model_gateway(endpoint_url=f"{clients.SEARCH_REVIEW_INTERNAL_URL}/v1", api_key=None, roles=[
        {"role": role, "isEnabled": role == "SearchGuidance" and mode not in ("role-off", "source-review"),
         "placement": "Local", "localModel": "lab/search", "cloudModel": None, "reasoning": "Disabled"}
        for role in FL_MODEL_ROLES
    ])
    return control


def _wait(api, request_id, control, mode, timeout=180):
    """Persisted attempts are the completion barrier, rather than a quiet fixture or a sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = control.state()
        attempts = api.provider_attempts(request_id)
        providers = {row.get("providerId") for row in attempts}
        if mode == "verified":
            complete = bool(state["acquisitions"])
            if not complete and {"source-a", "source-b"} <= providers:
                raise AssertionError(f"Positive fixture completed without acquisition: {attempts!r}; {state!r}")
        elif mode == "source-review":
            # An ordinary librarian review intentionally stops processing later sources.
            complete = q.needs_review(api, request_id) is not None and "source-a" in providers
        else:
            complete = {"source-a", "source-b"} <= providers
            if not complete and attempts and q.needs_review(api, request_id) is not None:
                complete = {row["source"] for row in state["searches"]} == {"source-a", "source-b"}
        if complete:
            return state
        time.sleep(1)
    raise AssertionError(f"No completed search cycle for {mode}: {q.describe(api, request_id)}; {control.state()!r}")


def _guidance_case(scenario, mode):
    api = scenario.api
    control = _prepare(scenario, mode)
    request_id, _ = api.create_demo_ebook_request(slug="cinder-gate")
    state = _wait(api, request_id, control, mode)
    expected_calls = 0 if mode == "role-off" else 1
    assert len(state["modelCalls"]) == expected_calls, state
    alias_sources = {row["source"] for row in state["searches"] if row["body"]["work"]["title"].casefold() == ALIAS.casefold()}
    expected_alias = mode in ("verified", "wrong-work")
    assert alias_sources == ({"source-a", "source-b"} if expected_alias else set()), state
    assert {row["source"] for row in state["searches"]} == {"source-a", "source-b"}, state
    usage = _get(api, "/api/v1/admin/model-gateway/usage")["rows"]
    guidance_usage = [row for row in usage if row["role"] == "SearchGuidance"]
    if expected_calls:
        outcome = "SchemaRejected" if mode in ("duplicate", "invented-id", "url", "too-long") else "Succeeded"
        assert len(guidance_usage) == 1 and guidance_usage[0]["outcome"] == outcome and guidance_usage[0]["calls"] == 1, usage
    else:
        assert not guidance_usage, usage
    if mode == "verified":
        assert len(state["acquisitions"]) == 1, state
        assert state["acquisitions"][0]["body"]["candidateReference"] == "verified-copy", state
    else:
        assert not state["acquisitions"], state
        assert not api.list_assets(), "Search proposals must not create an asset."
        assert q.request_view(api, request_id)["status"] != "Available"
    return {"request_id": request_id, "mode": mode, "model_calls": len(state["modelCalls"]),
            "searched_sources": sorted({row["source"] for row in state["searches"]}), "acquire_calls": len(state["acquisitions"])}


def _run(ctx, test_id, scenario_factory, mode):
    try:
        with scenario_factory(test_id) as scenario:
            detail = _guidance_case(scenario, mode)
    except Exception as error:
        ctx.fail(test_id, f"Search guidance regression: {error}")
    else:
        ctx.ok(test_id, "Search policy assertions passed.", detail)


@SUITE.case("SR-01")
def source_only_reviews_preserve_provenance_and_require_a_librarian(ctx, scenario_factory):
    try:
        with scenario_factory("SR-01") as scenario:
            api = scenario.api
            control = _prepare(scenario, "source-review")
            request_id, _ = api.create_demo_ebook_request(slug="cinder-gate")
            state = _wait(api, request_id, control, "source-review")
            review = q.needs_review(api, request_id)
            assert review["questionKind"] is None, review
            candidates = api.admin_request(request_id)["reviewCandidates"]
            # Two otherwise equivalent releases from one source: neither may be collapsed.
            assert len(candidates) == 2, candidates
            assert {row["releaseName"] for row in candidates} == {
                "Cinder Gate - Other Writer - Edition A", "Cinder Gate - Other Writer - Edition B"}, candidates
            assert all(row.get("author") is None for row in candidates), "The catalog author leaked into a source record."
            assert all(row.get("titleIsRequestFallback") is True for row in candidates), candidates
            mine = q.member_needs_review(api, request_id)
            assert mine is not None and mine["canAnswer"] is False and not mine["candidates"], mine
            response = api.resolve_needs_review(request_id, candidate_id=candidates[0]["candidateId"],
                                               expected_version=q.request_view(api, request_id)["version"])
            assert response.status == 409, (response.status, response.body)
            invented_id = "00000000-0000-0000-0000-000000000001"
            assert invented_id not in {row["candidateId"] for row in candidates}
            invented = api.resolve_needs_review(request_id, candidate_id=invented_id,
                                               expected_version=q.request_view(api, request_id)["version"])
            assert invented.status in (404, 409), (invented.status, invented.body)
            assert not control.state()["acquisitions"] and not api.list_assets(), state
            assert not state["modelCalls"], state
    except Exception as error:
        ctx.fail("SR-01", f"Source review regression: {error}")
    else:
        ctx.ok("SR-01", "Separate raw releases, no borrowed author, requester download denied.")


@SUITE.case("SG-01")
def a_novel_search_can_find_an_independently_verified_copy(ctx, scenario_factory):
    _run(ctx, "SG-01", scenario_factory, "verified")


@SUITE.case("SG-02")
def a_search_proposal_never_authorizes_a_different_work(ctx, scenario_factory):
    _run(ctx, "SG-02", scenario_factory, "wrong-work")


@SUITE.case("SG-03")
def already_tried_values_are_rejected(ctx, scenario_factory):
    _run(ctx, "SG-03", scenario_factory, "duplicate")


@SUITE.case("SG-04")
def disabled_guidance_makes_no_model_calls(ctx, scenario_factory):
    _run(ctx, "SG-04", scenario_factory, "role-off")


@SUITE.case("SG-05")
def give_up_ends_only_extra_searches(ctx, scenario_factory):
    _run(ctx, "SG-05", scenario_factory, "give-up")


@SUITE.case("SG-06")
def invented_evidence_ids_are_rejected(ctx, scenario_factory):
    _run(ctx, "SG-06", scenario_factory, "invented-id")


@SUITE.case("SG-07")
def url_proposals_are_rejected(ctx, scenario_factory):
    _run(ctx, "SG-07", scenario_factory, "url")


@SUITE.case("SG-08")
def overlong_proposals_are_rejected(ctx, scenario_factory):
    _run(ctx, "SG-08", scenario_factory, "too-long")
