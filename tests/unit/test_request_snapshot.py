"""`lab clean-books --requeue-all/--requeue-todo`: what is saved, and which formats are replayed."""

from __future__ import annotations

from datetime import UTC, datetime

from family_librarian_lab import request_snapshot as rs


def _req(work, title, email, formats, **extra):
    return {
        "request": {"workId": work, "workTitle": title, "authors": ["A"], "formats": [
            {"mediaType": m, "status": s} for m, s in formats], **extra},
        "requesterEmail": email,
    }


SRC = {"w1": ("openlibrary", "OL1W"), "w2": ("hardcover", "9"), "w3": None}


def test_duplicates_merge_per_requester_and_work_and_best_status_wins():
    built = rs.build_entries([
        _req("w1", "Dune", "a@x", [("Ebook", "Cancelled")]),
        _req("w1", "Dune", "a@x", [("Ebook", "Requested"), ("Audiobook", "Available")]),
        _req("w1", "Dune", "b@x", [("Ebook", "Available")]),
    ], SRC)
    assert [(e.requester_email, e.formats) for e in built.entries] == [
        ("a@x", {"Ebook": "Requested", "Audiobook": "Available"}),
        ("b@x", {"Ebook": "Available"}),
    ]


def test_work_without_source_is_reported_not_saved():
    built = rs.build_entries([_req("w3", "Lost", "a@x", [("Ebook", "Requested")])], SRC)
    assert built.entries == [] and [u.title for u in built.unresolved] == ["Lost"]


def test_todo_replays_only_unfinished_formats_and_drops_done_or_cancelled_books():
    built = rs.build_entries([
        _req("w1", "Mixed", "a@x", [("Ebook", "Available"), ("Audiobook", "NotAvailable")]),
        _req("w2", "Done", "a@x", [("Ebook", "Available")]),
        _req("w2", "Done", "b@x", [("Ebook", "Cancelled")]),
    ], SRC)
    todo = rs.plan_replay(built.entries, todo_only=True)
    assert [(i.entry.title, i.media_types) for i in todo] == [("Mixed", ["Audiobook"])]
    everything = rs.plan_replay(built.entries, todo_only=False)
    assert [(i.entry.title, i.media_types) for i in everything] == [
        ("Mixed", ["Audiobook", "Ebook"]), ("Done", ["Ebook"]), ("Done", ["Ebook"])]


def test_empty_list_never_overwrites_and_snapshots_are_pruned(tmp_path):
    assert rs.write_snapshot(tmp_path, []) is None and rs.latest_snapshot(tmp_path) is None
    built = rs.build_entries([_req("w1", "Dune", "a@x", [("Ebook", "Requested")], note="n")], SRC)
    for second in range(rs.SNAPSHOT_KEEP + 3):
        rs.write_snapshot(tmp_path, built.entries, now=datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC))
    assert len(list(tmp_path.glob("requests-*.json"))) == rs.SNAPSHOT_KEEP
    loaded = rs.read_snapshot(rs.latest_snapshot(tmp_path))
    assert loaded[0].provider_id == "openlibrary" and loaded[0].note == "n"


def test_confirmation_prompt_says_what_happens_to_requests():
    import argparse

    from family_librarian_lab.commands import _clean_books_request_plan

    def plan(**flags):
        return " ".join(_clean_books_request_plan(argparse.Namespace(**{"requeue_all": False, "requeue_todo": False, **flags})))

    assert "Nothing will be requested again" in plan()
    assert "EVERY saved book" in plan(requeue_all=True)
    assert "NOT Available" in plan(requeue_todo=True)
