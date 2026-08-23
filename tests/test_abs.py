"""Audiobookshelf single-file publish scenario (design doc ABS-02)."""

from __future__ import annotations

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
