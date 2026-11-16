"""The local UI server, over real HTTP on an ephemeral port.

Fake adapters and a temporary results directory: no network, no keys, nothing
written into the checkout. The security properties are asserted here because
they are the reason the server may hold a credential at all.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from metadata_lab import cli, serve, serve_data
from metadata_lab.cases import load_suite_file
from metadata_lab.model import ErrorKind, ProviderError, SeriesMember, SeriesRecord
from metadata_lab.providers.base import ProviderAdapter
from metadata_lab.serve import MAX_BODY, ServerConfig, create_server

PLANTED = "planted-secret-9c1f0b"
KEY_ENV = "FL_METADATA_LAB_TEST_KEY"

SUITE_YAML = """
suite: fake-suite
version: 1
providers: all
as_of: 2026-01-02
cases:
  - id: c1
    description: "first fake case"
    reference: {source: "unit test fixture"}
    input: {series: "Fake Series"}
    operations: [series_members]
  - id: c2
    description: "second fake case"
    reference: {source: "unit test fixture"}
    input: {series: "Other Series"}
    operations: [series_members]
"""


def _records(name: str) -> list[SeriesRecord]:
    return [
        SeriesRecord(
            provider_id="fake-keyless",
            name=name,
            members=[SeriesMember(title="One", position="1")],
        )
    ]


class FakeKeyless(ProviderAdapter):
    id = "fake-keyless"
    display_name = "Fake Keyless"
    VERIFIED_LIVE = True
    min_interval_s = 0.0

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        return _records(q.series or "Fake Series")

    def check_connection(self, ctx):  # noqa: ANN001, ANN201 - test double
        return "fake keyless ok"


class FakeEmpty(ProviderAdapter):
    id = "fake-empty"
    display_name = "Fake Empty"
    VERIFIED_LIVE = False
    min_interval_s = 0.0

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        return []

    def check_connection(self, ctx):  # noqa: ANN001, ANN201 - test double
        return "fake empty ok"


class FakeKeyed(ProviderAdapter):
    id = "fake-keyed"
    display_name = "Fake Keyed"
    credential_env = KEY_ENV
    VERIFIED_LIVE = False
    min_interval_s = 0.0

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        return _records(q.series or "Fake Series")

    def check_connection(self, ctx):  # noqa: ANN001, ANN201 - test double
        # Worst case on purpose: an error that echoes the credential.
        raise ProviderError(ErrorKind.AUTH, f"HTTP 401 from https://example.test/x?key={ctx.credential}")


class FakeBlocking(ProviderAdapter):
    """Waits inside one operation so the test can cancel or collide with it."""

    id = "fake-blocking"
    display_name = "Fake Blocking"
    VERIFIED_LIVE = False
    min_interval_s = 0.0
    entered = threading.Event()
    release = threading.Event()

    def series_members(self, ctx, q):  # noqa: ANN001, ANN201 - test double
        type(self).entered.set()
        type(self).release.wait(10)
        return _records(q.series or "Fake Series")

    def check_connection(self, ctx):  # noqa: ANN001, ANN201 - test double
        return "fake blocking ok"


ADAPTERS = {
    "fake-empty": FakeEmpty,
    "fake-keyless": FakeKeyless,
    "fake-keyed": FakeKeyed,
    "fake-blocking": FakeBlocking,
}


class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> dict:
        return json.loads(self.body)


class Client:
    def __init__(self, host: str, port: int, results_root: Path) -> None:
        self.host = host
        self.port = port
        self.results_root = results_root

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = "application/json",
        host: str | None = None,
    ) -> Reply:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        headers: dict[str, str] = {}
        if host:
            headers["Host"] = host
        if body is not None and content_type:
            headers["Content-Type"] = content_type
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return Reply(response.status, dict(response.getheaders()), response.read())
        finally:
            connection.close()

    def get(self, path: str, *, host: str | None = None) -> Reply:
        return self.request("GET", path, host=host)

    def post(self, path: str, payload: object | None = None, *, content_type: str = "application/json") -> Reply:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        return self.request("POST", path, body=body, content_type=content_type)

    def job(self, job_id: str) -> dict:
        return self.get(f"/api/jobs/{job_id}").json()["job"]

    def wait(self, job_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["status"] in {"complete", "cancelled", "error"}:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish: {self.job(job_id)}")


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "fake-suite.yaml").write_text(SUITE_YAML, encoding="utf-8")
    monkeypatch.setattr(serve_data, "SUITES_DIR", suites)
    results = tmp_path / "results"
    results.mkdir()
    FakeBlocking.entered = threading.Event()
    FakeBlocking.release = threading.Event()
    server = create_server(
        ServerConfig(
            host="127.0.0.1",
            port=0,
            results_root=results,
            stamp_root=tmp_path / "stamp-root",
            adapters=dict(ADAPTERS),
            quiet=True,
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client("127.0.0.1", server.server_address[1], results)
    try:
        yield client
    finally:
        FakeBlocking.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _oneoff(providers: list[str]) -> dict:
    return {
        "mode": "oneoff",
        "kind": "series",
        "fields": {"series": "Fake Series"},
        "providers": providers,
    }


def _suite(providers: list[str], **extra) -> dict:
    body = {"mode": "suite", "suite": "fake-suite", "providers": providers, "confirmed": True}
    body.update(extra)
    return body


# --- providers and secrets ---------------------------------------------------------


def test_providers_never_contain_a_credential(lab, monkeypatch):
    monkeypatch.setenv(KEY_ENV, PLANTED)

    reply = lab.get("/api/providers")

    assert reply.status == 200
    assert PLANTED not in reply.text
    entries = {entry["id"]: entry for entry in reply.json()["providers"]}
    assert entries["fake-keyed"]["configured"] is True
    assert entries["fake-keyed"]["credential_env"] == KEY_ENV
    for path in ("/api/harness", "/api/suites", "/api/runs", "/"):
        assert PLANTED not in lab.get(path).text


def test_a_check_that_echoes_the_credential_is_redacted(lab, monkeypatch):
    monkeypatch.setenv(KEY_ENV, PLANTED)

    reply = lab.post("/api/check", {"providers": ["fake-keyed"]})

    assert reply.status == 200
    assert PLANTED not in reply.text
    result = reply.json()["results"][0]
    assert result["status"] == "error"
    assert result["kind"] == "auth"


def test_check_reports_an_unconfigured_provider(lab, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)

    reply = lab.post("/api/check", {"providers": ["fake-keyed"]})

    assert reply.status == 200
    assert reply.json()["results"][0]["status"] == "not_configured"


def test_providers_never_publish_a_key_length(lab, monkeypatch):
    monkeypatch.setenv(KEY_ENV, PLANTED)

    text = lab.get("/api/providers").text

    assert str(len(PLANTED)) not in json.dumps(
        [{key: value for key, value in entry.items() if key != "declared"}
         for entry in json.loads(text)["providers"]]
    )


# --- transport guards -------------------------------------------------------------


def test_a_foreign_host_header_is_forbidden(lab):
    reply = lab.get("/api/providers", host="attacker.example.com")

    assert reply.status == 403
    assert "Host" in reply.json()["error"]


def test_localhost_and_the_bound_address_are_allowed(lab):
    assert lab.get("/api/providers", host="localhost").status == 200
    assert lab.get("/api/providers", host=f"127.0.0.1:{lab.port}").status == 200


def test_post_without_a_json_content_type_is_refused(lab):
    reply = lab.post("/api/run", _oneoff(["fake-keyless"]), content_type="text/plain")

    assert reply.status == 415


def test_post_without_a_content_length_is_refused(lab):
    connection = http.client.HTTPConnection(lab.host, lab.port, timeout=10)
    try:
        connection.putrequest("POST", "/api/run")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status in {400, 411}
        response.read()
    finally:
        connection.close()


def test_an_oversized_body_is_refused(lab):
    reply = lab.request(
        "POST", "/api/run", body=b"{" + b"a" * (MAX_BODY + 10), content_type="application/json"
    )

    assert reply.status == 413


def test_bad_json_is_refused(lab):
    reply = lab.request("POST", "/api/run", body=b"not json", content_type="application/json")

    assert reply.status == 400


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/../result.json",
        "/api/runs/../../etc/passwd/result.json",
        "/api/runs/..%2Fresult.json",
        "/api/runs/%2e%2e%2Fresult.json",
        "/api/runs/%2e%2e%2F%2e%2e%2Fetc/result.json",
        "/api/runs/%2Fetc%2Fpasswd/result.json",
        "/api/runs/..%5C..%5Cresult.json",
    ],
)
def test_path_traversal_is_not_found(lab, path):
    assert lab.get(path).status == 404


def test_only_the_three_result_files_are_served(lab):
    assert lab.get("/api/runs/anything/secret.json").status == 404
    assert lab.get("/api/runs/anything/result.json").status == 404


# --- request validation -----------------------------------------------------------


def test_unknown_provider_is_refused(lab):
    reply = lab.post("/api/run", _oneoff(["does-not-exist"]))

    assert reply.status == 400
    assert "does-not-exist" in reply.json()["error"]


def test_unknown_field_is_refused(lab):
    body = _oneoff(["fake-keyless"])
    body["fields"]["url"] = "https://example.test"

    assert lab.post("/api/run", body).status == 400


def test_an_unconfirmed_suite_is_refused(lab):
    body = _suite(["fake-keyless"])
    body.pop("confirmed")

    assert lab.post("/api/run", body).status == 409


def test_a_one_off_needs_the_field_its_type_uses(lab):
    body = {"mode": "oneoff", "kind": "isbn", "fields": {"title": "Fourth Wing"}, "providers": ["fake-keyless"]}

    reply = lab.post("/api/run", body)

    assert reply.status == 400
    assert "isbn" in reply.json()["error"]


# --- cool-down state --------------------------------------------------------------


def _cool_down(lab, provider_id: str, *, minutes: int = 30) -> None:
    """Write a cool-down entry, exactly the shape a tripped run would leave."""

    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    observed = datetime.now(timezone.utc)
    payload = {
        "schema": "metadata-lab.cooldowns/1",
        "providers": {
            provider_id: {
                "provider": provider_id,
                "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": "rate_limited",
                "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        },
    }
    (lab.results_root / "cooldowns.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_cooling_provider_is_listed_with_the_time_left(lab):
    _cool_down(lab, "fake-keyless", minutes=30)

    reply = lab.get("/api/providers")

    assert reply.status == 200
    entries = {entry["id"]: entry for entry in reply.json()["providers"]}
    cooling = entries["fake-keyless"]["cooldown"]
    assert cooling["reason"] == "rate_limited"
    assert cooling["until"].endswith("Z")
    assert 28 <= cooling["minutes_remaining"] <= 30
    assert entries["fake-empty"]["cooldown"] is None


def test_the_provider_list_carries_the_pacing_interval(lab):
    entries = {entry["id"]: entry for entry in lab.get("/api/providers").json()["providers"]}

    assert entries["fake-keyless"]["pacing"]["max_interval_s"] == 0.0
    assert "min_interval_s" in entries["fake-keyless"]["pacing"]


def test_a_run_on_a_cooling_provider_is_refused_without_override(lab):
    _cool_down(lab, "fake-keyless")

    refused = lab.post("/api/run", _oneoff(["fake-keyless"]))

    assert refused.status == 409
    message = refused.json()["error"]
    assert "cooling down" in message and "min remaining" in message
    assert "override" in message


def test_the_override_lets_the_run_start(lab):
    _cool_down(lab, "fake-keyless")

    body = {**_oneoff(["fake-keyless"]), "ignore_cooldown": True}
    started = lab.post("/api/run", body)

    assert started.status == 202
    job = lab.wait(started.json()["job"]["id"])
    assert job["status"] == "complete"
    document = json.loads(
        (lab.results_root / job["run_dir"] / "result.json").read_text(encoding="utf-8")
    )
    assert document["cases"][0]["results"][0]["outcome"] == "ok"


def test_a_corrupt_cooldown_file_does_not_break_the_ui(lab):
    (lab.results_root / "cooldowns.json").write_text("{oops", encoding="utf-8")

    assert lab.get("/api/providers").status == 200
    assert lab.post("/api/run", _oneoff(["fake-keyless"])).status == 202


# --- one run at a time ------------------------------------------------------------

def test_a_second_run_while_one_is_running_is_conflict(lab):
    first = lab.post("/api/run", _oneoff(["fake-blocking"]))
    assert first.status == 202
    assert FakeBlocking.entered.wait(10), "the fake adapter never started"

    second = lab.post("/api/run", _oneoff(["fake-keyless"]))

    assert second.status == 409
    assert "in progress" in second.json()["error"]

    FakeBlocking.release.set()
    assert lab.wait(first.json()["job"]["id"])["status"] == "complete"


# --- a finished run ---------------------------------------------------------------


def test_a_suite_run_writes_three_files_and_completes(lab):
    reply = lab.post("/api/run", _suite(["fake-keyless"]))
    assert reply.status == 202

    job = lab.wait(reply.json()["job"]["id"])

    assert job["status"] == "complete"
    assert job["error"] is None
    assert job["progress"]["rows_done"] == job["progress"]["rows_total"] == 2
    directory = lab.results_root / job["run_dir"]
    for name in ("result.json", "ai.json", "report.html"):
        assert (directory / name).is_file(), name
    document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert document["run"]["status"] == "complete"
    assert document["suite"]["name"] == "fake-suite"
    assert [case["id"] for case in document["cases"]] == ["c1", "c2"]
    assert job["files"]["report.html"] == f"/api/runs/{job['run_dir']}/report.html"


def test_a_suite_run_can_be_filtered_by_case_id(lab):
    reply = lab.post("/api/run", _suite(["fake-keyless"], case_ids=["c2"]))

    job = lab.wait(reply.json()["job"]["id"])

    document = json.loads((lab.results_root / job["run_dir"] / "result.json").read_text(encoding="utf-8"))
    assert [case["id"] for case in document["cases"]] == ["c2"]


def test_a_one_off_run_completes(lab):
    reply = lab.post("/api/run", _oneoff(["fake-keyless"]))

    job = lab.wait(reply.json()["job"]["id"])

    assert job["status"] == "complete"
    assert job["suite"] == "one-off-series"
    assert (lab.results_root / job["run_dir"] / "report.html").is_file()


def test_cancel_keeps_a_partial_result(lab):
    reply = lab.post("/api/run", _suite(["fake-blocking"]))
    job_id = reply.json()["job"]["id"]
    assert FakeBlocking.entered.wait(10), "the fake adapter never started"

    assert lab.post(f"/api/jobs/{job_id}/cancel", {}).status == 200
    FakeBlocking.release.set()
    job = lab.wait(job_id)

    assert job["status"] == "cancelled"
    document = json.loads((lab.results_root / job["run_dir"] / "result.json").read_text(encoding="utf-8"))
    assert document["run"]["status"] == "cancelled"
    outcomes = [row["outcome"] for case in document["cases"] for row in case["results"]]
    assert outcomes[0] == "ok"
    assert "not_run" in outcomes
    assert (lab.results_root / job["run_dir"] / "report.html").is_file()
    listed = next(item for item in lab.get("/api/runs").json()["runs"] if item["id"] == job["run_dir"])
    assert listed["status"] == "cancelled"
    assert listed["partial"] is True


# --- listing, comparing, page -----------------------------------------------------


def test_runs_are_listed_and_comparable(lab):
    first = lab.wait(lab.post("/api/run", _oneoff(["fake-keyless"])).json()["job"]["id"])
    second = lab.wait(lab.post("/api/run", _oneoff(["fake-empty"])).json()["job"]["id"])

    listing = lab.get("/api/runs").json()["runs"]

    ids = {entry["id"] for entry in listing}
    assert {first["run_dir"], second["run_dir"]} <= ids
    entry = next(item for item in listing if item["id"] == first["run_dir"])
    assert entry["suite"] == "one-off-series"
    assert entry["providers"] == ["fake-keyless"]
    assert entry["status"] == "complete"
    assert entry["harness"] == "not_run"
    assert entry["partial"] is False
    assert entry["partial_rows"] == 0

    diff = lab.get(f"/api/compare?a={first['run_dir']}&b={second['run_dir']}").json()

    assert diff["semantic_equal"] is False
    kinds = {change["kind"] for change in diff["changes"]}
    assert {"new_provider", "removed_provider"} <= kinds
    assert diff["a"]["id"] == first["run_dir"]
    assert diff["b"]["id"] == second["run_dir"]
    assert diff["semantic_changes"]


def test_compare_needs_two_known_runs(lab):
    assert lab.get("/api/compare?a=missing&b=missing").status == 404
    assert lab.get("/api/compare").status == 400


def test_the_page_and_suite_catalogue_are_served(lab):
    page = lab.get("/")

    assert page.status == 200
    assert "text/html" in page.headers["Content-Type"]
    assert "Metadata comparison lab" in page.text

    catalogue = lab.get("/api/suites").json()
    assert [suite["name"] for suite in catalogue["suites"]] == ["fake-suite"]
    case = catalogue["suites"][0]["cases"][0]
    assert case["id"] == "c1"
    assert case["operations"] == ["series_members"]
    assert case["max_calls"] == 10


def test_save_as_case_downloads_a_loadable_suite(lab, tmp_path):
    reply = lab.post("/api/case", {"kind": "series", "fields": {"series": "The Empyrean"}})

    assert reply.status == 200
    assert "yaml" in reply.headers["Content-Type"]
    assert "attachment" in reply.headers["Content-Disposition"]
    path = tmp_path / "saved.yaml"
    path.write_text(reply.text, encoding="utf-8")
    suite = load_suite_file(path)
    assert suite.cases[0].input == {"series": "The Empyrean"}
    assert [operation.value for operation in suite.cases[0].operations] == ["series_members"]


def test_harness_status_is_reported(lab):
    payload = lab.get("/api/harness").json()

    assert payload["status"] == "not_run"
    assert payload["digest"]
    assert payload["ran_at"] is None


def test_selftest_reports_thirty_results(lab):
    reply = lab.post("/api/selftest", {})

    assert reply.status == 200
    payload = reply.json()
    assert len(payload["results"]) == 30
    assert payload["passed"] == 30, payload["results"]
    assert payload["harness"]["status"] == "validated"
    assert all(set(item) >= {"id", "ok", "title", "detail"} for item in payload["results"])


# --- the CLI gate -----------------------------------------------------------------


def test_a_non_loopback_host_needs_allow_remote(capsys):
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2

    captured = capsys.readouterr()
    assert "--allow-remote" in captured.err
    assert "0.0.0.0" in captured.err


def test_is_loopback_understands_names_and_addresses():
    assert serve.is_loopback("127.0.0.1")
    assert serve.is_loopback("127.1.2.3")
    assert serve.is_loopback("localhost")
    assert serve.is_loopback("::1")
    assert not serve.is_loopback("0.0.0.0")
    assert not serve.is_loopback("example.test")


def test_only_one_self_test_or_run_at_a_time(lab):
    first = lab.post("/api/run", _oneoff(["fake-blocking"]))
    assert FakeBlocking.entered.wait(10)

    reply = lab.post("/api/selftest", {})

    assert reply.status == 409
    FakeBlocking.release.set()
    lab.wait(first.json()["job"]["id"])


def test_the_job_reports_its_progress(lab, monkeypatch):
    reply = lab.post("/api/run", _suite(["fake-keyless"]))

    job = lab.wait(reply.json()["job"]["id"])

    assert job["progress"]["rows_total"] == 2
    assert job["progress"]["calls"] == 0  # the fake adapters make no HTTP calls
    assert set(job) >= {"id", "kind", "status", "progress", "files", "run_dir"}


def test_an_unknown_job_is_not_found(lab):
    assert lab.get("/api/jobs/000000000000").status == 404
    assert lab.post("/api/jobs/000000000000/cancel", {}).status == 404


def test_the_page_is_a_single_static_file():
    page = Path(serve.PAGE).read_text(encoding="utf-8")

    assert "<script" in page
    assert "<script src=" not in page  # no build step, no external asset
    assert "<link rel=\"stylesheet\"" not in page
    assert "sandbox" in page
    assert "no overall score" in page.lower()


def test_yaml_dump_of_a_case_is_neutral():
    text = serve_data.case_yaml("book", {"title": "A Book", "author": "An Author"})

    assert yaml.safe_load(text)["cases"][0]["input"] == {"title": "A Book", "author": "An Author"}
