"""Negative assertions must wait for real work; an idle fixture is not proof."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "se-lab"))
spec = spec_from_file_location("search_review_barrier_cases", ROOT / "tests/test_search_review.py")
cases = module_from_spec(spec)
spec.loader.exec_module(cases)


class Api:
    def __init__(self, attempts, review=None):
        self.attempts = iter(attempts)
        self.review = review
        self.polls = 0

    def provider_attempts(self, _request_id):
        self.polls += 1
        return next(self.attempts)

    def admin_request(self, _request_id):
        return {"request": {"needsReview": self.review}}


class Control:
    def __init__(self, sources=("source-a", "source-b"), acquisitions=()):
        self.sources = sources
        self.acquisitions = acquisitions

    def state(self):
        return {"searches": [{"source": source} for source in self.sources], "acquisitions": list(self.acquisitions)}


A = {"providerId": "source-a"}
B = {"providerId": "source-b"}


def test_a_quiet_fixture_does_not_pass_before_provider_attempts_are_persisted(monkeypatch):
    api = Api([[], [A], [A, B]])
    sleeps = []
    monkeypatch.setattr(cases.time, "sleep", sleeps.append)
    cases._wait(api, "request", Control(), "role-off")
    assert api.polls == 3 and sleeps == [1, 1]


def test_ordinary_review_is_terminal_after_its_first_source():
    api = Api([[A]], review={"category": "PreferenceAmbiguity"})
    cases._wait(api, "request", Control(sources=("source-a",)), "source-review")
    assert api.polls == 1


def test_guidance_review_can_stop_processing_after_both_sources_were_searched():
    api = Api([[A]], review={"category": "PreferenceAmbiguity"})
    cases._wait(api, "request", Control(), "wrong-work")
    assert api.polls == 1


def test_the_positive_fixture_fails_immediately_when_both_sources_finish_without_a_job():
    api = Api([[A, B]])
    with pytest.raises(AssertionError, match="completed without acquisition"):
        cases._wait(api, "request", Control(), "verified")
