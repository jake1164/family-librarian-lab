"""A loopback-only scripted HTTP server.

It exists so the selftest can drive the *real* ``HttpClient`` (retries, timeouts,
``Retry-After``, pagination, redaction) instead of mocking it. Bind address is
always 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class ScriptedResponse:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    raw: str | None = None  # sent verbatim (malformed JSON, HTML, empty)

    def payload(self) -> bytes:
        if self.raw is not None:
            return self.raw.encode("utf-8")
        return json.dumps(self.body if self.body is not None else {}).encode("utf-8")


class ScriptedServer:
    """Serves a scripted sequence of responses per path and logs every request."""

    def __init__(self) -> None:
        self._routes: dict[str, list[ScriptedResponse]] = {}
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def queue(self, path: str, *responses: ScriptedResponse) -> "ScriptedServer":
        with self._lock:
            self._routes.setdefault(path, []).extend(responses)
        return self

    def reset(self) -> None:
        with self._lock:
            self._routes.clear()
            self.requests.clear()

    def start(self) -> "ScriptedServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # keep the selftest quiet
                return

            def _handle(self) -> None:
                parsed = urlparse(self.path)
                query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                with server._lock:
                    server.requests.append(
                        {
                            "path": parsed.path,
                            "query": query,
                            "headers": dict(self.headers),
                            "body": body,
                        }
                    )
                    queue = server._routes.get(parsed.path)
                    response = None
                    if queue:
                        response = queue.pop(0) if len(queue) > 1 else queue[0]
                if response is None:
                    response = ScriptedResponse(404, {"error": "no route"})
                if response.delay:
                    time.sleep(min(response.delay, 0.5))
                payload = response.payload()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle  # noqa: N815
            do_POST = _handle  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "ScriptedServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def calls(self, path: str) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["path"] == path]


def scripted(status: int = 200, body: Any = None, **kwargs: Any) -> ScriptedResponse:
    return ScriptedResponse(status=status, body=body, **kwargs)


__all__ = ["ScriptedResponse", "ScriptedServer", "scripted"]
