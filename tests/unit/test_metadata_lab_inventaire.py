"""Inventaire adapter against the real samples saved in Phase 0/5."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import Operation, Query
from metadata_lab.providers.inventaire import InventaireAdapter

SAMPLES = Path(__file__).resolve().parents[2] / "metadata_lab" / "samples" / "inventaire"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class InventaireTransport:
    """Routes each request to the matching saved response.

    The multi-uri claims response is assembled from the saved samples (the work
    entity and two label-only entities); no value is invented.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def _merged_claims(self) -> str:
        work = json.loads(sample("series_for_book__fourth-wing.json"))
        labels = json.loads(sample("member_labels__the-empyrean.json"))
        entities = dict(labels["entities"])
        entities.update(work["entities"])
        return json.dumps({"entities": entities, "redirects": {}})

    def send(self, method, url, *, params, headers, body, timeout):
        params = dict(params or {})
        self.requests.append((url, params))
        if "/search" in url:
            if params.get("types") == "humans":
                return TransportResponse(200, {}, sample("author_resolve__rebecca-yarros.json"))
            if params.get("types") == "series":
                return TransportResponse(200, {}, sample("series_search__the-empyrean.json"))
            return TransportResponse(200, {}, sample("book_by_title__fourth-wing.json"))
        if "/entities/serie-parts" in url:
            return TransportResponse(200, {}, sample("series_members__the-empyrean.json"))
        if "/entities/author-works" in url:
            return TransportResponse(200, {}, sample("author_works__rebecca-yarros.json"))
        if "/entities/by-uris" in url:
            uris = params.get("uris")
            assert isinstance(uris, str), "uris must be one pipe-joined string"
            uris = uris.split("|")
            wanted = {str(uri) for uri in uris if uri}
            attributes = str(params.get("attributes") or "")
            if any(uri.startswith("isbn:") for uri in wanted):
                return TransportResponse(200, {}, sample("book_by_isbn__9781649374042.json"))
            if wanted == {"wd:Q124618108"}:
                return TransportResponse(200, {}, sample("series_labels__the-empyrean.json"))
            if wanted == {"wd:Q121091992", "wd:Q124618108"}:
                return TransportResponse(200, {}, sample("labels__author-series.json"))
            if "claims" in attributes:
                return TransportResponse(200, {}, self._merged_claims())
            return TransportResponse(200, {}, sample("member_labels__the-empyrean.json"))
        raise AssertionError(f"unexpected URL in test: {url}")


def make_ctx(transport: InventaireTransport):
    client = HttpClient(provider_id="inventaire", transport=transport, min_interval_s=0.0)
    return client.context()


ADAPTER = InventaireAdapter()


def test_book_by_title_returns_work_records():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.book_by_title(ctx, Query(title="Fourth Wing"))

    assert records[0].title == "Fourth Wing"
    assert records[0].provider_id == "wd:Q120798796"
    assert records[0].record_level == "work"
    assert records[0].work_type == "unknown"
    assert records[0].source_url.startswith("https://inventaire.io/entity/")
    # The search hit is itself the work, so its claims were followed.
    assert records[0].authors == ["Rebecca Yarros"]
    assert records[0].series[0].name == "The Empyrean"


def test_book_by_isbn_normalises_the_edition_claims():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.book_by_isbn(ctx, Query(isbn="978-1-64937-404-2"))

    assert len(records) == 1
    record = records[0]
    assert record.title == "Fourth Wing"
    assert record.isbn13 == ["9781649374042"]
    assert record.isbn10 == ["1649374046"]
    assert record.published == "2023"
    assert record.record_level == "edition"
    assert record.identifiers["work"] == "wd:Q120798796"
    # P407 is a Wikidata entity uri, so no comparable ISO language is claimed.
    assert record.language is None
    # The edition's wdt:P629 work was followed for wdt:P50 / wdt:P179.
    assert record.authors == ["Rebecca Yarros"]
    assert record.author_ids == ["wd:Q121091992"]
    assert [(ref.name, ref.position) for ref in record.series] == [("The Empyrean", "1")]
    # edition + work claims + one batched label call, well inside the budget.
    assert len(ctx.exchanges) == 3


def test_series_for_book_reads_the_p179_claim():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.series_for_book(ctx, Query(isbn="9781649374042"))

    assert [record.name for record in records] == ["The Empyrean"]
    assert records[0].provider_id == "wd:Q124618108"


def test_series_members_returns_ordinals_and_titles():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.series_members(ctx, Query(series="The Empyrean"))

    members = records[0].members
    assert [member.position for member in members] == ["1", "2", "3"]
    assert [member.title for member in members] == ["Fourth Wing", "Iron Flame", "Onyx Storm"]
    assert ctx.truncated is False


def test_author_resolve_returns_humans():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.author_resolve(ctx, Query(author="Rebecca Yarros"))

    assert records[0].provider_id == "wd:Q121091992"
    assert records[0].name == "Rebecca Yarros"


def test_author_works_lists_work_titles():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.author_works(ctx, Query(author="Rebecca Yarros", provider_id="wd:Q121091992"))

    assert records[0].works[0] == "Fourth Wing"
    assert len(records[0].works) == 8


def test_author_series_lists_series_names():
    ctx = make_ctx(InventaireTransport())

    records = ADAPTER.author_series(ctx, Query(author="Rebecca Yarros", provider_id="wd:Q121091992"))

    assert [ref.name for ref in records[0].series] == ["The Empyrean"]


def test_check_connection_reports_a_count():
    ctx = make_ctx(InventaireTransport())

    assert "responded" in ADAPTER.check_connection(ctx)


def test_no_operation_is_declared_unsupported():
    assert ADAPTER.unsupported == {}
    assert ADAPTER.implements(Operation.AUTHOR_SERIES) is True


def test_many_uris_are_sent_pipe_joined_not_as_a_repeated_parameter():
    from metadata_lab.providers.inventaire import _pipe

    uris = [f"wd:Q{n}" for n in range(1, 41)]
    assert _pipe(uris) == "|".join(uris)
    assert _pipe(["wd:Q1"]) == "wd:Q1"


def test_label_prefers_english_then_latin_script_over_an_arbitrary_language():
    from metadata_lab.providers.inventaire import _label

    assert _label({"labels": {"am": "ሃሪ ፖተር", "fr": "Harry Potter", "en": "HP"}}) == "HP"
    assert _label({"labels": {"am": "ሃሪ ፖተር", "fr": "Harry Potter"}}) == "Harry Potter"
    assert (
        _label({"labels": {"am": "ሃሪ ፖተር", "tr": "Harry Potter serisi", "mul": "Harry Potter"}})
        == "Harry Potter"
    )
    assert _label({"labels": {"am": "ሃሪ ፖተር"}}) == "ሃሪ ፖተር"


def test_series_members_labels_every_kept_part_not_just_three():
    from metadata_lab.providers.inventaire import MAX_MEMBER_TITLE_LOOKUPS

    assert MAX_MEMBER_TITLE_LOOKUPS >= 29  # Jack Ryan has 29 parts and must not be truncated
