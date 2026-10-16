"""Phase 5b: answering a requester question from Matrix, against a real homeserver (Continuwuity).

Same fixtures as `test_acquisition_questions.py` (FL's sample provider with its question fixtures on). The household
member links their Matrix account the way MTX-03 does, then answers FL's DM with the keywords FL accepts: `take it` and
`keep looking`.

Not covered, with the reason:
- `1` / `2` as a reply to the question message: it needs a Matrix reply relation (`m.relates_to`) and the event id of
  the bot's message; the shared se-lab Matrix client sends neither. Adding them is a change to the se-lab submodule.
- A non-participant's linked account being refused: needs a second Matrix user and a second linked FL account. FL's
  own MatrixAcquisitionQuestionEndpointTests cover it with a scripted client.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab import question_cases as q
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav

SUITE = suite(
    "acquisition-questions-matrix",
    group="matrix",
    order=37,
    # cwa-local: the accepted collection lands there (and ebook requests need a passing destination).
    extra_profiles=(clients.MATRIX_PROFILE, clients.CWA_PROFILE, clients.EXTERNAL_PROVIDER_PROFILE),
)

_VERIFICATION_CODE_PATTERN = re.compile(r"\b(\d{6})\b")
_INBOUND_TIMEOUT_SECONDS = 30.0
_DISPATCH_TIMEOUT_SECONDS = 45.0


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = {
        **ensure_shared_clamav(),
        clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV: "1",
    }
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


def _link_household_member(scenario) -> str:
    """The verification-code round trip from test_matrix.py (MTX-03); returns the DM room id. Duplicated rather
    than imported because suite modules are loaded by path and are not importable."""
    household = scenario.matrix_household
    reader = scenario.matrix_reader
    if household is None or reader is None:
        raise AssertionError("Scenario did not bring up Matrix and a Matrix-testing reader.")

    link_requested = reader.request_matrix_link(household.user_id)
    if not link_requested.get("awaitingVerification"):
        raise AssertionError(f"Requesting a link did not report awaitingVerification: {link_requested!r}")
    invite_room = household.wait_for_invite(timeout=_INBOUND_TIMEOUT_SECONDS)
    if invite_room is None:
        raise AssertionError("The bot never invited the household member to a DM room.")
    household.join_room(invite_room)
    received = household.wait_for_message(room_id=invite_room, timeout=_INBOUND_TIMEOUT_SECONDS)
    if received is None:
        raise AssertionError("Never received the verification-code DM from the bot.")
    code_match = _VERIFICATION_CODE_PATTERN.search(received[1])
    if code_match is None:
        raise AssertionError(f"Verification DM did not contain a 6-digit code: {received[1]!r}")
    household.send_message(invite_room, code_match.group(1))

    deadline = time.monotonic() + _INBOUND_TIMEOUT_SECONDS
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = reader.matrix_link_status()
        if status.get("isVerified"):
            return invite_room
        time.sleep(0.5)
    raise AssertionError(f"Link never became verified after replying with the code: {status!r}")


def _wait_for_dm(household, room: str, needle: str, *, timeout: float = _DISPATCH_TIMEOUT_SECONDS) -> str:
    """Read the DM room until the bot sends a message containing `needle`, skipping earlier chatter."""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        received = household.wait_for_message(
            room_id=room, timeout=max(1.0, min(15.0, deadline - time.monotonic()))
        )
        if received is None:
            continue
        seen.append(received[1])
        if needle.lower() in received[1].lower():
            return received[1]
    raise AssertionError(f"The bot never sent a message containing {needle!r}; saw {seen!r}")


def _prepare(scenario) -> None:
    scenario.external_provider_control().reset()
    q.register_sample_provider(scenario.api)


@SUITE.case("Q-10")
def matrix_keyword_answers_settle_a_question(ctx, scenario_factory):
    """The question arrives as a DM; `take it` fetches the collection and `keep looking` declines it."""
    def operation() -> dict[str, object]:
        with scenario_factory("Q-10") as scenario:
            admin, reader, household = scenario.api, scenario.matrix_reader, scenario.matrix_household
            _prepare(scenario)
            room = _link_household_member(scenario)

            take_request, _ = reader.create_demo_ebook_request(slug=q.CINDER_GATE)
            q.wait_for_question(admin, take_request, "CollectionContaining")
            question = _wait_for_dm(household, room, "keep looking")
            household.send_message(room, "take it")
            _wait_for_dm(household, room, "fetching")
            q.wait_for_status(admin, take_request, "Available")

            keep_request, _ = reader.create_demo_ebook_request(slug=q.ASHEN_CROWN)
            q.wait_for_question(admin, keep_request, "CollectionContaining")
            _wait_for_dm(household, room, "keep looking")
            household.send_message(room, "keep looking")
            _wait_for_dm(household, room, "keep looking")

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                review = q.needs_review(admin, keep_request)
                if review is None or review.get("questionKind") is None:
                    break
                time.sleep(2)
            else:
                raise AssertionError("'keep looking' did not close the question: " + q.describe(admin, keep_request))
            if q.request_view(admin, keep_request).get("status") == "Available":
                raise AssertionError("'keep looking' must not fetch the declined collection.")
            return {"take_request": take_request, "keep_request": keep_request, "question": question}

    _run(ctx, "Q-10", operation)
