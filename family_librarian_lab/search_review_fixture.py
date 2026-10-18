"""Disposable protocol-v2 sources and scripted model. No network calls or book downloads.

Run only in the lab's isolated search-review profile. The app, gateway parser,
matcher, database and scheduler remain real. Acquisitions stop at a running
fixture job: these cases do not claim to test file validation or publishing.
"""

from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from uuid import uuid4

TITLE = "Cinder Gate"
AUTHOR = "Mara Quillon"
ALIAS = "The Cinder Road"
SCENARIOS = {"source-review", "verified", "wrong-work", "duplicate", "role-off", "give-up", "invented-id", "url", "too-long"}


class Fixture:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.instance_id = str(uuid4())
        self.reset("role-off")

    def reset(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("Unknown fixture scenario")
        with self.lock:
            self.scenario = scenario
            self.searches: list[dict] = []
            self.model_calls: list[dict] = []
            self.acquisitions: list[dict] = []
            self.jobs: dict[str, dict] = {}
            self.offered: set[tuple[str, str]] = set()

    def state(self) -> dict:
        with self.lock:
            return deepcopy({"scenario": self.scenario, "searches": self.searches,
                             "modelCalls": self.model_calls, "acquisitions": self.acquisitions})

    def search(self, source: str, body: dict) -> dict:
        with self.lock:
            self.searches.append({"source": source, "body": deepcopy(body)})
            title = body.get("work", {}).get("title", "")
            candidates = []
            if self.scenario == "source-review":
                for suffix in ("A", "B"):
                    candidates.append(candidate(f"release-{suffix}", None, None,
                                                f"Cinder Gate - Other Writer - Edition {suffix}"))
            elif title.casefold() == ALIAS.casefold():
                if self.scenario == "verified":
                    candidates = [candidate("verified-copy", TITLE, AUTHOR, "cinder-gate.epub", isbn=True)]
                elif self.scenario == "wrong-work":
                    # Same title, different author: a title collision must stay blocked.
                    candidates = [candidate("wrong-copy", TITLE, "Other Writer", "other-work.epub")]
            for item in candidates:
                self.offered.add((source, item["providerReference"]))
            return {"candidates": candidates}

    def model(self, body: dict) -> dict:
        with self.lock:
            self.model_calls.append(deepcopy(body))
            value = {"duplicate": TITLE, "url": "https://example.test/book", "too-long": "x" * 121}.get(self.scenario, ALIAS)
            reply = {"schema": "search-guidance/1", "give_up": self.scenario == "give-up", "suggestions": []}
            if not reply["give_up"]:
                reply["suggestions"] = [{"reason": "alternate_published_title", "value": value,
                    "evidence_ids": ["r9999" if self.scenario == "invented-id" else "req-title"],
                    "note": "Try an alternate bibliographic title."}]
            return {"id": "lab-response", "object": "chat.completion", "model": "lab/search",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(reply)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 30, "total_tokens": 60}}


def candidate(reference: str, title: str | None, author: str | None, release: str, *, isbn: bool = False) -> dict:
    return {"providerReference": reference, "candidateRevision": "rev-1", "acquireToken": None,
            "work": {"title": title, "subtitle": None, "authors": [] if author is None else [{"name": author, "role": "author"}],
                     "series": [], "identifiers": []},
            "edition": {"language": "en", "publicationYear": 2003, "publisher": None,
                        "identifiers": [{"scheme": "isbn13", "value": "9791000000022"}] if isbn else []},
            "release": {"name": release, "format": "epub", "sizeBytes": 4096,
                        "isCollection": False, "isAbridged": False, "isSample": False, "drm": "none"}, "extensions": {}}


def make_server(address: tuple[str, int]) -> ThreadingHTTPServer:
    fixture = Fixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def respond(self, status: int, value=None) -> None:
            data = b"" if value is None else json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parts = self.path.strip("/").split("/")
            source = parts[0]
            if self.path == "/test/state":
                self.respond(200, fixture.state())
            elif self.path == "/health" or len(parts) == 2 and parts[1] == "health":
                self.respond(200, {"status": "healthy", "operations": {"search": "available", "acquire": "available"}})
            elif source in ("source-a", "source-b") and parts[1:] == ["manifest"]:
                self.respond(200, {"protocolVersions": ["2"], "protocolVersion": "2", "instanceId": fixture.instance_id + source,
                    "id": source, "name": source, "version": "1.0.0", "capabilities": {"mediaTypes": ["ebook"],
                    "operations": ["search", "acquire"], "features": []}})
            elif len(parts) == 3 and parts[1] == "acquire":
                with fixture.lock:
                    job = fixture.jobs.get(parts[2])
                    self.respond(200 if job else 404, job)
            else:
                self.respond(404, {"message": "Unknown fixture route"})

        def do_POST(self) -> None:
            try:
                # HttpClient streams provider JSON with chunked transfer encoding.
                # BaseHTTPRequestHandler leaves decoding to the handler.
                self.connection.settimeout(15)
                data = bytearray()
                if self.headers.get("Transfer-Encoding", "").casefold() == "chunked":
                    while True:
                        size = int(self.rfile.readline(128).split(b";", 1)[0].strip(), 16)
                        if size < 0 or len(data) + size > 100_000:
                            self.respond(413)
                            return
                        if size == 0:
                            if self.rfile.readline(128) != b"\r\n":
                                raise ValueError("Unexpected trailer")
                            break
                        data.extend(self.rfile.read(size))
                        if self.rfile.read(2) != b"\r\n":
                            raise ValueError("Invalid chunk")
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 100_000:
                        self.respond(413)
                        return
                    data.extend(self.rfile.read(length))
                body = json.loads(data or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                parts = self.path.strip("/").split("/")
                if self.path == "/test/configure":
                    fixture.reset(body.get("scenario", ""))
                    self.respond(204)
                elif self.path == "/v1/chat/completions":
                    self.respond(200, fixture.model(body))
                elif parts[0] in ("source-a", "source-b") and parts[1:] == ["search"]:
                    self.respond(200, fixture.search(parts[0], body))
                elif parts[0] in ("source-a", "source-b") and parts[1:] == ["acquire"]:
                    with fixture.lock:
                        fixture.acquisitions.append({"source": parts[0], "body": deepcopy(body)})
                        if (parts[0], body.get("candidateReference")) not in fixture.offered:
                            self.respond(404, {"code": "UNKNOWN_CANDIDATE"})
                            return
                        job_id = str(uuid4())
                        job = {"jobId": job_id, "status": "InProgress", "state": "running", "phase": "acquiring", "pollAfterSeconds": 30}
                        fixture.jobs[job_id] = job
                        self.respond(202, job)
                else:
                    self.respond(404)
            except (ValueError, TypeError):
                self.respond(400, {"message": "Invalid fixture request"})

    return ThreadingHTTPServer(address, Handler)


if __name__ == "__main__":
    make_server(("0.0.0.0", 8080)).serve_forever()
