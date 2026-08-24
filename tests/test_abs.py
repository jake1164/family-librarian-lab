"""Audiobookshelf single-file publish scenario (design doc ABS-02)."""

from __future__ import annotations

import json
import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab.fixtures import clean_audiobook

SUITE = suite("abs", group="abs", order=21)


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
