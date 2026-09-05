"""Audiobookshelf single-file publish scenario (design doc ABS-02)."""

from __future__ import annotations

import json
import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.fixtures import clean_audiobook, malformed_audiobook

SUITE = suite("abs", group="abs", order=21)


@SUITE.setup
def _setup(scenario_factory):
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


@SUITE.case("ABS-01")
def discovery_and_configuration_do_not_leak_token(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("ABS-01") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            token, library_id, folder_id = scenario.abs_client.ensure_bootstrapped()
            discovery = scenario.api.discover_audiobookshelf_libraries(
                {"baseUrl": "http://abs:80", "apiToken": token}
            )
            libraries = discovery.get("libraries")
            if not discovery.get("succeeded") or not isinstance(libraries, list):
                raise AssertionError(f"Audiobookshelf library discovery failed: {discovery!r}")
            matching_library = next((item for item in libraries if item.get("id") == library_id), None)
            if not isinstance(matching_library, dict) or not any(
                isinstance(folder, dict) and folder.get("id") == folder_id
                for folder in matching_library.get("folders", [])
            ):
                raise AssertionError("Audiobookshelf discovery did not return the selected library and folder.")

            probe = scenario.api.test_audiobookshelf(
                {"baseUrl": "http://abs:80", "libraryId": library_id, "folderId": folder_id, "apiToken": token}
            )
            settings = scenario.api.audiobookshelf_settings()
            if not probe.get("succeeded"):
                raise AssertionError(f"Audiobookshelf connection probe did not succeed: {probe!r}")
            if not settings.get("hasApiToken"):
                raise AssertionError("Saved Audiobookshelf configuration does not retain an API token.")
            for response in (discovery, probe, settings):
                if token in json.dumps(response, sort_keys=True):
                    raise AssertionError("Audiobookshelf API token leaked into an API response.")
            if token in json.dumps(scenario.api.trace, sort_keys=True):
                raise AssertionError("Audiobookshelf API token leaked into the captured API trace.")
            return {
                "library_id": library_id,
                "folder_id": folder_id,
                "discovered_libraries": len(libraries),
                "token_redacted": True,
            }

    _run(ctx, "ABS-01", operation)


@SUITE.case("ABS-02")
def single_file_publish(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("ABS-02") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            request_id, format_id = scenario.api.create_demo_audiobook_request()
            uploaded = scenario.api.upload_manual_audio(
                request_id, format_id, clean_audiobook(), "abs-02-the-hobbit.mp3"
            )
            if uploaded.status != 200:
                raise AssertionError(f"Manual import returned HTTP {uploaded.status}: {uploaded.body!r}")

            delivery = _poll_delivery(scenario.api, scenario.abs_client, request_id, timeout_seconds=90)
            if delivery is None:
                raise AssertionError("No Delivery record reached status Delivered for the request.")

            external_id = delivery.get("externalItemId")
            if not external_id:
                raise AssertionError("Delivery is Delivered but has no externalItemId.")

            abs_item_id = scenario.abs_client.find_item("The Hobbit", "J. R. R. Tolkien")
            if abs_item_id is None:
                raise AssertionError("Audiobookshelf's own library-item API does not show the uploaded item.")

            return {
                "request_id": request_id,
                "delivery_id": delivery.get("id"),
                "external_item_id": external_id,
                "abs_item_id": abs_item_id,
            }

    _run(ctx, "ABS-02", operation)


@SUITE.case("ABS-03")
def existing_item_is_reused_without_a_second_upload(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("ABS-03") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            # The destination acquires the item after the ordinary request was made.
            request_id, format_id = scenario.api.create_demo_audiobook_request()
            seeded_item_id = scenario.abs_client.seed_item(
                clean_audiobook(), "abs-03-existing-the-hobbit.mp3", "The Hobbit", "J. R. R. Tolkien"
            )
            _require_exactly_one_abs_item(scenario.abs_client, "ABS seed created duplicate matching items")

            uploaded = scenario.api.upload_manual_audio(
                request_id, format_id, clean_audiobook(), "abs-03-the-hobbit.mp3"
            )
            if uploaded.status != 200:
                raise AssertionError(f"Manual import returned HTTP {uploaded.status}: {uploaded.body!r}")
            delivery = _poll_delivery(scenario.api, scenario.abs_client, request_id, timeout_seconds=60)
            if delivery is None:
                raise AssertionError("Existing ABS item was not recorded as Delivered.")
            if delivery.get("externalItemId") != seeded_item_id:
                raise AssertionError(
                    "Family Librarian did not retain the directly seeded ABS item id: "
                    f"expected {seeded_item_id!r}, got {delivery.get('externalItemId')!r}."
                )
            matching_ids = _require_exactly_one_abs_item(
                scenario.abs_client, "Publishing an existing ABS item created a duplicate"
            )
            return {"request_id": request_id, "delivery_id": delivery.get("id"), "seeded_item_id": matching_ids[0]}

    _run(ctx, "ABS-03", operation)


@SUITE.case("ABS-06")
def unavailable_api_records_safe_failure_then_recovers_once(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("ABS-06") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            scenario.stop_service("abs")
            request_id, format_id = scenario.api.create_demo_audiobook_request()
            uploaded = scenario.api.upload_manual_audio(
                request_id, format_id, clean_audiobook(), "abs-06-api-outage-the-hobbit.mp3"
            )
            if uploaded.status != 200:
                raise AssertionError(f"Approval during ABS outage returned HTTP {uploaded.status}: {uploaded.body!r}")
            delivery = _find_delivery(scenario.api.publishing_queue(), request_id)
            if not isinstance(delivery, dict):
                raise AssertionError("ABS outage did not create a delivery record.")
            if delivery.get("status") != "Failed":
                raise AssertionError(f"ABS outage produced an unsafe delivery state: {delivery!r}")
            reason = delivery.get("failureReason")
            if not isinstance(reason, str) or not reason.strip():
                raise AssertionError("ABS outage did not record a safe failure reason.")
            _assert_request_is_not_available(scenario.api, request_id)

            scenario.start_service("abs")
            scenario.api.recheck_delivery(_required_delivery_id(delivery))
            recovered = _poll_delivery(scenario.api, scenario.abs_client, request_id, timeout_seconds=90)
            if recovered is None:
                raise AssertionError("ABS recheck did not recover the failed delivery after the API restarted.")
            matching_ids = _require_exactly_one_abs_item(
                scenario.abs_client, "ABS recovery created a duplicate destination item"
            )
            return {
                "request_id": request_id,
                "delivery_id": delivery.get("id"),
                "failure_reason": reason,
                "external_item_id": matching_ids[0],
            }

    _run(ctx, "ABS-06", operation)


@SUITE.case("ABS-07")
def rejected_audiobook_never_reaches_audiobookshelf(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("ABS-07") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            request_id, format_id = scenario.api.create_demo_audiobook_request()
            uploaded = scenario.api.upload_manual_audio(
                request_id, format_id, malformed_audiobook(), "abs-07-malformed-the-hobbit.mp3"
            )
            if uploaded.status not in (200, 400):
                raise AssertionError(f"Malformed audio returned unexpected HTTP {uploaded.status}: {uploaded.body!r}")
            if _find_delivery(scenario.api.publishing_queue(), request_id) is not None:
                raise AssertionError("Rejected audiobook created an ABS delivery record.")
            if scenario.abs_client.find_item("The Hobbit", "J. R. R. Tolkien", timeout_seconds=5) is not None:
                raise AssertionError("Rejected audiobook reached Audiobookshelf's library API.")
            _assert_request_is_not_available(scenario.api, request_id)
            return {"request_id": request_id, "upload_status": uploaded.status, "destination_items": 0}

    _run(ctx, "ABS-07", operation)


def _poll_delivery(api, abs_client, request_id: str, *, timeout_seconds: float) -> dict[str, object] | None:
    """Drives both sides of Audiobookshelf's eventual consistency directly,
    rather than assuming a background job closes the gap on its own:
    Audiobookshelf's upload-triggered auto-scan was found not to fire
    reliably for an upload from Family Librarian's own HTTP client (see
    AbsClient.trigger_scan), and -- unlike CWA's CwaVerificationHostedService
    -- Family Librarian has no background job that rechecks a Verifying
    Audiobookshelf delivery on its own; it tries exactly once synchronously
    and then waits for an explicit recheck call (confirmed against
    AudiobookshelfPublishingService's source)."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        abs_client.trigger_scan()
        queue = api.publishing_queue()
        delivery = next((item for item in queue.get("deliveries", []) if item.get("requestId") == request_id), None)
        if delivery is not None and delivery.get("status") == "Delivered":
            return delivery
        if delivery is not None and delivery.get("status") == "Verifying":
            api.recheck_delivery(delivery["id"])
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def _find_delivery(queue: dict[str, object], request_id: str) -> dict[str, object] | None:
    return next((item for item in queue.get("deliveries", []) if item.get("requestId") == request_id), None)


def _required_delivery_id(delivery: dict[str, object]) -> str:
    delivery_id = delivery.get("id")
    if not isinstance(delivery_id, str):
        raise AssertionError("Delivery did not contain an id.")
    return delivery_id


def _require_exactly_one_abs_item(abs_client, message: str) -> list[str]:
    item_ids = abs_client.find_items("The Hobbit", "J. R. R. Tolkien")
    if len(item_ids) != 1:
        raise AssertionError(f"{message}: expected one matching ABS item, got {item_ids!r}.")
    return item_ids


def _assert_request_is_not_available(api, request_id: str) -> None:
    request = api.admin_request(request_id)
    if request.get("status") == "Available":
        raise AssertionError("Request became Available while its ABS destination was unavailable or rejected the asset.")
