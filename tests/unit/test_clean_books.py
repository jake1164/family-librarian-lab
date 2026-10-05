"""`lab clean-books` must only ever target Family Librarian's own volumes."""

from __future__ import annotations

from family_librarian_lab.commands import CLEAN_BOOKS_SERVICES, _clean_books_volume_names


def test_clean_books_targets_only_database_and_storage_volumes():
    assert _clean_books_volume_names("family-librarian-lab") == [
        "family-librarian-lab_postgres-data",
        "family-librarian-lab_family-librarian-data",
    ]


def test_clean_books_never_touches_other_services():
    assert set(CLEAN_BOOKS_SERVICES) == {"family-librarian", "migrate", "postgres"}
