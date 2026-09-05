"""Audiobookshelf multi-track direct-acquisition scenario (design doc ABS-05)."""

from __future__ import annotations

import re
import time
from typing import Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_gutenberg_fixture_tls, ensure_shared_clamav, teardown_shared_clamav
from family_librarian_lab.gutenberg_fixtures import multi_track_mirror_files


# This is intentionally a separate suite from test_abs.py.  It needs the
# HTTPS Gutenberg fixture and direct-acquisition path; the ordinary ABS
# publishing cases should remain focused on their manual-import boundary and
# must not pay for (or accidentally depend on) a catalog sync.
SUITE = suite(
    "abs-bundle",
    group="abs",
    order=22,
    extra_profiles=(clients.ABS_PROFILE, clients.GUTENBERG_PROFILE),
)


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = {**ensure_shared_clamav(), **ensure_gutenberg_fixture_tls()}


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


@SUITE.case("ABS-05")
def ordered_multi_track_direct_acquisition_reaches_one_abs_item(ctx, scenario_factory):
    """Prove the normal multi-file distribution path, not a fake repeated upload.

    The three fixture tracks differ in frame count.  GutenbergProvider sorts
    the source paths, fetches each one through the real mirror resolver, and
    stages them as one bundle.  The assertion against Audiobookshelf's own
    item API then proves that the destination has one item containing all
    three tracks in that expected sequence.
    """

    def operation() -> dict[str, object]:
        with scenario_factory("ABS-05") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Scenario did not bring up an Audiobookshelf destination.")

            _sync_to_completion(scenario.api)
            _enable_gutendex(scenario.api)
            work_id = scenario.api.resolve_demo_work("the-hobbit")
            request_id, format_id = scenario.api.create_demo_audiobook_request()
            option = _gutenberg_audio_bundle_option(scenario.api.fulfillment_options(work_id))
            if option is None:
                raise AssertionError("No Gutenberg multi-track audiobook option was offered for The Hobbit.")

            provider_id = _required_option_string(option, "providerId")
            provider_result_id = _required_option_string(option, "providerResultId")
            acquired = scenario.api.acquire_direct(request_id, format_id, provider_id, provider_result_id)
            if acquired.status != 200:
                raise AssertionError(f"Direct audiobook acquisition returned HTTP {acquired.status}: {acquired.body!r}")

            delivery = _poll_delivery(scenario.api, scenario.abs_client, request_id, timeout_seconds=90)
            if delivery is None:
                raise AssertionError("The acquired multi-track bundle did not reach Delivered state.")
            external_item_id = delivery.get("externalItemId")
            if not isinstance(external_item_id, str) or not external_item_id:
                raise AssertionError(f"Delivered bundle has no Audiobookshelf item id: {delivery!r}")

            matching_ids = scenario.abs_client.find_items("The Hobbit", "J. R. R. Tolkien")
            if matching_ids != [external_item_id]:
                raise AssertionError(
                    "The bundle did not produce exactly its recorded Audiobookshelf item: "
                    f"expected {[external_item_id]!r}, got {matching_ids!r}."
                )

            tracks = scenario.abs_client.audio_track_filenames(external_item_id)
            sequences = _bundle_track_sequences(tracks)
            if sequences != [1, 2, 3]:
                raise AssertionError(
                    "Audiobookshelf did not retain the three bundle tracks in source order: "
                    f"expected sequences [1, 2, 3], got {sequences!r} from {tracks!r}."
                )

            return {
                "request_id": request_id,
                "delivery_id": delivery.get("id"),
                "abs_item_id": external_item_id,
                "track_filenames": tracks,
                "track_sequences": sequences,
                "fixture_track_bytes": [len(content) for _, content in multi_track_mirror_files()],
            }

    _run(ctx, "ABS-05", operation)


def _sync_to_completion(api, *, timeout_seconds: float = 90) -> None:
    triggered = api.gutenberg_sync()
    if triggered.status not in (200, 409):
        raise AssertionError(f"Gutenberg sync trigger returned HTTP {triggered.status}: {triggered.body!r}")
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = api.gutenberg_status()
        if status.get("status") == "Completed" and status.get("isReady"):
            return
        if status.get("status") == "Failed":
            raise AssertionError(f"Gutenberg sync failed: {status!r}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"Gutenberg sync did not complete within {timeout_seconds}s: {status!r}")
        time.sleep(1)


def _enable_gutendex(api) -> None:
    response = api.set_provider_enabled("gutendex", True)
    if response.status != 200:
        raise AssertionError(f"Enabling Gutenberg returned HTTP {response.status}: {response.body!r}")


def _gutenberg_audio_bundle_option(options: dict[str, object]) -> dict[str, object] | None:
    audiobook_options = options.get("audiobook")
    if not isinstance(audiobook_options, list):
        return None
    # FulfillmentOptionResponse deliberately exposes only the provider/result
    # identity needed to acquire a fresh option; its internal format marker
    # ("audio-bundle") is not a client API field. The destination assertion
    # below is the black-box proof that this selected option was a bundle.
    return next(
        (
            option
            for option in audiobook_options
            if isinstance(option, dict)
            and option.get("providerId") == "gutendex"
        ),
        None,
    )


def _required_option_string(option: dict[str, object], field: str) -> str:
    value = option.get(field)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"Direct-acquisition option does not contain {field}: {option!r}")
    return value


def _bundle_track_sequences(filenames: list[str]) -> list[int]:
    """Extract the deterministic sequence Family Librarian gives ABS tracks.

    PublishingFilenames intentionally includes a random GUID to avoid
    collisions, so asserting literal filenames would make this test flaky.
    The zero-padded sequence is the contract that preserves bundle order.
    """
    pattern = re.compile(r"^The Hobbit-(\d{3})-[0-9a-f]{32}\.mp3$")
    sequences: list[int] = []
    for filename in filenames:
        match = pattern.fullmatch(filename)
        if match is None:
            raise AssertionError(f"Audiobookshelf track does not have the expected bundle filename shape: {filename!r}")
        sequences.append(int(match.group(1)))
    return sequences


def _poll_delivery(api, abs_client, request_id: str, *, timeout_seconds: float) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        abs_client.trigger_scan()
        queue = api.publishing_queue()
        delivery = next((item for item in queue.get("deliveries", []) if item.get("requestId") == request_id), None)
        if delivery is not None and delivery.get("status") == "Delivered":
            return delivery
        if delivery is not None and delivery.get("status") == "Verifying":
            delivery_id = delivery.get("id")
            if not isinstance(delivery_id, str):
                raise AssertionError(f"Verifying delivery has no id: {delivery!r}")
            api.recheck_delivery(delivery_id)
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


@SUITE.case("ABS-08")
def shared_acquisition_and_version_hold_survive_restart(ctx, scenario_factory):
    """Real worker + HTTPS source + ABS; no direct-acquisition shortcut."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    def operation() -> dict[str, object]:
        with scenario_factory("ABS-08") as scenario:
            if scenario.abs_client is None:
                raise AssertionError("Audiobookshelf destination is missing.")
            api = scenario.api
            assert api.set_provider_enabled("gutendex", False).status == 200
            _sync_to_completion(api)
            readers = [api.create_reader(f"abs06-reader-{i}@example.test", "Lab-Reader-2026!Pass")
                       for i in range(2)]
            work_id = api.resolve_demo_work()
            barrier = Barrier(2)

            def submit(reader):
                barrier.wait(timeout=10)
                response = reader.create_request(work_id, ["Audiobook"])
                assert response.status == 201, f"Shared request returned HTTP {response.status}"
                return response.body

            with ThreadPoolExecutor(max_workers=2) as pool:
                requests = list(pool.map(submit, readers))
            request_id = requests[0]["id"]
            assert requests[1]["id"] == request_id, "Concurrent members created two requests"
            assert requests[0]["formats"][0]["formatId"] == requests[1]["formats"][0]["formatId"]

            review = readers[1].create_request(work_id, ["Audiobook"], version_kind="Narration",
                                               version_details="A different narrator; administrator must verify credits.")
            assert review.status == 201, f"Version request returned HTTP {review.status}"
            review_id = review.body["id"]
            assert review_id != request_id
            repeated = readers[0].create_request(work_id, ["Audiobook"], version_kind="Narration",
                                                 version_details="A different narrator; administrator must verify credits.")
            assert repeated.status == 201 and repeated.body["id"] == review_id
            _enable_gutendex(api)
            # A disabled-provider pass may already have moved the ordinary request to review.
            assert api.recheck_requests().status == 200
            # Startup runs the real automatic worker immediately, then every two minutes.
            scenario.restart_service("family-librarian")
            scenario.reauthenticate()
            delivery = _poll_delivery(api, scenario.abs_client, request_id, timeout_seconds=180)
            assert delivery is not None, "Healthy provider failed the ordinary-request positive control"
            item_id = delivery["externalItemId"]
            assert _bundle_track_sequences(scenario.abs_client.audio_track_filenames(item_id)) == [1, 2, 3]
            assert api.provider_attempts(request_id), "Ordinary request has no automatic provider evidence"

            scenario.restart_service("family-librarian")
            scenario.reauthenticate()
            assert api.recheck_requests().status == 200
            held_before = api.admin_request(review_id)["request"]
            bypass = api.admin_transition_request(review_id, "PendingAcquisition",
                                                   expected_version=held_before["version"])
            assert bypass.status == 400, "Individual admin requeue bypassed manual version review"
            # Observe more than a complete automatic-worker interval after restart/recheck.
            deadline = time.monotonic() + 130
            while True:
                held = api.admin_request(review_id)["request"]
                assert held["status"] == "NeedsReview", "Version review escaped the admin queue"
                assert held["requiresManualFulfillment"] is True
                assert api.provider_attempts(review_id) == [], "Version review reached an automatic provider"
                queue = api.publishing_queue()
                assert not any(row.get("requestId") == review_id for row in queue.get("deliveries", []))
                if time.monotonic() >= deadline:
                    break
                time.sleep(2)
            assert scenario.abs_client.find_items("The Hobbit", "J. R. R. Tolkien") == [item_id], \
                "Shared request or held version produced a duplicate destination item"
            deliveries = [row for row in api.publishing_queue().get("deliveries", [])
                          if row.get("requestId") == request_id]
            assert len(deliveries) == 1, "Shared request produced multiple deliveries"
            # Existing member cookies and their shared membership must survive both restarts.
            for reader in readers:
                mine = reader.my_requests()
                rows = [row for group in mine.values() if isinstance(group, list) for row in group]
                ordinary = [row for row in rows if row.get("id") == request_id]
                assert len(ordinary) == 1 and ordinary[0]["status"] == "Available"
                assert any(row.get("id") == review_id for row in rows)
            return {"request_id": request_id, "review_id": review_id, "abs_item_id": item_id,
                    "review_observation_seconds": 130, "participant_count": 2}

    _run(ctx, "ABS-08", operation)
