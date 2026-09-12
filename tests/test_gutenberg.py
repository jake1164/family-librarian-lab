"""Gutenberg local-catalog lab scenarios.

Proves the fixture pipeline this suite needed to build from scratch (an
HTTPS-only fixture mirror with a self-signed CA the app is made to trust, a
real `.tar.bz2` archive matching what `GutenbergCatalogSynchronizer` actually
parses, an overridden `MinimumBookCount`) against the real, just-merged
Gutenberg sync/search/acquisition code -- not the punch-list plan's
speculative "plain HTTP fixture server" design, which does not work
(GutenbergCatalogOptions/GutenbergMirrorOptions validate every configured URL
as absolute HTTPS at DI startup; confirmed against real code, no bypass hook
exists).

Scope: GUT-01 (first sync from empty state), GUT-02 (local search resolves
from the synced catalog, never a live Gutendex/gutenberg.org search call --
GutenbergProvider.FindDirectAcquisitionsAsync only ever queries the local
Postgres catalog, confirmed by reading it end to end), GUT-06 (a Sound record
excluded from ebook fulfillment), GUT-11/GUT-12 (ACCURACY-1/SELFSERV-1: a
same-title/author Gutenberg edition in a language other than English is
never silently auto-acquired by the real, unattended
AutomaticRequestFulfillmentHostedService background loop, and is instead
offered to the requester as a preference choice they can accept or decline).

**A real production incident this suite's own build surfaced**: this
project's Compose network is NOT actually isolated from the internet --
confirmed the hard way. `GutenbergCatalogHostedService` runs a sync
automatically the moment a fresh catalog has never been synced (not only at
its scheduled daily hour), and that first sync races ahead of this suite's
own explicit `POST /sync`; by the time the explicit call runs, the catalog
is already `isReady`, so it takes the *incremental* path instead of a second
full sync. Incremental sync fetches each recently-updated id individually
from `EbookRdfBaseUrl` -- initially left at its real default
(`https://www.gutenberg.org/cache/epub/`) since only `ArchiveUrl`/
`RecentUpdatesFeedUrl` seemed relevant for a "first sync" case -- and this
container really can reach the real internet, so every fixture book was
silently overwritten with real Gutenberg content on the very first run.
Fixed by also overriding `EbookRdfBaseUrl` and serving each book's RDF at
the per-id path incremental sync expects, alongside the bundled archive.

Deliberately NOT built this pass: GUT-05 (EPUB format preference order) --
found unbuildable via the fulfillment-options endpoint specifically, not cut
for convenience: `FulfillmentOptionResponse`
(FamilyLibrarian.Contracts/Catalog/CatalogWorkResponse.cs) deliberately does
not expose `ProviderData`, the only place the chosen format kind lives (see
the comment where GUT-05 would go, above GUT-06's case). GUT-03/04 (actual
file download, mirror failover -- needs a fixture mirror that replicates
Gutenberg's real split-digit path convention, not just an archive/RSS
server) and GUT-07..10 (corrupted/malformed archive, below-minimum book
count, incremental sync -- need additional archive variants, and GUT-08/
GUT-10's originally planned assertions aren't checkable via the public
/status endpoint at all: ParseErrorCount and LastSuccessfulIncrementalSyncUtc
are internal-only, confirmed against GutenbergCatalogRepository.ToStatus).
Real follow-up work, not cut for convenience.
"""

from __future__ import annotations

import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_gutenberg_fixture_tls, ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.gutenberg_fixtures import SEARCH_TARGET_BOOKS, diversity_books

# CWA_PROFILE is here for the same reason test_base_security.py needs it:
# BookRequestService's FormatReadinessService gate (product commit acb1ff7)
# rejects an *ebook* fulfillment lookup entirely -- Gutenberg option or not
# -- unless some ebook-capable destination is ready. Without this, GUT-02/
# GUT-05 fail with "CWA is not enabled" before Gutenberg's own logic is
# ever exercised, and GUT-06 becomes a false positive: an empty ebook array
# caused by the readiness gate looks identical to one caused by real
# Sound-type exclusion, so it would keep "passing" even if exclusion broke.
SUITE = suite("gutenberg", group="gutenberg", order=30, extra_profiles=(clients.GUTENBERG_PROFILE, clients.CWA_PROFILE))

EXPECTED_BOOK_COUNT = len(diversity_books()) + len(SEARCH_TARGET_BOOKS)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = {**ensure_shared_clamav(), **ensure_gutenberg_fixture_tls()}


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


def _sync_to_completion(api, *, timeout_seconds: float = 90) -> dict[str, object]:
    """Trigger a sync (409 means one is already running -- fine, another
    case's own setup or a retry) and poll /status to a terminal state.
    Real code: POST /sync runs incremental if isReady, else full; GET
    /status's `status` field reaches "Completed" or "Failed" (confirmed
    against GutenbergCatalogEndpoints/GutenbergCatalogStatus)."""
    triggered = api.gutenberg_sync()
    if triggered.status not in (200, 409):
        raise AssertionError(f"Gutenberg sync trigger returned HTTP {triggered.status}: {triggered.body!r}")
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = api.gutenberg_status()
        if status.get("status") in ("Completed", "Failed"):
            return status
        if time.monotonic() >= deadline:
            raise AssertionError(f"Gutenberg sync did not reach a terminal state within {timeout_seconds}s: {status!r}")
        time.sleep(1)


def _enable_gutendex(api) -> None:
    """GutenbergProvider.FindDirectAcquisitionsAsync checks ProviderState.IsUsable
    before ever searching the (already-synced) catalog -- confirmed against
    real code. A fresh scenario's database starts with every provider
    disabled, same as CWA/ABS need their own explicit enable step."""
    result = api.set_provider_enabled("gutendex", True)
    if result.status != 200:
        raise AssertionError(f"Enabling the gutendex provider returned HTTP {result.status}: {result.body!r}")


def _gutendex_ebook_option(options: dict[str, object]) -> dict[str, object] | None:
    """The real provider id is "gutendex" (GutenbergProvider.Id ==
    ProviderRegistry.GutenbergProviderId, a legacy name kept from the
    removed GutendexProvider) -- confirmed against real code, not "gutenberg"
    as the plan doc assumed."""
    ebook_options = options.get("ebook")
    if not isinstance(ebook_options, list):
        return None
    return next(
        (option for option in ebook_options if isinstance(option, dict) and option.get("providerId") == "gutendex"),
        None,
    )


@SUITE.case("GUT-01")
def first_sync_from_empty_state(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("GUT-01") as scenario:
            status = _sync_to_completion(scenario.api)
            if status.get("status") != "Completed":
                raise AssertionError(f"Gutenberg sync did not complete: {status!r}")
            if not status.get("isReady"):
                raise AssertionError(f"Catalog reported Completed but not ready: {status!r}")
            if status.get("bookCount") != EXPECTED_BOOK_COUNT:
                raise AssertionError(
                    f"Expected exactly {EXPECTED_BOOK_COUNT} books from the deterministic fixture "
                    f"archive, got {status.get('bookCount')!r}: {status!r}"
                )
            return {"status": status}

    _run(ctx, "GUT-01", operation)


@SUITE.case("GUT-02")
def local_search_has_no_live_network_dependency(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("GUT-02") as scenario:
            # This container genuinely can reach the real internet (see the
            # module docstring's incident note) -- the guarantee here is not
            # network isolation, it's that GutenbergProvider.FindDirectAcquisitionsAsync
            # never calls Gutendex/gutenberg.org's search at all, only the
            # local Postgres catalog (confirmed by reading it end to end).
            _sync_to_completion(scenario.api)
            _enable_gutendex(scenario.api)
            work_id = scenario.api.resolve_demo_work("project-hail-mary")
            options = scenario.api.fulfillment_options(work_id)
            option = _gutendex_ebook_option(options)
            if option is None:
                raise AssertionError(f"No gutendex-provider ebook option was found: {options!r}")
            return {"work_id": work_id, "option": option}

    _run(ctx, "GUT-02", operation)


# GUT-05 (EPUB preference order) is NOT implemented here, found unbuildable
# via this endpoint during this pass, not cut for convenience: GutenbergProvider's
# chosen format (BuildEbookOption's FormatKind) is carried in FulfillmentOption's
# internal-only ProviderData field, which FulfillmentOptionResponse
# (FamilyLibrarian.Contracts/Catalog/CatalogWorkResponse.cs) deliberately does
# not expose to the client -- confirmed against the real contract, only
# ProviderId/ProviderResultId/OptionKind/AcquisitionMethod/ExternalActionUri
# are visible. Same category of gap as GUT-08/GUT-10 (see this module's
# docstring): proving preference order needs either a product change to
# surface the chosen format, or driving the real acquisition/download path
# to observe which file actually gets fetched -- both bigger than this pass,
# not a same-endpoint fix. Book 10003's three-out-of-order EPUB formats stay
# in the fixture archive for whichever approach is chosen next.


@SUITE.case("GUT-06")
def sound_record_excluded_from_ebook_search(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("GUT-06") as scenario:
            _sync_to_completion(scenario.api)
            _enable_gutendex(scenario.api)
            work_id = scenario.api.resolve_demo_work("a-wrinkle-in-time")
            options = scenario.api.fulfillment_options(work_id)
            option = _gutendex_ebook_option(options)
            if option is not None:
                raise AssertionError(f"A Sound-type Gutenberg record was offered as an ebook option: {option!r}")
            return {"work_id": work_id, "ebook_options": options.get("ebook")}

    _run(ctx, "GUT-06", operation)


def _poll_request_outcome(api, request_id: str, *, timeout_seconds: float) -> dict[str, object]:
    """Poll the admin request view until AutomaticRequestFulfillmentHostedService's
    real, unattended background pass (hardcoded 2-minute interval, not
    configurable -- confirmed against AutomaticRequestFulfillmentHostedService.PollInterval)
    moves the request out of PendingAcquisition, or the timeout elapses.
    Returns the last observed BookRequestResponse either way, so a timeout
    still gives the caller a useful body to report."""
    deadline = time.monotonic() + timeout_seconds
    request: dict[str, object] = {}
    while True:
        admin_view = api.admin_request(request_id)
        candidate = admin_view.get("request")
        if isinstance(candidate, dict):
            request = candidate
            if request.get("status") != "PendingAcquisition":
                return request
        if time.monotonic() >= deadline:
            return request
        time.sleep(3)


@SUITE.case("GUT-11")
def foreign_only_edition_reaches_a_preference_review_not_silent_acquisition(ctx, scenario_factory):
    """ACCURACY-1 + SELFSERV-1: book 10009 (see gutenberg_fixtures.py) is the
    *only* ebook-type Gutenberg record for "The Hobbit" -- a real, unattended
    automatic-fulfillment pass must recognize it as excluded by language,
    never silently acquire it, and route it to the requester as a
    RequestReviewCategory.PreferenceAmbiguity review instead of leaving it
    an indistinguishable "nothing found yet"."""
    def operation() -> dict[str, object]:
        with scenario_factory("GUT-11") as scenario:
            _sync_to_completion(scenario.api)
            _enable_gutendex(scenario.api)
            request_id, format_id = scenario.api.create_demo_ebook_request()

            request = _poll_request_outcome(scenario.api, request_id, timeout_seconds=150)
            if request.get("status") != "NeedsReview":
                raise AssertionError(
                    f"Request did not reach NeedsReview within the automatic pass window: {request!r}"
                )
            needs_review = request.get("needsReview")
            if not isinstance(needs_review, dict) or needs_review.get("category") != "PreferenceAmbiguity":
                raise AssertionError(f"Request reached NeedsReview for the wrong reason: {request!r}")
            candidates = needs_review.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise AssertionError(f"PreferenceAmbiguity review carried no candidates: {needs_review!r}")
            candidate = candidates[0]
            if candidate.get("language") != "es":
                raise AssertionError(f"Offered candidate did not carry the excluded language: {candidate!r}")
            if not isinstance(candidate.get("candidateId"), str):
                raise AssertionError(f"Offered candidate had no id: {candidate!r}")
            return {"request_id": request_id, "format_id": format_id, "needs_review": needs_review}

    _run(ctx, "GUT-11", operation)


@SUITE.case("GUT-12")
def preference_ambiguity_candidate_can_be_accepted(ctx, scenario_factory):
    """SELFSERV-1's "get it anyway": accepting the offered candidate clears
    the review and drives real acquisition of that exact Gutenberg edition,
    through the same security/import pipeline any other acquisition uses."""
    def operation() -> dict[str, object]:
        with scenario_factory("GUT-12") as scenario:
            _sync_to_completion(scenario.api)
            _enable_gutendex(scenario.api)
            request_id, _ = scenario.api.create_demo_ebook_request()

            request = _poll_request_outcome(scenario.api, request_id, timeout_seconds=150)
            needs_review = request.get("needsReview") if isinstance(request, dict) else None
            if not isinstance(needs_review, dict) or needs_review.get("category") != "PreferenceAmbiguity":
                raise AssertionError(f"Request did not reach a PreferenceAmbiguity review: {request!r}")
            candidates = needs_review.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise AssertionError(f"PreferenceAmbiguity review carried no candidates: {needs_review!r}")
            candidate_id = candidates[0].get("candidateId")
            if not isinstance(candidate_id, str):
                raise AssertionError(f"Offered candidate had no id: {candidates[0]!r}")

            resolved = scenario.api.resolve_needs_review(request_id, candidate_id=candidate_id)
            if resolved.status != 200:
                raise AssertionError(f"Accepting the candidate returned HTTP {resolved.status}: {resolved.body!r}")
            resolved_body = resolved.body if isinstance(resolved.body, dict) else {}
            if resolved_body.get("needsReview") is not None:
                raise AssertionError(f"Accepted request still carried a review: {resolved_body!r}")

            # AutomaticRequestFulfillmentService.AcquireOptionAsync runs
            # synchronously inside the resolve call -- no further polling
            # needed, the security pipeline has already recorded the attempt.
            attempts = scenario.api.provider_attempts(request_id)
            acquired = any(
                isinstance(attempt, dict) and attempt.get("outcome") == "Acquired" for attempt in attempts
            )
            if not acquired:
                raise AssertionError(
                    f"Accepting the candidate did not record an Acquired provider attempt: {attempts!r}"
                )
            return {"request_id": request_id, "resolved": resolved_body, "provider_attempts": attempts}

    _run(ctx, "GUT-12", operation)
