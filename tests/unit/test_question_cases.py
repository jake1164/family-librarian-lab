"""The Phase 5 case helpers: they must fail clearly when a fixture lands on the wrong path."""

from __future__ import annotations

import pytest

from family_librarian_lab import question_cases as q


class FakeAdmin:
    def __init__(self, reviews: list[dict | None], status: str = "NeedsReview") -> None:
        self._reviews = reviews
        self._status = status
        self.calls = 0

    def admin_request(self, request_id: str) -> dict:
        review = self._reviews[min(self.calls, len(self._reviews) - 1)]
        self.calls += 1
        return {"request": {"id": request_id, "status": self._status, "needsReview": review, "version": 3}}

    def provider_attempts(self, request_id: str) -> list[dict]:
        return [{"outcome": "NoMatch"}]


def _review(kind: str | None, candidates: int = 1) -> dict:
    return {
        "category": "PreferenceAmbiguity",
        "questionKind": kind,
        "candidates": [{"candidateId": f"c{i}"} for i in range(candidates)],
    }


def test_wait_for_question_returns_the_matching_review(monkeypatch):
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    admin = FakeAdmin([None, _review("CollectionContaining")])
    assert q.wait_for_question(admin, "r1", "CollectionContaining")["questionKind"] == "CollectionContaining"


def test_a_different_question_kind_fails_immediately_not_after_the_timeout(monkeypatch):
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    with pytest.raises(AssertionError, match="Expected a CollectionContaining question but the request asked 'AbridgedOnly'"):
        q.wait_for_question(FakeAdmin([_review("AbridgedOnly")]), "r1", "CollectionContaining")


def test_no_question_reports_what_the_request_looks_like(monkeypatch):
    ticks = iter([0.0, 1.0, 1000.0, 1000.0])
    monkeypatch.setattr(q.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    with pytest.raises(AssertionError, match=r"No PartsOnly question appeared within 5s: status='NeedsReview'"):
        q.wait_for_question(FakeAdmin([None]), "r1", "PartsOnly", timeout=5)


def test_a_question_must_offer_exactly_one_candidate():
    assert q.question_candidate_id(_review("PartsOnly")) == "c0"
    with pytest.raises(AssertionError, match="exactly one candidate"):
        q.question_candidate_id(_review("PartsOnly", candidates=2))


def test_answer_passes_the_candidate_only_for_take_it():
    class Api:
        def __init__(self) -> None:
            self.sent: list[tuple] = []

        def resolve_needs_review(self, request_id, *, candidate_id, always_accept):
            self.sent.append((request_id, candidate_id, always_accept))
            return "ok"

    api = Api()
    q.answer(api, "r1", _review("CollectionContaining"), take=True, always=True)
    q.answer(api, "r1", _review("CollectionContaining"), take=False)
    assert api.sent == [("r1", "c0", True), ("r1", None, False)]


def test_question_notifications_are_filtered_by_category_and_request():
    class Api:
        def notifications(self):
            return [
                {"category": q.QUESTION_CATEGORY, "subjectId": "r1", "repeatCount": 1},
                {"category": q.QUESTION_CATEGORY, "subjectId": "r2", "repeatCount": 1},
                {"category": "request.needs_review", "subjectId": "r1", "repeatCount": 1},
            ]

    assert len(q.question_notifications(Api(), "r1")) == 1


def test_every_seeded_request_names_a_fixture_work_and_media_type():
    slugs = [slug for slug, _, _ in q.SEED_REQUESTS]
    assert len(slugs) == len(set(slugs))
    assert {media for _, media, _ in q.SEED_REQUESTS} == {"Ebook", "Audiobook"}
    assert q.CINDER_GATE in slugs and q.STANDALONE_CINDER not in slugs
