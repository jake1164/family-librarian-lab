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


def test_parse_row_counts_reads_psql_output():
    from family_librarian_lab.commands import _parse_row_counts

    assert _parse_row_counts("catalog_books|79539\ncatalog_formats|2028231\n\n") == {
        "catalog_books": 79539,
        "catalog_formats": 2028231,
    }
    assert _parse_row_counts("") == {}


def test_gutenberg_is_kept_in_the_database_not_in_a_removed_volume():
    from family_librarian_lab.commands import CLEAN_BOOKS_VOLUMES, GUTENBERG_SCHEMA

    # The catalogue lives inside postgres-data, so clean-books must dump/restore it rather than assume it survives.
    assert GUTENBERG_SCHEMA == "gutenberg"
    assert "postgres-data" in CLEAN_BOOKS_VOLUMES
