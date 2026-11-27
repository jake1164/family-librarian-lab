"""Outbound SMTP provider (feature/smtp-configuration; the first slice of
family-librarian's own ".ai_docs/Family Librarian — Communications and
Notification Provider Plan.md" -- Phase 1 foundation + Phase 2, SMTP only,
refactored onto that plan's provider abstraction), exercised against a real
disposable SMTP server.

family-librarian's own test suite never exercises MailKitSmtpTestSender --
tests/FamilyLibrarian.Web.Tests/Harness/FamilyLibrarianAppFactory.cs
force-registers AlwaysSucceedsSmtpTestSender for every in-repo test, so
nothing anywhere proves the real MailKit connect/STARTTLS/authenticate/send
path actually works. That is this suite's whole purpose: assert against a
real SMTP catcher's own API (Mailpit), never against Family Librarian's own
state, per this lab's "Purpose" design principle (docs/01-...-design.md).

SmtpSecurityMode has no plaintext option (see family-librarian's own
SmtpSettings.cs: "Plaintext SMTP is intentionally not an option") -- every
case here, including the happy path, genuinely negotiates STARTTLS and
validates Mailpit's certificate. See ensure_smtp_fixture_tls() in
commands.py for the self-signed CA this depends on (same "extend the app
image's own trust store" approach ensure_gutenberg_fixture_tls() already
established, kept independent so the two suites' own SSL_CERT_FILE
overrides never collide -- only one suite is ever active in a given
scenario).

Not covered here, deferred -- see the punch list for the reasons:
  - SmtpSecurityMode.SslOnConnect (implicit TLS): Mailpit's own SMTP server
    only supports STARTTLS, not a separate implicit-TLS listener (confirmed
    against the running container's own startup log), so this security mode
    is unproven by this suite.
  - A server that advertises no STARTTLS support at all: MailKit's
    SecureSocketOptions.StartTls throws NotSupportedException in that case,
    which MailKitSmtpTestSender.SendTestAsync's catch clauses do not
    handle -- worth a dedicated case against a second, TLS-less Mailpit
    instance, but that is new lab infrastructure, not built here.
  - The settings-backup HTTP API (SettingsBackupService's encrypted-JSON
    export/import, distinct from BASE-04's pg_dump-based backup) has no lab
    coverage at all yet, for any setting -- SMTP is only the newest thing
    enrolled in it (SettingsBackupContracts.cs). Tracked as its own
    punch-list item rather than built here; BASE-04 already proves SMTP
    settings and the encrypted password survive a real pg_dump-based
    backup/restore as part of the whole database, which is the more
    realistic "does this data actually survive backup/restore" question.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, ensure_smtp_fixture_tls, teardown_shared_clamav

SUITE = suite("smtp", group="smtp", order=25, extra_profiles=(clients.SMTP_PROFILE,))

_FROM_ADDRESS = "library@example.test"
_FROM_NAME = "Family Librarian Lab"


@SUITE.setup
def _setup(scenario_factory):
    # Family Librarian's own /health/ready includes SecurityScannerHealthCheck
    # -- without ClamAv__Host/_Port pointed at the suite-shared scanner, the
    # family-librarian container never becomes healthy at all ("A required
    # malware scanner is unavailable"), regardless of anything SMTP-specific.
    # Every other suite wires this the same way; this one just needs its own
    # env merged in alongside it.
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


def _set_valid_settings(scenario, *, from_address: str = _FROM_ADDRESS) -> dict[str, Any]:
    settings = scenario.api.set_smtp_settings(
        host=clients.SMTP_INTERNAL_HOST,
        port=clients.SMTP_INTERNAL_PORT,
        security_mode="StartTls",
        username=clients.SMTP_AUTH_USERNAME,
        from_address=from_address,
        from_name=_FROM_NAME,
    )
    if settings.get("host") != clients.SMTP_INTERNAL_HOST or settings.get("port") != clients.SMTP_INTERNAL_PORT:
        raise AssertionError(f"Saved SMTP settings did not round-trip host/port: {settings!r}")
    return settings


def _send_test(scenario, recipient_address: str, *, from_address: str = _FROM_ADDRESS) -> dict[str, Any]:
    # SmtpSettingsService.SendTestAsync tests the draft values passed here,
    # not whatever _set_valid_settings() already saved (see its own remarks
    # and send_smtp_test()'s docstring) -- every case must resend the same
    # host/port/securityMode/username/fromAddress/fromName it just saved.
    # `password` is left out deliberately: it falls back to the currently
    # saved password, exercising that fallback exactly the way a real "Test"
    # click without retyping the password would.
    return scenario.api.send_smtp_test(
        recipient_address,
        host=clients.SMTP_INTERNAL_HOST,
        port=clients.SMTP_INTERNAL_PORT,
        security_mode="StartTls",
        username=clients.SMTP_AUTH_USERNAME,
        from_address=from_address,
        from_name=_FROM_NAME,
    )


@SUITE.case("SMTP-01")
def configure_test_and_enable_delivers_a_real_authenticated_email(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SMTP-01") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            scenario.smtp_client.clear()

            _set_valid_settings(scenario)
            scenario.api.set_smtp_password(clients.SMTP_AUTH_PASSWORD)

            recipient = "smtp-01-recipient@example.test"
            test_result = _send_test(scenario, recipient)
            if not test_result.get("succeeded"):
                raise AssertionError(f"SMTP test send did not succeed: {test_result!r}")

            # Assert against Mailpit's own API, not Family Librarian's
            # report of success -- a real message that a real SMTP server
            # actually received and can show back, not an internal log line.
            delivered = scenario.smtp_client.find_message(to=recipient, subject_contains="Family Librarian")
            if delivered is None:
                raise AssertionError("Mailpit's own API never observed the test email.")
            if delivered.get("Username") != clients.SMTP_AUTH_USERNAME:
                raise AssertionError(
                    f"Delivered message was not authenticated as {clients.SMTP_AUTH_USERNAME!r}: {delivered!r}"
                )

            enable_response = scenario.api.set_smtp_enabled(True)
            if enable_response.status != 200:
                raise AssertionError(
                    f"Enabling SMTP after a passing test was rejected: HTTP {enable_response.status} "
                    f"{enable_response.body!r}"
                )
            status = scenario.api.smtp_settings()
            if not status.get("isEnabled"):
                raise AssertionError(f"SMTP settings did not report enabled after a successful enable call: {status!r}")

            return {
                "delivered_message_id": delivered.get("ID"),
                "delivered_subject": delivered.get("Subject"),
                "authenticated_username": delivered.get("Username"),
            }

    _run(ctx, "SMTP-01", operation)


def _smtp_validation_errors(body: object) -> list[str]:
    if not isinstance(body, dict):
        return []
    errors = body.get("errors")
    if not isinstance(errors, dict):
        return []
    smtp_errors = errors.get("smtp")
    return [str(message) for message in smtp_errors] if isinstance(smtp_errors, list) else []


def _require_no_recorded_test(scenario, when: str) -> None:
    status = scenario.api.smtp_settings()
    if status.get("lastTestSucceeded") is not None:
        raise AssertionError(f"Expected no recorded SMTP test result {when}: {status!r}")


@SUITE.case("SMTP-02")
def enabling_requires_a_passing_test_of_the_saved_settings(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SMTP-02") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            admin_email = str(scenario.api.me().get("email") or "")
            if not admin_email:
                raise AssertionError("The signed-in administrator has no email address to receive the turn-on test.")

            _set_valid_settings(scenario)
            scenario.api.set_smtp_password(clients.SMTP_AUTH_PASSWORD)
            first_test = _send_test(scenario, "smtp-02-first@example.test")
            if not first_test.get("succeeded"):
                raise AssertionError(f"Initial SMTP test did not succeed: {first_test!r}")

            # Every saved change discards the recorded result (SmtpSettings.ResetTestResult()),
            # so the pass above must not carry over to a saved password that does not work.
            scenario.api.set_smtp_password("definitely-the-wrong-password")
            _require_no_recorded_test(scenario, "after the saved password changed")

            # Testing the right password without saving it proves nothing about what is saved,
            # so the host reports the result but must not record it.
            unsaved = scenario.api.send_smtp_test(
                "smtp-02-unsaved@example.test",
                host=clients.SMTP_INTERNAL_HOST,
                port=clients.SMTP_INTERNAL_PORT,
                security_mode="StartTls",
                username=clients.SMTP_AUTH_USERNAME,
                password=clients.SMTP_AUTH_PASSWORD,
                from_address=_FROM_ADDRESS,
                from_name=_FROM_NAME,
            )
            if not unsaved.get("succeeded"):
                raise AssertionError(f"Test of the unsaved correct password did not succeed: {unsaved!r}")
            if "not saved yet" not in str(unsaved.get("message", "")):
                raise AssertionError(f"Expected the result to say the values are not saved: {unsaved!r}")
            _require_no_recorded_test(scenario, "after testing unsaved values")

            # Enabling runs its own test of the saved settings, so the broken saved password
            # has to be refused by the real server.
            scenario.smtp_client.clear()
            blocked = scenario.api.set_smtp_enabled(True)
            if blocked.status != 400:
                raise AssertionError(
                    f"Expected enabling SMTP with a broken saved password to be rejected; "
                    f"got HTTP {blocked.status} {blocked.body!r}"
                )
            blocked_errors = _smtp_validation_errors(blocked.body)
            if not any("Email stayed off" in message for message in blocked_errors):
                raise AssertionError(f"Expected the failed turn-on test message, got {blocked_errors!r}")
            status_after_block = scenario.api.smtp_settings()
            if status_after_block.get("isEnabled"):
                raise AssertionError("SMTP reported enabled despite the enable request being rejected.")
            if status_after_block.get("lastTestSucceeded") is not False:
                raise AssertionError(f"The failed turn-on test was not recorded: {status_after_block!r}")
            if scenario.smtp_client.find_message(to=admin_email, timeout_seconds=3.0) is not None:
                raise AssertionError("Mailpit received a turn-on test despite the rejected authentication.")

            # With working saved settings and no separate test, enabling tests them itself and
            # sends that message to the administrator. The changed From address shows that the
            # message was sent with what is saved now.
            changed_from = "library-changed@example.test"
            _set_valid_settings(scenario, from_address=changed_from)
            scenario.api.set_smtp_password(clients.SMTP_AUTH_PASSWORD)
            _require_no_recorded_test(scenario, "before enabling the repaired settings")
            scenario.smtp_client.clear()
            allowed = scenario.api.set_smtp_enabled(True)
            if allowed.status != 200:
                raise AssertionError(f"Enabling working SMTP settings was rejected: HTTP {allowed.status} {allowed.body!r}")
            status_after_enable = scenario.api.smtp_settings()
            if not status_after_enable.get("isEnabled") or status_after_enable.get("lastTestSucceeded") is not True:
                raise AssertionError(f"SMTP was not enabled with a recorded pass: {status_after_enable!r}")
            delivered = scenario.smtp_client.find_message(to=admin_email, subject_contains="Family Librarian")
            if delivered is None:
                raise AssertionError("Mailpit never observed the turn-on test sent to the administrator.")
            if delivered.get("Username") != clients.SMTP_AUTH_USERNAME:
                raise AssertionError(f"The turn-on test was not authenticated as expected: {delivered!r}")
            sender = (delivered.get("From") or {}).get("Address")
            if sender != changed_from:
                raise AssertionError(f"The turn-on test did not use the saved From address: {sender!r}")

            # A recorded pass for unchanged settings is enough: enabling again sends nothing.
            disabled = scenario.api.set_smtp_enabled(False)
            if disabled.status != 200:
                raise AssertionError(f"Disabling SMTP was rejected: HTTP {disabled.status} {disabled.body!r}")
            scenario.smtp_client.clear()
            enabled_again = scenario.api.set_smtp_enabled(True)
            if enabled_again.status != 200:
                raise AssertionError(f"Re-enabling SMTP was rejected: HTTP {enabled_again.status} {enabled_again.body!r}")
            if scenario.smtp_client.find_message(to=admin_email, timeout_seconds=3.0) is not None:
                raise AssertionError("Re-enabling unchanged, already tested settings sent another test email.")

            return {
                "blocked_status": blocked.status,
                "blocked_errors": blocked_errors,
                "allowed_status": allowed.status,
                "turn_on_test_message_id": delivered.get("ID"),
            }

    _run(ctx, "SMTP-02", operation)


@SUITE.case("SMTP-03")
def wrong_credentials_surface_a_real_authentication_failure(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SMTP-03") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            scenario.smtp_client.clear()

            _set_valid_settings(scenario)
            scenario.api.set_smtp_password("definitely-the-wrong-password")

            recipient = "smtp-03-recipient@example.test"
            result = _send_test(scenario, recipient)
            if result.get("succeeded"):
                raise AssertionError(f"Expected the test send to fail with wrong credentials: {result!r}")
            message = (result.get("message") or "").lower()
            if "auth" not in message:
                raise AssertionError(f"Expected an authentication-failure message, got: {result!r}")

            # Short timeout: proving a negative, not waiting out the full
            # default deadline for something that will never arrive.
            if scenario.smtp_client.find_message(to=recipient, timeout_seconds=3.0) is not None:
                raise AssertionError("Mailpit received a message despite the rejected authentication.")

            return {"message": result.get("message")}

    _run(ctx, "SMTP-03", operation)


@SUITE.case("SMTP-04")
def unreachable_host_surfaces_a_real_connection_failure(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("SMTP-04") as scenario:
            if scenario.smtp_client is None:
                raise AssertionError("Scenario did not bring up a Mailpit destination.")
            scenario.smtp_client.clear()

            # Real Mailpit host, but a port nothing listens on -- a real,
            # deterministic connection-refused, not a DNS-dependent guess.
            scenario.api.set_smtp_settings(
                host=clients.SMTP_INTERNAL_HOST,
                port=clients.SMTP_UNREACHABLE_PORT,
                security_mode="StartTls",
                username=None,
                from_address=_FROM_ADDRESS,
                from_name=_FROM_NAME,
            )

            result = scenario.api.send_smtp_test(
                "smtp-04-recipient@example.test",
                host=clients.SMTP_INTERNAL_HOST,
                port=clients.SMTP_UNREACHABLE_PORT,
                security_mode="StartTls",
                username=None,
                from_address=_FROM_ADDRESS,
                from_name=_FROM_NAME,
            )
            if result.get("succeeded"):
                raise AssertionError(f"Expected the test send to fail against an unreachable host: {result!r}")

            return {"message": result.get("message")}

    _run(ctx, "SMTP-04", operation)
