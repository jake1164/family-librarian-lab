"""Shared pieces of the Phase 5 requester-question lab cases and `lab seed-questions`.

The fixtures themselves live in Family Librarian's sample provider (`SampleProviderHost.QuestionFixtures`) and demo
catalog; every title, author and series is invented. What each one is for:

=====================  ===========================  ==========================================================
demo catalog slug      sample-provider fixture      what it reaches
=====================  ===========================  ==========================================================
cinder-gate (ebook)    lantern-books-1-3            collection holding the work, TOC lists it  -> question
ashen-crown (ebook)    lantern-books-1-3-short      collection whose TOC lacks the work        -> held if taken
ember-road (ebook)     lantern-box-set              contents unknowable                        -> no question
windfall-hollow (aud.) windfall-abridged            abridged-only                              -> question
hollowmere (ebook)     hollowmere-fr                other-language-only                        -> question
saltmarsh-light (aud.) saltmarsh-part-1             part of a set only                         -> question
cinder-gate (ebook)    lantern-cinder-standalone    off until enabled: a standalone copy appears later
=====================  ===========================  ==========================================================
"""

from __future__ import annotations

import time
from typing import Any

from family_librarian_lab import clients

SAMPLE_PROVIDER_ID = "sample-provider"
SAMPLE_PROVIDER_DISPLAY_NAME = "Sample Provider (lab fixture)"

CINDER_GATE = "cinder-gate"
ASHEN_CROWN = "ashen-crown"
EMBER_ROAD = "ember-road"
WINDFALL_HOLLOW = "windfall-hollow"
HOLLOWMERE = "hollowmere"
SALTMARSH_LIGHT = "saltmarsh-light"

STANDALONE_CINDER = "lantern-cinder-standalone"

QUESTION_CATEGORY = "request.acquisition_question"

#: How long a request gets to reach a question: FL's immediate search plus its background pass.
QUESTION_TIMEOUT_SECONDS = 90.0
#: A collection or standalone copy is downloaded, scanned by the shared ClamAV, verified and published.
AVAILABLE_TIMEOUT_SECONDS = 150.0


def register_sample_provider(api: Any, *, confidence: str = "Strict") -> str:
    """Register the sample provider as an administrator would, set its confidence so a collection is a *question*
    rather than an automatic download, test the connection, and enable it. Returns the provider row id."""
    created = api.create_external_provider(
        SAMPLE_PROVIDER_ID, SAMPLE_PROVIDER_DISPLAY_NAME, clients.EXTERNAL_PROVIDER_INTERNAL_URL
    )
    provider_id = created.get("id")
    if not isinstance(provider_id, str):
        raise AssertionError(f"External provider registration did not return an id: {created!r}")
    tested = api.test_external_provider(provider_id)
    if tested.get("lastTestSucceeded") is not True:
        raise AssertionError(f"External provider connection test did not succeed: {tested!r}")
    set_confidence = api.set_external_provider_confidence(provider_id, confidence, None, None)
    if set_confidence.status != 200:
        raise AssertionError(f"Setting confidence {confidence!r} returned HTTP {set_confidence.status}: {set_confidence.body!r}")
    enabled = api.set_external_provider_enabled(provider_id, True)
    if enabled.status != 200:
        raise AssertionError(f"Enabling the external provider returned HTTP {enabled.status}: {enabled.body!r}")
    return provider_id


def request_view(admin_api: Any, request_id: str) -> dict[str, Any]:
    view = admin_api.admin_request(request_id)
    request = view.get("request")
    if not isinstance(request, dict):
        raise AssertionError(f"Admin request view had no request: {view!r}")
    return request


def needs_review(admin_api: Any, request_id: str) -> dict[str, Any] | None:
    review = request_view(admin_api, request_id).get("needsReview")
    return review if isinstance(review, dict) else None


def describe(admin_api: Any, request_id: str) -> str:
    """One line of what the request looks like now, for a failure message."""
    try:
        request = request_view(admin_api, request_id)
        attempts = admin_api.provider_attempts(request_id)
    except Exception as error:  # noqa: BLE001 - describing a failure must not raise another
        return f"(could not read the request: {error})"
    last = attempts[-1] if attempts else None
    return (
        f"status={request.get('status')!r} needsReview={request.get('needsReview')!r} "
        f"attempts={len(attempts)} last_attempt={last!r}"
    )


def wait_for_question(
    admin_api: Any, request_id: str, kind: str, *, timeout: float = QUESTION_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Poll until the request is waiting on a question of this kind and return its `needsReview`. A *different*
    question kind fails at once: that means the fixture reached another path than the case is about."""
    deadline = time.monotonic() + timeout
    while True:
        review = needs_review(admin_api, request_id)
        if review is not None and review.get("category") == "PreferenceAmbiguity":
            seen = review.get("questionKind")
            if seen == kind:
                return review
            if seen is not None:
                raise AssertionError(f"Expected a {kind} question but the request asked {seen!r}: {review!r}")
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"No {kind} question appeared within {timeout:.0f}s: {describe(admin_api, request_id)}"
            )
        time.sleep(2)


def wait_for_status(
    admin_api: Any, request_id: str, wanted: str, *, timeout: float = AVAILABLE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        request = request_view(admin_api, request_id)
        if request.get("status") == wanted:
            return request
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Request did not reach {wanted!r} within {timeout:.0f}s: {describe(admin_api, request_id)}"
            )
        time.sleep(3)


def question_candidate_id(review: dict[str, Any]) -> str:
    candidates = review.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
        raise AssertionError(f"A question should offer exactly one candidate: {review!r}")
    candidate_id = candidates[0].get("candidateId")
    if not isinstance(candidate_id, str):
        raise AssertionError(f"The question candidate had no id: {candidates[0]!r}")
    return candidate_id


def answer(api: Any, request_id: str, review: dict[str, Any], *, take: bool, always: bool = False) -> Any:
    """Answer a question as `api`'s own user: "Take it" passes the offered candidate, "Keep looking" passes none."""
    return api.resolve_needs_review(
        request_id,
        candidate_id=question_candidate_id(review) if take else None,
        always_accept=always,
    )


def question_notifications(api: Any, request_id: str) -> list[dict[str, Any]]:
    return [
        item for item in api.notifications()
        if item.get("category") == QUESTION_CATEGORY and item.get("subjectId") == request_id
    ]


def asset_hold_codes(admin_api: Any) -> list[str | None]:
    return [asset.get("identityHoldCode") for asset in admin_api.list_assets()]


#: What `lab seed-questions` creates: (demo slug, media type, what it demonstrates).
SEED_REQUESTS: tuple[tuple[str, str, str], ...] = (
    (CINDER_GATE, "Ebook", "collection that holds the book -> CollectionContaining question"),
    (ASHEN_CROWN, "Ebook", "collection whose contents lack the book -> question, then held if taken"),
    (EMBER_ROAD, "Ebook", "box set, contents unknown -> NO question (ordinary review)"),
    (HOLLOWMERE, "Ebook", "only a French copy -> OtherLanguageOnly question"),
    (WINDFALL_HOLLOW, "Audiobook", "only an abridged copy -> AbridgedOnly question (needs Audiobookshelf)"),
    (SALTMARSH_LIGHT, "Audiobook", "only part 1 of 2 -> PartsOnly question (needs Audiobookshelf)"),
)
