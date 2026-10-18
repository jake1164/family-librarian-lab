"""Exercise the disposable fixture over real HTTP, including its negative target guard."""

from __future__ import annotations

from contextlib import contextmanager
import json
from http.client import HTTPConnection
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "se-lab"))
from family_librarian_lab.clients import SearchReviewControl, _http
from family_librarian_lab.search_review_fixture import ALIAS, TITLE, make_server


@contextmanager
def running_fixture():
    server = make_server(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def call(base, path, body=None):
    status, raw = _http(base + path, method="GET" if body is None else "POST", json_body=body)
    return status, json.loads(raw) if raw else None


def test_sources_are_distinct_and_only_offer_the_verified_copy_after_the_alias_search():
    with running_fixture() as base:
        control = SearchReviewControl(base)
        control.configure("verified")
        for source in ("source-a", "source-b"):
            status, manifest = call(base, f"/{source}/manifest")
            assert status == 200 and manifest["id"] == source and manifest["protocolVersions"] == ["2"]
            assert call(base, f"/{source}/search", {"work": {"title": TITLE}})[1]["candidates"] == []
            copy = call(base, f"/{source}/search", {"work": {"title": ALIAS}})[1]["candidates"][0]
            assert copy["edition"]["identifiers"] == [{"scheme": "isbn13", "value": "9791000000022"}]
        assert call(base, "/source-a/acquire", {"candidateReference": "invented"})[0] == 404
        status, job = call(base, "/source-a/acquire", {"candidateReference": "verified-copy"})
        assert status == 202 and job["state"] == "running"
        assert call(base, f"/source-a/acquire/{job['jobId']}")[1] == job
        control.configure("role-off")
        assert control.state() == {"scenario": "role-off", "searches": [], "modelCalls": [], "acquisitions": []}


def test_source_review_records_have_no_structured_title_or_author_and_retain_two_releases():
    with running_fixture() as base:
        SearchReviewControl(base).configure("source-review")
        candidates = call(base, "/source-a/search", {"work": {"title": TITLE}})[1]["candidates"]
        assert len(candidates) == 2
        assert all(row["work"]["title"] is None and row["work"]["authors"] == [] for row in candidates)
        assert candidates[0]["release"]["name"] != candidates[1]["release"]["name"]


def test_wrong_work_is_a_title_collision_with_a_conflicting_author_and_no_matching_isbn():
    with running_fixture() as base:
        SearchReviewControl(base).configure("wrong-work")
        copy = call(base, "/source-a/search", {"work": {"title": ALIAS}})[1]["candidates"][0]
        assert copy["work"]["title"] == TITLE
        assert copy["work"]["authors"][0]["name"] == "Other Writer"
        assert copy["edition"]["identifiers"] == []


@pytest.mark.parametrize("mode,value,ids", [
    ("verified", ALIAS, ["req-title"]), ("duplicate", TITLE, ["req-title"]),
    ("invented-id", ALIAS, ["r9999"]), ("url", "https://example.test/book", ["req-title"]),
    ("too-long", "x" * 121, ["req-title"]),
])
def test_model_outputs_reach_the_intended_parser_boundary(mode, value, ids):
    with running_fixture() as base:
        control = SearchReviewControl(base)
        control.configure(mode)
        status, response = call(base, "/v1/chat/completions", {"messages": []})
        reply = json.loads(response["choices"][0]["message"]["content"])
        assert status == 200 and reply["schema"] == "search-guidance/1"
        assert reply["suggestions"][0]["value"] == value
        assert reply["suggestions"][0]["evidence_ids"] == ids
        assert len(control.state()["modelCalls"]) == 1


def test_unknown_fixture_configuration_is_rejected():
    with running_fixture() as base:
        assert call(base, "/test/configure", {"scenario": "typo"})[0] == 400


def test_streamed_provider_requests_decode_chunked_json_like_dotnet_httpclient():
    with running_fixture() as base:
        control = SearchReviewControl(base)
        control.configure("verified")
        connection = HTTPConnection(base.removeprefix("http://"))
        try:
            raw = json.dumps({"work": {"title": ALIAS}}).encode()
            connection.request("POST", "/source-a/search", body=iter([raw[:10], raw[10:]]),
                               headers={"Content-Type": "application/json"}, encode_chunked=True)
            response = connection.getresponse()
            body = json.loads(response.read())
            assert response.status == 200 and body["candidates"][0]["providerReference"] == "verified-copy"
        finally:
            connection.close()
        assert control.state()["searches"][0]["body"]["work"]["title"] == ALIAS
