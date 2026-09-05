"""Real outbound dispatcher and SMTP delivery, including shared membership.

Admin status transitions enqueue durable communications. Mailpit independently
checks actual recipients across dispatcher polls and application restarts.
"""

from __future__ import annotations

import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, ensure_smtp_fixture_tls, teardown_shared_clamav

SUITE = suite(
    "communications",
    group="communications",
    order=26,
    # cwa-local is required, not just smtp: create_demo_ebook_request() ->
    # BookRequestService.CreateAsync gates on FormatReadinessService, which
    # for Ebook requires CWA to be configured and passing its connection
    # test (FormatReadinessService.CheckAsync -> CwaSettingsService.Get
    # RequestReadinessErrorAsync). Bringing up this profile is what makes
    # _wire_destinations() in commands.py auto-configure+enable CWA before
    # any case runs, the same free wiring CWA-L-02 relies on -- without it
    # every ebook request here is rejected with a 400 before SMTP is ever
    # involved (found for real: COMM-01/02 failing on request creation with
    # only the smtp profile active).
    extra_profiles=(clients.SMTP_PROFILE, clients.CWA_PROFILE),
)

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


@SUITE.case("COMM-04")
def shared_request_emails_only_active_members_after_restart(ctx, scenario_factory):
    """Exercise durable participant lookup and SMTP dispatch, including withdrawal."""
    def operation() -> dict[str, object]:
        with scenario_factory("COMM-04") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Mailpit destination is missing.")
            api = scenario.api
            emails = [f"comm04-reader-{i}@example.test" for i in range(4)]
            readers = [api.create_reader(email, "Lab-Reader-2026!Pass") for email in emails]
            work_id = api.resolve_demo_work()
            requests = [reader.create_request(work_id, ["Ebook"], note=f"Private note {i}")
                        for i, reader in enumerate(readers[:3])]
            assert all(response.status == 201 for response in requests)
            request_id = requests[0].body["id"]
            assert all(response.body["id"] == request_id for response in requests)
            assert readers[0].withdraw_request(request_id).status == 200
            scenario.restart_service("family-librarian")
            scenario.reauthenticate()
            for i, reader in enumerate(readers):
                mine = reader.my_requests()
                rows = mine["active"] + mine["history"]
                shared = [row for row in rows if row["id"] == request_id]
                if i == 3:
                    assert shared == [], "Unrelated member can see the request"
                else:
                    assert len(shared) == 1 and shared[0]["note"] == f"Private note {i}"
                    assert (shared[0]["status"] == "Cancelled") == (i == 0)
            _configure_and_enable_smtp(scenario)
            before = api.admin_request(request_id)["request"]
            transition = api.admin_transition_request(request_id, "NotAvailable",
                                                       expected_version=before["version"],
                                                       reason="No suitable copy is available.")
            assert transition.status == 200, f"Transition returned HTTP {transition.status}"
            for email in emails[1:3]:
                assert scenario.smtp_client.find_message(to=email, subject_contains=before["workTitle"],
                                                        timeout_seconds=45) is not None, \
                    f"No shared-request email reached {email}"
            # A second dispatcher startup must not resend already delivered messages.
            scenario.restart_service("family-librarian")
            scenario.reauthenticate()
            deadline = time.monotonic() + 35
            while True:
                messages = scenario.smtp_client.messages()
                recipients = [address["Address"] for message in messages
                              for address in message.get("To", [])]
                assert sorted(recipients) == sorted(emails[1:3]), \
                    "Missing, duplicate, withdrawn-member, or unrelated-member SMTP delivery"
                assert all(message.get("Username") == clients.SMTP_AUTH_USERNAME for message in messages)
                if time.monotonic() >= deadline:
                    break
                time.sleep(1)
            return {"request_id": request_id, "recipient_count": 2,
                    "withdrawn_recipient_count": 0, "unrelated_recipient_count": 0}

    _run(ctx, "COMM-04", operation)
