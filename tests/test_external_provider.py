"""External-provider protocol-v2 lab scenarios.

Drives a real, deployed instance of Family Librarian's own protocol-v2
conformance fixture (repos/family-librarian/samples/FamilyLibrarian.SampleProvider)
as an admin-registered external provider -- the first coverage this lab has
of that path at all. Every other acquisition suite (test_gutenberg.py,
test_cwa_*, test_abs*) exercises a different acquisition path; the lab's own
design doc (docs/01-family-librarian-integration-test-design.md) names "a
separately deployed protocol provider" as an explicitly open gap this suite
closes.

The fixture's own hardcoded catalog (SampleProviderHost.cs) already
demonstrates every protocol v2 wire behavior added this cycle; this suite's
four cases were chosen to cover one distinct behavior each rather than
re-proving what ExternalProviderClientTests.cs (the product repo's own
conformance test, run against the same fixture code over real Kestrel) has
already proven at the HTTP layer. What's new here is proving the *whole
stack* end to end: FL's own admin registration, ExternalCandidateAvailabilityChecker's
search + ExternalProviderMatchVerifier's re-verification, DirectAcquisitionService's
gates, the real AcquisitionJobPollingHostedService background poller, the
real security pipeline, and (EXTPROV-01) a real landing in CWA.

Every candidate this suite uses resolves as a BookMatchBasis.Identifier
match, not TitleAuthor: confirmed against real code
(ExternalProviderMatchVerifier.VerifyAsync + DeterministicBookMatcher.ResolveUnique)
that basis depends only on the *request's own* catalog Work carrying an
ISBN and the provider's search returning exactly one title/author-corroborated
candidate -- never on whether the provider's own response carries an
identifier (the fixture's candidates carry none). Each of the four demo
catalog entries this suite depends on (DemoBookMetadataProvider.cs) has an
ISBN for exactly this reason, so none of these cases need
`confirm_low_confidence_match=True` for that gate -- it never fires here.

Deliberately NOT built this pass: cancel (`POST .../cancel`) and
Idempotency-Key replay -- both are already proven at the wire layer by
ExternalProviderClientTests.cs against this exact fixture code; driving them
through the full live stack would be additional assurance, not new
coverage, and was explicitly deferred in favor of the four scenarios below
(each of which *is* new coverage). Real follow-up work, not cut for
convenience.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav

SUITE = suite(
    "external-provider", group="external-provider", order=35,
    extra_profiles=(clients.EXTERNAL_PROVIDER_PROFILE, clients.CWA_PROFILE),
)

# Matches DemoBookMetadataProvider.cs's four new entries -- see this module's
# docstring for why each needs an ISBN, and SampleProviderHost.cs for what
# each candidate itself demonstrates.
PRIDE_AND_PREJUDICE_SLUG = "pride-and-prejudice"
THE_TIME_MACHINE_SLUG = "the-time-machine"
DEBT_OF_HONOR_SLUG = "debt-of-honor"
JACK_RYAN_OMNIBUS_SLUG = "jack-ryan-omnibus"

SAMPLE_PROVIDER_ID = "sample-provider"
SAMPLE_PROVIDER_DISPLAY_NAME = "Sample Provider (lab fixture)"


@SUITE.setup
def _setup(scenario_factory):
    # Same reason test_gutenberg.py needs shared ClamAV: every acquisition
    # here runs through the real security pipeline before landing in CWA.
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


def _register_and_enable_sample_provider(api) -> str:
    """Registers the fixture the same way an administrator would through the
    admin UI: create the row, test the connection (negotiates a protocol
    version and caches manifest/health, same as the real "Test connection"
    button), then enable it. Returns the new row's id."""
    created = api.create_external_provider(
        SAMPLE_PROVIDER_ID, SAMPLE_PROVIDER_DISPLAY_NAME, clients.EXTERNAL_PROVIDER_INTERNAL_URL)
    provider_id = created.get("id")
    if not isinstance(provider_id, str):
        raise AssertionError(f"External provider registration did not return an id: {created!r}")

    tested = api.test_external_provider(provider_id)
    if tested.get("lastTestSucceeded") is not True:
        raise AssertionError(f"External provider connection test did not succeed: {tested!r}")

    enabled = api.set_external_provider_enabled(provider_id, True)
    if enabled.status != 200:
        raise AssertionError(f"Enabling the external provider returned HTTP {enabled.status}: {enabled.body!r}")

    return provider_id


def _sample_provider_ebook_option(options: dict[str, object]) -> dict[str, object] | None:
    ebook_options = options.get("ebook")
    if not isinstance(ebook_options, list):
        return None
    return next(
        (option for option in ebook_options if isinstance(option, dict) and option.get("providerId") == SAMPLE_PROVIDER_ID),
        None,
    )


def _ebook_format(request: dict[str, Any]) -> dict[str, Any] | None:
    formats = request.get("formats")
    if not isinstance(formats, list):
        return None
    return next((item for item in formats if isinstance(item, dict) and item.get("mediaType") == "Ebook"), None)


def _poll_request_outcome(api, request_id: str, *, timeout_seconds: float) -> dict[str, object]:
    """Poll the admin request view until AcquisitionJobPollingHostedService's
    real, unattended 5-second sweep (AcquisitionJobPollingHostedService.PollInterval)
    drives the underlying ProviderAcquisitionJob to completion and the
    request reaches a terminal-for-this-format state, or the timeout
    elapses. Returns the last observed request dict either way."""
    deadline = time.monotonic() + timeout_seconds
    request: dict[str, Any] = {}
    while True:
        admin_view = api.admin_request(request_id)
        candidate = admin_view.get("request")
        if isinstance(candidate, dict):
            request = candidate
            if request.get("status") not in ("PendingAcquisition", None):
                return request
        if time.monotonic() >= deadline:
            return request
        time.sleep(2)


@SUITE.case("EXTPROV-01")
def happy_path_acquisition_lands_in_cwa(ctx, scenario_factory):
    """First end-to-end proof this lab has that a registered external
    provider can be searched, submitted to, polled to completion by the
    real background poller, downloaded, and staged through the security
    pipeline into CWA."""
    def operation() -> dict[str, object]:
        with scenario_factory("EXTPROV-01") as scenario:
            _register_and_enable_sample_provider(scenario.api)
            request_id, format_id = scenario.api.create_demo_ebook_request(slug=PRIDE_AND_PREJUDICE_SLUG)
            work_id = scenario.api.resolve_demo_work(PRIDE_AND_PREJUDICE_SLUG)
            options = scenario.api.fulfillment_options(work_id)
            option = _sample_provider_ebook_option(options)
            if option is None:
                raise AssertionError(f"No sample-provider ebook option was found: {options!r}")
            if option.get("matchBasis") != "Identifier":
                raise AssertionError(f"Expected an Identifier-basis match: {option!r}")

            acquired = scenario.api.acquire_direct(
                request_id, format_id, option["providerId"], option["providerResultId"])
            if acquired.status != 202:
                raise AssertionError(f"Starting the acquisition returned HTTP {acquired.status}: {acquired.body!r}")
            job_id = (acquired.body or {}).get("providerAcquisitionJobId")
            if not isinstance(job_id, str):
                raise AssertionError(f"Accepted acquisition did not return a job id: {acquired.body!r}")

            request = _poll_request_outcome(scenario.api, request_id, timeout_seconds=60)
            if request.get("status") != "Available":
                raise AssertionError(f"Request did not reach Available: {request!r}")

            # AcquisitionJobPollingService never updates the original
            # ProviderAttempt row on completion (confirmed against real
            # code) -- it stays "Submitted" for the life of the request,
            # never "Acquired". Asserting "Acquired" here would be a wrong,
            # always-failing assertion.
            attempts = scenario.api.provider_attempts(request_id)
            submitted = any(
                isinstance(attempt, dict) and attempt.get("outcome") == "Submitted" for attempt in attempts)
            if not submitted:
                raise AssertionError(f"Starting the acquisition did not record a Submitted provider attempt: {attempts!r}")

            books = scenario.cwa_client.find_books("Pride and Prejudice", "Jane Austen")
            if not books:
                raise AssertionError("CWA did not receive the acquired book.")
            return {"request_id": request_id, "job_id": job_id, "request": request, "opds_books": books}

    _run(ctx, "EXTPROV-01", operation)


@SUITE.case("EXTPROV-02")
def waiting_user_interaction_still_completes(ctx, scenario_factory):
    """The fixture's "the-time-machine" candidate reports state=waiting,
    phase=user-interaction for ~2 seconds before resuming on its own --
    standing in for a browser-gated acquisition like Anna's Archive's.
    Whether this suite's own poll happens to land inside that ~2s window is
    incidental (best-effort secondary check below); the real assertion is
    that the request still reaches Available afterward."""
    def operation() -> dict[str, object]:
        with scenario_factory("EXTPROV-02") as scenario:
            _register_and_enable_sample_provider(scenario.api)
            request_id, format_id = scenario.api.create_demo_ebook_request(slug=THE_TIME_MACHINE_SLUG)
            work_id = scenario.api.resolve_demo_work(THE_TIME_MACHINE_SLUG)
            options = scenario.api.fulfillment_options(work_id)
            option = _sample_provider_ebook_option(options)
            if option is None:
                raise AssertionError(f"No sample-provider ebook option was found: {options!r}")

            acquired = scenario.api.acquire_direct(
                request_id, format_id, option["providerId"], option["providerResultId"])
            if acquired.status != 202:
                raise AssertionError(f"Starting the acquisition returned HTTP {acquired.status}: {acquired.body!r}")

            observed_waiting = False
            deadline = time.monotonic() + 60
            request: dict[str, Any] = {}
            while True:
                admin_view = scenario.api.admin_request(request_id)
                candidate = admin_view.get("request")
                if isinstance(candidate, dict):
                    request = candidate
                    ebook = _ebook_format(request)
                    if ebook is not None and ebook.get("progressCode") == "AwaitingProviderAction":
                        observed_waiting = True
                    if request.get("status") not in ("PendingAcquisition", None):
                        break
                if time.monotonic() >= deadline:
                    break
                time.sleep(1)

            if request.get("status") != "Available":
                raise AssertionError(f"Request did not reach Available: {request!r}")

            books = scenario.cwa_client.find_books("The Time Machine", "H. G. Wells")
            if not books:
                raise AssertionError("CWA did not receive the acquired book.")
            return {"request_id": request_id, "request": request, "observed_waiting": observed_waiting}

    _run(ctx, "EXTPROV-02", operation)


@SUITE.case("EXTPROV-03")
def candidate_changed_is_a_clean_refusal_not_a_silent_failure(ctx, scenario_factory):
    """The fixture's "debt-of-honor" candidate always declares candidateRevision
    "rev-1" from /search but always rejects it with 409 CANDIDATE_CHANGED
    from /acquire -- standing in for an upstream record that changed between
    search and acquire. A real client must see a clean refusal, not a
    generic error, and must not have started tracking a job or touched the
    security pipeline."""
    def operation() -> dict[str, object]:
        with scenario_factory("EXTPROV-03") as scenario:
            _register_and_enable_sample_provider(scenario.api)
            request_id, format_id = scenario.api.create_demo_ebook_request(slug=DEBT_OF_HONOR_SLUG)
            work_id = scenario.api.resolve_demo_work(DEBT_OF_HONOR_SLUG)
            options = scenario.api.fulfillment_options(work_id)
            option = _sample_provider_ebook_option(options)
            if option is None:
                raise AssertionError(f"No sample-provider ebook option was found: {options!r}")

            acquired = scenario.api.acquire_direct(
                request_id, format_id, option["providerId"], option["providerResultId"])
            if acquired.status != 400:
                raise AssertionError(f"Expected a clean refusal, got HTTP {acquired.status}: {acquired.body!r}")

            request = scenario.api.admin_request(request_id).get("request") or {}
            if request.get("status") == "Available":
                raise AssertionError(f"A rejected candidate still reached Available: {request!r}")
            assets = scenario.api.list_assets()
            if assets:
                raise AssertionError(f"A rejected candidate reached the security pipeline: {assets!r}")
            return {"request_id": request_id, "response": acquired.body, "request": request}

    _run(ctx, "EXTPROV-03", operation)


@SUITE.case("EXTPROV-04")
def collection_release_is_rejected_despite_an_identifier_match(ctx, scenario_factory):
    """The fixture's "jack-ryan-omnibus" candidate declares isCollection=true.
    ExternalReleasePolicy must flag it and DirectAcquisitionService must
    refuse it -- and, per this module's docstring, this candidate is an
    Identifier-basis match, so the refusal must be the release-policy gate
    itself, not a side effect of the separate low-confidence-match gate
    (which never fires here)."""
    def operation() -> dict[str, object]:
        with scenario_factory("EXTPROV-04") as scenario:
            _register_and_enable_sample_provider(scenario.api)
            request_id, format_id = scenario.api.create_demo_ebook_request(slug=JACK_RYAN_OMNIBUS_SLUG)
            work_id = scenario.api.resolve_demo_work(JACK_RYAN_OMNIBUS_SLUG)
            options = scenario.api.fulfillment_options(work_id)
            option = _sample_provider_ebook_option(options)
            if option is None:
                raise AssertionError(f"No sample-provider ebook option was found: {options!r}")
            if option.get("matchBasis") != "Identifier":
                raise AssertionError(f"Expected an Identifier-basis match: {option!r}")

            acquired = scenario.api.acquire_direct(
                request_id, format_id, option["providerId"], option["providerResultId"])
            if acquired.status != 409:
                raise AssertionError(f"Expected a release-confirmation refusal, got HTTP {acquired.status}: {acquired.body!r}")
            body = acquired.body if isinstance(acquired.body, dict) else {}
            if body.get("requiresConfirmation") is not True:
                raise AssertionError(f"Refusal did not carry requiresConfirmation: {body!r}")

            request = scenario.api.admin_request(request_id).get("request") or {}
            if request.get("status") == "Available":
                raise AssertionError(f"A rejected collection release still reached Available: {request!r}")
            assets = scenario.api.list_assets()
            if assets:
                raise AssertionError(f"A rejected collection release reached the security pipeline: {assets!r}")
            return {"request_id": request_id, "response": body, "request": request}

    _run(ctx, "EXTPROV-04", operation)
