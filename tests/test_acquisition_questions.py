"""Phase 5 requester questions (AF-14) against a real, deployed Family Librarian.

Family Librarian's own endpoint tests cover each rule with scripted providers. This suite is the first time the whole
stack runs it: FL's real search and verification, the real security pipeline and shared ClamAV, a real protocol-v2
provider, and publishing into a real CWA. The provider is FL's sample provider running with its question fixtures on
(SAMPLE_PROVIDER_QUESTION_FIXTURES=1, set in this suite's extra_env); the books are invented, see
`family_librarian_lab/question_cases.py` for which fixture reaches which path.

Matrix answers are in `test_acquisition_questions_matrix.py`, so the Matrix stack only starts when wanted.

Timing: Family Librarian searches external providers in a background sweep every two minutes and creating a request does
not wake it, so each case waits for a sweep (see question_cases.SWEEP_SECONDS); expect a few minutes per case.

Not covered here: the 7-day reminder (needs a controllable clock; FL's unit tests cover it).

Every case fails loudly if a fixture lands on a different path than it is about (a different question kind, or an
ordinary review): the question trigger depends on the provider's confidence level and on FL's matching order, so a
mismatch means the fixture or the setup needs attention, not that the case should pass.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab import question_cases as q
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav

SUITE = suite(
    "acquisition-questions", group="acquisition-questions", order=36,
    # cwa-local: ebook requests are gated on a passing destination, and Available lands there. abs: the abridged and
    # parts questions are audiobook questions, and audiobook requests need Audiobookshelf.
    extra_profiles=(clients.EXTERNAL_PROVIDER_PROFILE, clients.CWA_PROFILE, clients.ABS_PROFILE),
)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = {
        **ensure_shared_clamav(),
        clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV: "1",
    }
    # Two seeded readers: the shared-request and saved-preference cases need requesters other than the admin.
    scenario_factory.seed_readers = True


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


def _prepare(scenario) -> Any:
    """Fresh fixture state and an enabled, Strict sample provider. Returns the provider control client."""
    control = scenario.external_provider_control()
    control.reset()
    q.register_sample_provider(scenario.api)
    return control


@SUITE.case("Q-01")
def collection_only_result_asks_exactly_once(ctx, scenario_factory):
    """Only a collection that holds the requested book exists: FL asks one question, notifies once, and offers
    the saved-preference option (a collection may be "always accepted"); it must not download anything yet."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-01") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(api, request_id, "CollectionContaining")

            q.question_candidate_id(review)  # exactly one candidate
            # canSavePreference is computed for the viewer, so read it as the requester (the admin view carries False).
            mine = q.member_needs_review(api, request_id)
            if mine is None or mine.get("canSavePreference") is not True:
                raise AssertionError(f"A collection question should offer the requester 'always accept': {mine!r}")
            notices = q.question_notifications(api, request_id)
            if len(notices) != 1 or notices[0].get("repeatCount") != 1:
                raise AssertionError(f"Expected exactly one notification for the question: {notices!r}")
            if api.list_assets():
                raise AssertionError("A question must not download anything before it is answered.")
            return {"request_id": request_id, "question": review}

    _run(ctx, "Q-01", operation)


@SUITE.case("Q-02")
def take_it_fetches_the_collection_and_validates_its_contents(ctx, scenario_factory):
    """"Take it" on a collection whose table of contents lists the book: it is fetched, FL's identity check accepts it
    because the contents contain the requested work (no hold), and it is delivered into CWA. Whether the request then
    becomes Available is Q-14."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-02") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(api, request_id, "CollectionContaining")
            version = q.request_view(api, request_id).get("version")

            response = api.resolve_needs_review(
                request_id, candidate_id=q.question_candidate_id(review), expected_version=version
            )
            if response.status != 200:
                raise AssertionError(f"Taking the collection returned HTTP {response.status}: {response.body!r}")
            books = q.wait_for_cwa_book(scenario.cwa_client, q.COLLECTION_TITLE, q.COLLECTION_AUTHOR)
            holds = [code for code in q.asset_hold_codes(api) if code]
            if holds:
                raise AssertionError(f"The accepted collection was held: {holds!r}")
            return {"request_id": request_id, "cwa_books": books}

    _run(ctx, "Q-02", operation)


@SUITE.case("Q-14")
def an_accepted_collection_becomes_available(ctx, scenario_factory):
    """The end of the "Take it" path: once the collection is in CWA the request is Available.

    This is a known gap, not a flaky case. CWA files the collection under its own title ("The Lantern Cycle Books 1-3"),
    and Family Librarian's CWA verification looks the book up by the requested work's title and ISBN, so it cannot
    recognise it. The failure text shows where the request is stuck (status, attempts, publishing queue). It passes when
    delivery verification honours the accepted collection (the format already records its title as the accepted variant)."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-14") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(api, request_id, "CollectionContaining")
            version = q.request_view(api, request_id).get("version")
            response = api.resolve_needs_review(
                request_id, candidate_id=q.question_candidate_id(review), expected_version=version
            )
            if response.status != 200:
                raise AssertionError(f"Taking the collection returned HTTP {response.status}: {response.body!r}")
            q.wait_for_cwa_book(scenario.cwa_client, q.COLLECTION_TITLE, q.COLLECTION_AUTHOR)
            request = q.wait_for_status(api, request_id, "Available")
            return {"request_id": request_id, "request": request}

    _run(ctx, "Q-14", operation)


@SUITE.case("Q-03")
def take_it_on_a_collection_missing_the_book_is_held(ctx, scenario_factory):
    """The collection's title says "Books 1-3" but its table of contents lacks the requested book: consent to the
    collection must not let it through; it is held for a librarian."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-03") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.ASHEN_CROWN)
            review = q.wait_for_question(api, request_id, "CollectionContaining")
            version = q.request_view(api, request_id).get("version")

            response = api.resolve_needs_review(
                request_id, candidate_id=q.question_candidate_id(review), expected_version=version
            )
            if response.status != 200:
                raise AssertionError(f"Taking the collection returned HTTP {response.status}: {response.body!r}")

            deadline = time.monotonic() + q.AVAILABLE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                holds = [code for code in q.asset_hold_codes(api) if code]
                if "identity.hold.collection_missing_requested_work" in holds:
                    break
                if q.request_view(api, request_id).get("status") == "Available":
                    raise AssertionError("A collection missing the requested book reached Available.")
                time.sleep(3)
            else:
                raise AssertionError(
                    "The collection was never held as missing the requested work: "
                    f"holds={q.asset_hold_codes(api)!r} {q.describe(api, request_id)}"
                )
            return {"request_id": request_id}

    _run(ctx, "Q-03", operation)


@SUITE.case("Q-04")
def keep_looking_is_not_asked_again_for_the_same_candidate(ctx, scenario_factory):
    """"Keep looking" declines the offered candidate; a recheck must not ask about it again."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-04") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(api, request_id, "CollectionContaining")
            version = q.request_view(api, request_id).get("version")

            baseline = q.attempt_count(api, request_id)
            response = api.resolve_needs_review(request_id, candidate_id=None, expected_version=version)
            if response.status != 200:
                raise AssertionError(f"Keep looking returned HTTP {response.status}: {response.body!r}")
            api.recheck_requests()

            # A sweep has to run before "not asked again" means anything; then give the pass a moment to settle.
            q.wait_for_attempts_beyond(api, request_id, baseline)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                again = q.needs_review(api, request_id)
                if again is not None and again.get("questionKind") is not None:
                    raise AssertionError(f"The declined candidate was offered again: {again!r}")
                time.sleep(3)
            if q.request_view(api, request_id).get("status") == "Available" or api.list_assets():
                raise AssertionError("Keep looking must not fetch the declined collection.")
            return {"request_id": request_id, "declined": review}

    _run(ctx, "Q-04", operation)


@SUITE.case("Q-05")
def a_standalone_copy_appearing_later_withdraws_the_question(ctx, scenario_factory):
    """While a question is open the provider gets a standalone copy: the next check fetches it, the question goes
    away and the request becomes Available without anyone answering."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-05") as scenario:
            api = scenario.api
            control = _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            q.wait_for_question(api, request_id, "CollectionContaining")

            control.enable(q.STANDALONE_CINDER)
            api.recheck_requests()
            request = q.wait_for_status(api, request_id, "Available")
            leftover = q.needs_review(api, request_id)
            if leftover is not None and leftover.get("questionKind") is not None:
                raise AssertionError(f"The question was still open after the standalone copy arrived: {leftover!r}")
            return {"request_id": request_id, "request": request}

    _run(ctx, "Q-05", operation)


@SUITE.case("Q-06")
def a_box_set_with_unknown_contents_is_not_a_question(ctx, scenario_factory):
    """A box set says nothing about which books it holds, so FL must not ask the requester about it (negative)."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-06") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.EMBER_ROAD)

            # Only once the provider has actually been searched does "no question" mean anything.
            q.wait_for_attempts_beyond(api, request_id, 0)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                review = q.needs_review(api, request_id)
                if review is not None and review.get("questionKind") is not None:
                    raise AssertionError(f"A box set of unknown contents was offered as a question: {review!r}")
                time.sleep(3)
            return {"request_id": request_id}

    _run(ctx, "Q-06", operation)


@SUITE.case("Q-07")
def abridged_only_audiobook_asks_without_an_always_option(ctx, scenario_factory):
    """Only an abridged audiobook exists: a question, never downloaded automatically, and no "always accept"."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-07") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_request("Audiobook", slug=q.WINDFALL_HOLLOW)
            review = q.wait_for_question(api, request_id, "AbridgedOnly")
            mine = q.member_needs_review(api, request_id)
            if mine is None or mine.get("canSavePreference") is not False:
                raise AssertionError(f"Abridged-only must not offer the requester 'always accept': {mine!r}")
            if api.list_assets():
                raise AssertionError("An abridged copy must not be downloaded before the question is answered.")
            return {"request_id": request_id, "question": review}

    _run(ctx, "Q-07", operation)


@SUITE.case("Q-08")
def other_language_only_asks_and_is_never_fetched_unasked(ctx, scenario_factory):
    """Only a French copy exists: asked, and not fetched before an answer."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-08") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_ebook_request(slug=q.HOLLOWMERE)
            review = q.wait_for_question(api, request_id, "OtherLanguageOnly")
            if api.list_assets():
                raise AssertionError("A copy in another language must not be downloaded before the question is answered.")
            return {"request_id": request_id, "question": review}

    _run(ctx, "Q-08", operation)


@SUITE.case("Q-09")
def parts_only_audiobook_asks_without_an_always_option(ctx, scenario_factory):
    """Only part 1 of a 2-part audiobook exists: a question, and no "always accept"."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-09") as scenario:
            api = scenario.api
            _prepare(scenario)
            request_id, _ = api.create_demo_request("Audiobook", slug=q.SALTMARSH_LIGHT)
            review = q.wait_for_question(api, request_id, "PartsOnly")
            mine = q.member_needs_review(api, request_id)
            if mine is None or mine.get("canSavePreference") is not False:
                raise AssertionError(f"Parts-only must not offer the requester 'always accept': {mine!r}")
            return {"request_id": request_id, "question": review}

    _run(ctx, "Q-09", operation)


@SUITE.case("Q-11")
def a_saved_preference_skips_the_question(ctx, scenario_factory):
    """Answering "Take it" with "always accept collections that include my book" saves a preference; the next
    collection-only request is accepted by it, with no question."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-11") as scenario:
            api = scenario.api
            _prepare(scenario)
            first, _ = api.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(api, first, "CollectionContaining")
            response = q.answer(api, first, review, take=True, always=True)
            if response.status != 200:
                raise AssertionError(f"Answering with 'always accept' returned HTTP {response.status}: {response.body!r}")
            saved = api.acquisition_preferences()
            if not any(item.get("kind") == "AcceptCollectionsContaining" for item in saved):
                raise AssertionError(f"The 'always accept' choice was not saved: {saved!r}")

            second, _ = api.create_demo_ebook_request(slug=q.ASHEN_CROWN)
            deadline = time.monotonic() + q.QUESTION_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                review = q.needs_review(api, second)
                if review is not None and review.get("questionKind") is not None:
                    raise AssertionError(f"The saved preference did not skip the question: {review!r}")
                history = api.admin_request(second).get("statusHistory") or []
                if any("saved preferences" in str(item.get("reason", "")) for item in history if isinstance(item, dict)):
                    break
                time.sleep(3)
            else:
                raise AssertionError(
                    f"Never saw 'Accepted by your saved preferences.' in the history: {q.describe(api, second)}"
                )
            return {"first": first, "second": second, "preferences": saved}

    _run(ctx, "Q-11", operation)


@SUITE.case("Q-12")
def shared_request_first_answer_applies_to_everyone(ctx, scenario_factory):
    """Two people want the same book and share one request. The first answer settles it for both; the second
    person's answer is refused because it was already answered."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-12") as scenario:
            admin, reader = scenario.api, scenario.cwa_reader1
            if reader is None:
                raise AssertionError("Scenario did not seed a reader.")
            _prepare(scenario)
            request_id, _ = admin.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(admin, request_id, "CollectionContaining")

            joined_id, _ = reader.create_demo_ebook_request(slug=q.CINDER_GATE)
            if joined_id != request_id:
                raise AssertionError(f"The second requester got their own request ({joined_id}), not the shared one.")
            mine = [item for item in reader.my_requests().get("active", []) if item.get("id") == request_id]
            if not mine or not isinstance(mine[0].get("needsReview"), dict):
                raise AssertionError(f"The second requester cannot see the question: {mine!r}")

            first = q.answer(reader, request_id, review, take=True)
            if first.status != 200:
                raise AssertionError(f"The first answer returned HTTP {first.status}: {first.body!r}")
            second = q.answer(admin, request_id, review, take=False)
            if second.status not in (404, 409):
                raise AssertionError(f"A second answer should be refused, got HTTP {second.status}: {second.body!r}")
            return {"request_id": request_id}

    _run(ctx, "Q-12", operation)


@SUITE.case("Q-13")
def someone_who_already_accepts_is_not_asked(ctx, scenario_factory):
    """Reader1 saved "always accept collections"; reader2 has not. When both want the same collection-only book,
    only reader2 is asked: reader1's own answer is refused (NotAsked), reader2's is accepted."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-13") as scenario:
            admin, reader1, reader2 = scenario.api, scenario.cwa_reader1, scenario.cwa_reader2
            if reader1 is None or reader2 is None:
                raise AssertionError("Scenario did not seed two readers.")
            _prepare(scenario)

            warmup, _ = reader1.create_demo_ebook_request(slug=q.CINDER_GATE)
            review = q.wait_for_question(admin, warmup, "CollectionContaining")
            saved = q.answer(reader1, warmup, review, take=True, always=True)
            if saved.status != 200:
                raise AssertionError(f"Saving reader1's preference returned HTTP {saved.status}: {saved.body!r}")
            if not any(item.get("kind") == "AcceptCollectionsContaining" for item in reader1.acquisition_preferences()):
                raise AssertionError("Reader1's preference was not saved.")

            request_id, _ = reader2.create_demo_ebook_request(slug=q.ASHEN_CROWN)
            review = q.wait_for_question(admin, request_id, "CollectionContaining")
            joined, _ = reader1.create_demo_ebook_request(slug=q.ASHEN_CROWN)
            if joined != request_id:
                raise AssertionError("Reader1 did not join the shared request.")

            refused = q.answer(reader1, request_id, review, take=True)
            if refused.status != 409:
                raise AssertionError(
                    f"Reader1 already accepts collections and was not asked; expected HTTP 409, got {refused.status}: {refused.body!r}"
                )
            accepted = q.answer(reader2, request_id, review, take=True)
            if accepted.status != 200:
                raise AssertionError(f"Reader2 was asked and should be able to answer: {accepted.status} {accepted.body!r}")
            return {"request_id": request_id}

    _run(ctx, "Q-13", operation)
