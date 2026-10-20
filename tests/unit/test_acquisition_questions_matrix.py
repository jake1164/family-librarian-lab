"""Q-10 must inspect every event in a sync batch before the client advances its cursor."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def question_suite(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "se-lab"))
    spec = importlib.util.spec_from_file_location("q10_suite_under_test", root / "tests/test_acquisition_questions_matrix.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _batched_client(question_suite, monkeypatch, messages, *, room="!dm:example.test"):
    client = question_suite.clients.MatrixTestClient("http://matrix.example.test", "example.test")
    requests = []

    def sync(*, since=None, timeout_ms=0):
        requests.append(since)
        events = [{"type": "m.room.message", "content": {"msgtype": "m.text", "body": body}} for body in messages] if since is None else []
        client._next_batch = "after-the-whole-batch"
        return {"next_batch": client._next_batch, "rooms": {"join": {room: {"timeline": {"events": events}}}}}

    monkeypatch.setattr(client, "sync", sync)
    return client, requests


@pytest.mark.parametrize("messages,needle,expected", [
    (["329847", "Your Matrix account is verified.", "Choose a copy: TAKE or KEEP LOOKING."], "keep looking", "Choose a copy: TAKE or KEEP LOOKING."),
    (["take it", "A status update.", "I'm fetching the collection."], "fetching", "I'm fetching the collection."),
    (["Earlier chatter.", "Please Keep Looking for a different copy."], "KEEP LOOKING", "Please Keep Looking for a different copy."),
])
def test_expected_question_or_ack_is_not_lost_after_an_earlier_event_in_the_same_batch(question_suite, monkeypatch, messages, needle, expected):
    client, requests = _batched_client(question_suite, monkeypatch, messages)
    assert question_suite._wait_for_dm(client, "!dm:example.test", needle, timeout=0.01) == expected
    assert requests == [None], "The matching event was already present in the first batch."
    assert client._next_batch == "after-the-whole-batch"


def test_a_message_in_another_room_cannot_satisfy_the_question_wait(question_suite, monkeypatch):
    client, _ = _batched_client(question_suite, monkeypatch, ["KEEP LOOKING"], room="!other:example.test")
    with pytest.raises(AssertionError, match="never sent a message containing 'keep looking'"):
        question_suite._wait_for_dm(client, "!dm:example.test", "keep looking", timeout=0.001)


def test_missing_keyword_still_fails_with_the_observed_chatter(question_suite, monkeypatch):
    client, _ = _batched_client(question_suite, monkeypatch, ["A status update without the requested answer."])
    with pytest.raises(AssertionError, match="A status update without the requested answer"):
        question_suite._wait_for_dm(client, "!dm:example.test", "keep looking", timeout=0.001)
