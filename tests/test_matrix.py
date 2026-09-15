"""Outbound Matrix provider and the minimal two-way proof of concept
(COMM-1, family-librarian's own
".ai_docs/family-librarian-communications-alpha4-plan.md"), exercised
against a real disposable Matrix homeserver (Continuwuity).

family-librarian's own test suite never exercises HttpMatrixClient for
real -- IMatrixClient is mocked in every in-repo test (MatrixSettingsServiceTests,
MatrixOutboundCommunicationProviderTests, MatrixIdentityLinkServiceTests,
MatrixInboundRouterTests). That is this suite's whole purpose: assert
against a real homeserver's own API (Continuwuity) and a real second
Matrix client acting as an ordinary household member, never against Family
Librarian's own state, per this lab's "Purpose" design principle
(docs/01-...-design.md) -- same posture test_smtp.py already established
for SMTP.

MTX-04 reuses the exact RequestStatusChanged trigger tests/test_communications.py's
COMM-01 already proved for SMTP (force a request to Available via
admin_transition_request), but adds Matrix as a second, simultaneously
enabled provider and asserts independent delivery to both -- the actual
"multi-provider fan-out" claim COMM-1's plan makes, not just Matrix in
isolation. It does not re-prove the dispatcher mechanics test_communications.py
already covers (disabled-provider skip, restart durability, shared-request
membership) -- those are provider-agnostic and stay proven there.

Not covered here, deferred -- both blocked on the same real product gap
already documented for KIN-01/02 in docs/01-family-librarian-integration-test-
design.md §8 ("nothing yet triggers an actual [Kindle] send... add this
scenario once the app exposes a trigger for it"):
  - KindleDeliveryConfirmationRequested actually reaching Matrix and the
    inbound yes/no reply driving DeliveryAttemptService.ConfirmReceivedAsync/
    ReportMissingAsync -- COMM-1 plan §A/§D's whole point, and MatrixInboundRouterTests
    already unit-tests the routing logic against a mock, but nothing in this
    lab can yet produce a *real* DeliveryAttempt to hang that ask off of.
  - Reply idempotency (confirm via Matrix then the web, or vice versa) and
    the "unrecognized reply" generic response -- both need the above to
    exist first.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, ensure_smtp_fixture_tls, teardown_shared_clamav

SUITE = suite(
    "matrix",
    group="matrix",
    order=27,
    # cwa-local is required for MTX-04's request creation, same reason
    # test_communications.py's own SUITE declares it: BookRequestService.
    # CreateAsync gates ebook requests on FormatReadinessService, which
    # requires a configured, passing CWA connection regardless of anything
    # Matrix-specific. smtp is required too -- MTX-04 proves delivery to
    # both providers at once, so both destinations need to be up for every
    # case in this suite (scenario_factory brings up the same profile set
    # for all of a suite's cases, not per-case).
    extra_profiles=(clients.MATRIX_PROFILE, clients.CWA_PROFILE, clients.SMTP_PROFILE),
)

_VERIFICATION_CODE_PATTERN = re.compile(r"\b(\d{6})\b")
# MatrixInboundSyncHostedService's own idle backoff is 2s (far shorter than
# OutboundCommunicationDispatcherHostedService's 15s poll), but this still
# needs a real homeserver round trip (DM room, /sync, reply, /sync again) on
# top -- comfortable margin without waiting out an excessive deadline on a
# real failure.
_INBOUND_TIMEOUT_SECONDS = 30.0
_DISPATCH_TIMEOUT_SECONDS = 30.0


@SUITE.setup
def _setup(scenario_factory):
    # ensure_smtp_fixture_tls() is needed even though this suite is
    # matrix-first: MTX-04 configures and tests SMTP too (see its own
    # docstring), and STARTTLS is mandatory (SmtpSettings has no plaintext
    # option) -- same reason test_smtp.py/test_communications.py both merge
    # this in.
    scenario_factory.extra_env = {**ensure_shared_clamav(), **ensure_smtp_fixture_tls()}
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


def _require_matrix(scenario) -> None:
    if scenario.matrix_household is None:
        raise AssertionError("Scenario did not bring up a Matrix destination.")
    if scenario.matrix_reader is None:
        raise AssertionError("Scenario did not seed a Matrix-testing FL reader.")


def _link_household_member(scenario) -> str:
    """Drives the full verification-code round trip (request -> bot DM ->
    reply -> verified) and returns the DM room id, ready for a caller to
    read further messages (e.g. a later notification) from. Shared by
    MTX-03 (which proves this flow itself) and MTX-04 (which needs a
    verified link as a precondition, not something it's re-proving)."""
    household = scenario.matrix_household
    reader = scenario.matrix_reader

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
            break
        time.sleep(0.5)
    if not status.get("isVerified"):
        raise AssertionError(f"Link never became verified after replying with the code: {status!r}")
    if status.get("matrixUserId") != household.user_id:
        raise AssertionError(
            f"Verified link recorded the wrong Matrix user id: {status!r} (expected {household.user_id!r})"
        )

    return invite_room


@SUITE.case("MTX-01")
def configure_test_and_enable_connects_to_a_real_homeserver(ctx, scenario_factory):
    """_wire_destinations() already ran this exact flow once during scenario
    setup (it's a prerequisite almost every other case needs) -- this case
    re-drives it explicitly against the same saved config, proving the
    flow itself works rather than just trusting setup's own side effect."""
    def operation() -> dict[str, object]:
        with scenario_factory("MTX-01") as scenario:
            _require_matrix(scenario)

            status = scenario.api.matrix_settings()
            if not status.get("isEnabled"):
                raise AssertionError(f"Matrix settings did not report enabled after scenario setup: {status!r}")
            if not status.get("hasAccessToken"):
                raise AssertionError(f"Matrix settings did not report a saved access token: {status!r}")

            probe = scenario.api.send_matrix_test()
            if not probe.get("succeeded"):
                raise AssertionError(f"Matrix test did not succeed against the saved config: {probe!r}")

            return {"homeserver_url": status.get("homeserverUrl"), "bot_user_id": status.get("botUserId")}

    _run(ctx, "MTX-01", operation)


@SUITE.case("MTX-02")
def wrong_access_token_surfaces_a_real_connection_failure(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("MTX-02") as scenario:
            _require_matrix(scenario)

            result = scenario.api.send_matrix_test(access_token="definitely-the-wrong-access-token")
            if result.get("succeeded"):
                raise AssertionError(f"Expected the test to fail with a wrong access token: {result!r}")

            # The saved (correct) settings must be untouched -- send_matrix_test()
            # tests draft values, it never persists them (MatrixSettingsEndpoints.
            # SendTestAsync's own contract, same as SMTP's equivalent).
            status_after = scenario.api.matrix_settings()
            if not status_after.get("isEnabled"):
                raise AssertionError(f"Matrix reported disabled after a failed *test* call: {status_after!r}")

            return {"message": result.get("message")}

    _run(ctx, "MTX-02", operation)


@SUITE.case("MTX-03")
def household_member_links_their_matrix_id_via_verification_code(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("MTX-03") as scenario:
            _require_matrix(scenario)
            reader = scenario.matrix_reader

            before = reader.matrix_link_status()
            if before.get("isVerified") or before.get("awaitingVerification"):
                raise AssertionError(f"Reader already had a Matrix link before requesting one: {before!r}")

            _link_household_member(scenario)
            status = reader.matrix_link_status()

            return {"matrix_user_id": status.get("matrixUserId")}

    _run(ctx, "MTX-03", operation)


@SUITE.case("MTX-04")
def request_status_change_reaches_both_smtp_and_a_linked_matrix_user(ctx, scenario_factory):
    """The actual multi-provider claim COMM-1's plan makes: the same
    RequestStatusChanged communication independently reaches a Matrix DM and
    an SMTP inbox for one enabled-on-both requester -- not just that Matrix
    works in isolation. SMTP-side mechanics (dispatcher polling, retries,
    restart durability) are already covered by test_communications.py and
    not re-proven here."""
    def operation() -> dict[str, object]:
        with scenario_factory("MTX-04") as scenario:
            _require_matrix(scenario)
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            scenario.smtp_client.clear()

            scenario.api.configure_smtp(
                host=clients.SMTP_INTERNAL_HOST,
                port=clients.SMTP_INTERNAL_PORT,
                username=clients.SMTP_AUTH_USERNAME,
                password=clients.SMTP_AUTH_PASSWORD,
                from_address="library@example.test",
                from_name="Family Librarian Lab",
            )
            scenario.smtp_client.clear()  # drop configure_smtp()'s own test-send probe

            reader = scenario.matrix_reader
            household = scenario.matrix_household
            invite_room = _link_household_member(scenario)

            # create_demo_ebook_request() is a member action, fine on the
            # reader (makes the reader the requester, so requesterEmail ==
            # clients.MATRIX_READER_EMAIL below). admin_request()/
            # admin_transition_request() are admin-only
            # (RequireAuthorization("Admin")) -- use the scenario's own
            # admin-authenticated api for those instead.
            request_id, _ = reader.create_demo_ebook_request()
            before = scenario.api.admin_request(request_id)
            requester_email = before["requesterEmail"]
            work_title = before["request"]["workTitle"]

            transitioned = scenario.api.admin_transition_request(
                request_id, "Available", expected_version=before["request"]["version"]
            )
            if transitioned.status != 200:
                raise AssertionError(
                    f"Admin transition to Available was rejected: HTTP {transitioned.status} {transitioned.body!r}"
                )

            delivered_email = scenario.smtp_client.find_message(
                to=requester_email, subject_contains=work_title, timeout_seconds=_DISPATCH_TIMEOUT_SECONDS
            )
            if delivered_email is None:
                raise AssertionError("Mailpit's own API never observed an email to the requester.")

            delivered_matrix = household.wait_for_message(
                room_id=invite_room,
                predicate=lambda _room, text: work_title in text,
                timeout=_DISPATCH_TIMEOUT_SECONDS,
            )
            if delivered_matrix is None:
                raise AssertionError(
                    "The household member's Matrix room never received a DM mentioning the request's "
                    "work title after it became Available."
                )

            return {
                "request_id": request_id,
                "delivered_email_subject": delivered_email.get("Subject"),
                "delivered_matrix_room": delivered_matrix[0],
            }

    _run(ctx, "MTX-04", operation)
