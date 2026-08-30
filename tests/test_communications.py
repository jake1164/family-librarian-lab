"""Outbound communications dispatcher: the real end-to-end notification
trigger (family-librarian's own ".ai_docs/Family Librarian — Communications
and Notification Provider Plan.md", Phase 1 foundation + Phase 2 SMTP
provider), exercised against a real disposable SMTP server.

`test_smtp.py`'s SMTP-01..04 only ever exercise the manual admin "send test
email" probe (POST .../smtp/test -> MailKitSmtpTestSender). They say nothing
about the feature that actually motivated building the provider
abstraction: an administrator transitions a book request to Available or
NotAvailable, BookRequestService.AdminTransitionAsync enqueues an
OutboundCommunication, and a separate background hosted service
(OutboundCommunicationDispatcherHostedService, ~15s poll loop) later picks it
up and sends it through SmtpOutboundCommunicationProvider -- different code
(SmtpMailTransport shared, but a distinct provider class), different timing
(asynchronous, not inline with the HTTP request), and a distinct recipient-
resolution path (IUserEmailLookup against the request's own owning user, not
an address supplied by the caller). None of that is proven anywhere: not by
family-librarian's own C# tests (OutboundCommunicationDispatcherTests uses
hand-rolled fakes, no real network I/O or background-service timing), and
not by SMTP-01..04 (which never enqueue anything -- they call the test-send
endpoint directly).

Requires `admin_transition_request()` (family_librarian_lab/api.py), which
drives BookRequestService.AdminTransitionAsync directly rather than the real
upload/CWA-import pipeline CWA-L-02 uses to reach Available -- see that
method's docstring for why status="Available" is a legitimate, allowed call
here even though the admin UI never offers it as a button.

Not covered here, deferred:
  - Multiple simultaneous providers delivering the same communication
    independently (plan §20) -- moot until a second outbound provider
    (e.g. Matrix) exists to register alongside SMTP.
  - Any admin-facing view of the outbound communication/delivery log --
    no such endpoint exists in the product yet, so there is nothing here to
    assert against beyond Mailpit's own inbox.
"""

from __future__ import annotations

from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, ensure_smtp_fixture_tls, teardown_shared_clamav

SUITE = suite("communications", group="communications", order=26, extra_profiles=(clients.SMTP_PROFILE,))

_FROM_ADDRESS = "library@example.test"
_FROM_NAME = "Family Librarian Lab"

# OutboundCommunicationDispatcherHostedService polls every 15s
# (src/FamilyLibrarian.Web/Communications/OutboundCommunicationDispatcherHostedService.cs).
# A communication enqueued just after a poll starts waits almost a full
# interval before the next pass picks it up, plus real SMTP connect/send
# time on top -- 30s gives that a comfortable margin without waiting out an
# excessive deadline on a real failure.
_DISPATCH_TIMEOUT_SECONDS = 30.0


@SUITE.setup
def _setup(scenario_factory):
    # Every other suite wires ClamAV the same way (see test_smtp.py) -- FL's
    # own /health/ready never becomes healthy without it, regardless of
    # anything communications-specific.
    scenario_factory.extra_env = {**ensure_shared_clamav(), **ensure_smtp_fixture_tls()}


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


def _configure_and_enable_smtp(scenario) -> None:
    scenario.api.configure_smtp(
        host=clients.SMTP_INTERNAL_HOST,
        port=clients.SMTP_INTERNAL_PORT,
        username=clients.SMTP_AUTH_USERNAME,
        password=clients.SMTP_AUTH_PASSWORD,
        from_address=_FROM_ADDRESS,
        from_name=_FROM_NAME,
    )
    # configure_smtp()'s own test-send probe landed in Mailpit too -- clear
    # it so only the case's own trigger produces a message from here on.
    scenario.smtp_client.clear()


@SUITE.case("COMM-01")
def request_becomes_available_delivers_a_real_email(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("COMM-01") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            _configure_and_enable_smtp(scenario)

            request_id, _ = scenario.api.create_demo_ebook_request()
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

            delivered = scenario.smtp_client.find_message(
                to=requester_email, subject_contains=work_title, timeout_seconds=_DISPATCH_TIMEOUT_SECONDS
            )
            if delivered is None:
                raise AssertionError(
                    "Mailpit's own API never observed an email to the requester after the request "
                    "became Available."
                )
            if delivered.get("Username") != clients.SMTP_AUTH_USERNAME:
                raise AssertionError(
                    f"Delivered notification was not authenticated as {clients.SMTP_AUTH_USERNAME!r}: {delivered!r}"
                )

            return {
                "request_id": request_id,
                "delivered_subject": delivered.get("Subject"),
                "delivered_to": requester_email,
            }

    _run(ctx, "COMM-01", operation)


@SUITE.case("COMM-02")
def request_becomes_not_available_delivers_a_real_email(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("COMM-02") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            _configure_and_enable_smtp(scenario)

            request_id, _ = scenario.api.create_demo_ebook_request()
            before = scenario.api.admin_request(request_id)
            requester_email = before["requesterEmail"]
            work_title = before["request"]["workTitle"]

            transitioned = scenario.api.admin_transition_request(
                request_id,
                "NotAvailable",
                expected_version=before["request"]["version"],
                reason="Could not be sourced from any provider.",
            )
            if transitioned.status != 200:
                raise AssertionError(
                    f"Admin transition to NotAvailable was rejected: HTTP {transitioned.status} {transitioned.body!r}"
                )

            delivered = scenario.smtp_client.find_message(
                to=requester_email, subject_contains=work_title, timeout_seconds=_DISPATCH_TIMEOUT_SECONDS
            )
            if delivered is None:
                raise AssertionError(
                    "Mailpit's own API never observed an email to the requester after the request "
                    "became NotAvailable."
                )

            return {"request_id": request_id, "delivered_subject": delivered.get("Subject")}

    _run(ctx, "COMM-02", operation)


@SUITE.case("COMM-03")
def a_disabled_provider_never_blocks_the_request_transition(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("COMM-03") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            scenario.smtp_client.clear()
            # Deliberately left unconfigured/disabled -- OutboundCommunicationDispatcher
            # is expected to skip every disabled provider and still mark the
            # communication processed (plan §19: a provider failure/absence
            # must never roll back the FL operation that queued it).

            request_id, _ = scenario.api.create_demo_ebook_request()
            before = scenario.api.admin_request(request_id)

            transitioned = scenario.api.admin_transition_request(
                request_id, "Available", expected_version=before["request"]["version"]
            )
            if transitioned.status != 200:
                raise AssertionError(
                    f"Admin transition to Available was rejected with no communication provider "
                    f"enabled: HTTP {transitioned.status} {transitioned.body!r}"
                )
            body = transitioned.body if isinstance(transitioned.body, dict) else {}
            status_after = body.get("request", {}).get("status")
            if status_after != "Available":
                raise AssertionError(f"Request did not report status Available after the transition: {body!r}")

            # Proving a negative: give the dispatcher's poll loop time to run
            # and confirm it never produced a message, rather than asserting
            # instantly (which would pass even if the dispatcher were broken
            # and simply slow).
            requester_email = before["requesterEmail"]
            leaked = scenario.smtp_client.find_message(to=requester_email, timeout_seconds=_DISPATCH_TIMEOUT_SECONDS)
            if leaked is not None:
                raise AssertionError(
                    f"Mailpit received a message despite SMTP never being configured/enabled: {leaked!r}"
                )

            return {"request_id": request_id, "status_after_transition": status_after}

    _run(ctx, "COMM-03", operation)
