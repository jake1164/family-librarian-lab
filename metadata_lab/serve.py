"""Local UI server: stdlib ``http.server``, one job at a time.

Security posture (the reason this file is small and paranoid):

* loopback by default; another ``--host`` needs ``--allow-remote`` and warns;
* the ``Host`` header must be localhost/127.0.0.1/::1 or the bound host, which
  blocks DNS-rebinding pages from driving the server;
* POST needs ``Content-Type: application/json`` and a small body;
* only provider ids and typed fields are accepted - never a URL or an endpoint;
* result files are served only from the results directory, run ids restricted to
  ``[0-9A-Za-z._-]+``;
* no credential value is ever put in a response. Keys are read from the server's
  environment only when a run starts.
"""

from __future__ import annotations

import ipaddress
import json
import sys
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from . import artifacts, registry, serve_data
from .config import is_configured
from .http import HttpClient
from .model import Operation
from .providers.base import ProviderAdapter
from .serve_data import RequestError
from .serve_jobs import Busy, JobManager, harness_payload

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 16 * 1024
PAGE = Path(__file__).resolve().parent / "static" / "index.html"
LOOPBACK_NAMES = {"localhost"}


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and any loopback address."""

    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    results_root: Path = artifacts.DEFAULT_RESULTS
    stamp_root: Path | None = None
    adapters: Mapping[str, type[ProviderAdapter]] | None = None
    http_factory: Callable[[str], HttpClient] | None = None
    quiet: bool = False
    jobs: JobManager | None = field(default=None, repr=False)


class HttpError(Exception):
    """A refusal that carries the status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _host_allowed(header: str | None, bound: str) -> bool:
    """Accept only our own names: the DNS-rebinding guard."""

    if not header:
        return False
    host = header.strip()
    if host.startswith("["):  # IPv6 literal: [::1]:port
        host = host[1:].split("]", 1)[0]
    else:
        host = host.rsplit(":", 1)[0]
    host = host.lower()
    return host in LOOPBACK_NAMES or host == bound.lower() or is_loopback(host)


def _segments(path: str) -> list[str]:
    """Split a request path into decoded segments; ``None``-free and traversal-free."""

    raw = urllib.parse.urlsplit(path).path
    if ".." in raw or "\\" in raw:
        raise HttpError(404, "not found")
    return [urllib.parse.unquote(part) for part in raw.split("/") if part]


class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        super().__init__((config.host, config.port), LabHandler)


class LabHandler(BaseHTTPRequestHandler):
    server_version = "metadata-lab-ui/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
        if not self.server.config.quiet:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except HttpError as exc:
            self._send_error(exc.status, str(exc))
        except RequestError as exc:
            self._send_error(exc.status, str(exc))
        except Busy as exc:
            self._send_error(409, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never hand a traceback to the browser
            self._send_error(500, f"internal error: {type(exc).__name__}")

    def _route(self, method: str) -> None:
        config = self.server.config
        if not _host_allowed(self.headers.get("Host"), config.host):
            raise HttpError(403, "Host header is not localhost, 127.0.0.1 or the bound host")
        parts = _segments(self.path)
        if method == "GET":
            return self._get(parts)
        return self._post(parts)

    # -- GET -------------------------------------------------------------------

    def _get(self, parts: list[str]) -> None:
        config = self.server.config
        if parts == []:
            return self._send_page()
        if parts == ["api", "harness"]:
            return self._json(200, harness_payload(config.stamp_root))
        if parts == ["api", "providers"]:
            return self._json(200, self._providers())
        if parts == ["api", "suites"]:
            return self._json(200, serve_data.suites_payload())
        if parts == ["api", "runs"]:
            return self._json(200, {"runs": serve_data.list_runs(config.results_root)})
        if parts == ["api", "compare"]:
            return self._compare()
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            return self._job(parts[2])
        if len(parts) == 4 and parts[:2] == ["api", "runs"]:
            return self._run_file(parts[2], parts[3])
        raise HttpError(404, "not found")

    def _providers(self) -> dict[str, Any]:
        config = self.server.config
        entries = []
        for provider_id, adapter_cls in config.adapters.items():
            adapter = adapter_cls()
            entries.append(
                {
                    "id": provider_id,
                    "display_name": adapter.display_name,
                    "adapter_version": adapter.adapter_version,
                    "configured": is_configured(adapter_cls),
                    "verified_live": bool(adapter.VERIFIED_LIVE),
                    "credential_env": adapter.credential_env,
                    "declared": {
                        operation.value: adapter.declared_support(operation)
                        for operation in Operation
                    },
                    "unsupported_reasons": {
                        operation.value: {
                            "reason": item.reason,
                            "source_url": item.source_url,
                        }
                        for operation, item in adapter.unsupported.items()
                    },
                }
            )
        return {"providers": entries}

    def _compare(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        first = (query.get("a") or [""])[0]
        second = (query.get("b") or [""])[0]
        if not first or not second:
            raise HttpError(400, "compare needs a=<run id> and b=<run id>")
        self._json(200, serve_data.compare_runs(self.server.config.results_root, first, second))

    def _job(self, job_id: str) -> None:
        job = self.server.config.jobs.get(job_id) if self.server.config.jobs else None
        if job is None:
            raise HttpError(404, "unknown job")
        self._json(200, {"job": job.to_dict()})

    def _run_file(self, run_id: str, name: str) -> None:
        if not serve_data.RUN_ID.match(run_id) or name not in serve_data.RESULT_FILES:
            raise HttpError(404, "not found")
        root = Path(self.server.config.results_root).resolve()
        target = root / run_id / name
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            raise HttpError(404, "not found") from None
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise HttpError(404, "not found")
        payload = resolved.read_bytes()
        if name == "report.html":
            return self._send(200, payload, "text/html; charset=utf-8")
        self._send(
            200,
            payload,
            "application/json; charset=utf-8",
            extra={"Content-Disposition": f'attachment; filename="{run_id}-{name}"'},
        )

    # -- POST ------------------------------------------------------------------

    def _post(self, parts: list[str]) -> None:
        config = self.server.config
        if parts == ["api", "run"]:
            body = self._read_json()
            catalogue, _broken = serve_data.suite_catalogue()
            request = serve_data.parse_run(
                body, adapters=config.adapters, catalogue=catalogue
            )
            job = config.jobs.submit(request) if config.jobs else None
            if job is None:  # pragma: no cover - create_server always sets it
                raise HttpError(500, "no job manager")
            return self._json(202, {"job": job.to_dict()})
        if parts == ["api", "check"]:
            body = self._read_json()
            selected = serve_data.parse_check(body, adapters=config.adapters)
            results = config.jobs.check(selected) if config.jobs else []
            return self._json(200, {"results": results})
        if parts == ["api", "selftest"]:
            # Runs on this request's worker thread (about 3 s) and holds the gate, so it
            # can never overlap a run; the reply carries the 26 lines and the stamp.
            self._read_json()
            payload = config.jobs.selftest() if config.jobs else {}
            return self._json(200, payload)
        if parts == ["api", "case"]:
            body = self._read_json()
            kind, fields = serve_data.parse_case(body)
            text = serve_data.case_yaml(kind, fields)
            return self._send(
                200,
                text.encode("utf-8"),
                "text/yaml; charset=utf-8",
                extra={"Content-Disposition": f'attachment; filename="case-{kind}.yaml"'},
            )
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            job = config.jobs.get(parts[2]) if config.jobs else None
            if job is None:
                raise HttpError(404, "unknown job")
            self._read_json()
            job.request_cancel()
            return self._json(200, {"job": job.to_dict()})
        raise HttpError(404, "not found")

    def _read_json(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "POST requires Content-Type: application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise HttpError(411, "Content-Length is required")
        if int(raw_length) > MAX_BODY:
            raise HttpError(413, f"body is larger than {MAX_BODY} bytes")
        raw = self.rfile.read(int(raw_length))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, "body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise HttpError(400, "body must be a JSON object")
        return payload

    # -- responses -------------------------------------------------------------

    def _send_page(self) -> None:
        try:
            payload = PAGE.read_bytes()
        except OSError:
            raise HttpError(500, "the page is missing from this checkout") from None
        self._send(200, payload, "text/html; charset=utf-8")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: int, message: str) -> None:
        # Close: a rejected POST body may still be unread, and nothing we return
        # carries a request URL (those can contain provider query parameters).
        self.close_connection = True
        self._json(status, {"error": message})

    def _send(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


def create_server(config: ServerConfig) -> LabServer:
    """Fill in the defaults (adapters, HTTP clients, job manager) and bind."""

    adapters = dict(config.adapters) if config.adapters is not None else registry.discover()
    config.adapters = adapters
    config.http_factory = config.http_factory or registry.http_factory(adapters)
    if config.jobs is None:
        config.jobs = JobManager(
            adapters=adapters,
            http_factory=config.http_factory,
            results_root=Path(config.results_root),
            stamp_root=config.stamp_root,
        )
    return LabServer(config)


def serve(config: ServerConfig) -> None:
    """Run until interrupted. Prints the URL to open; never prints a credential."""

    server = create_server(config)
    host, port = server.server_address[:2]
    if not is_loopback(str(host)):
        print(
            "WARNING: this server spends API quota with server-held keys and is bound "
            f"to a non-loopback address ({host}). Keep it on a trusted network.",
            file=sys.stderr,
        )
    print(f"metadata lab UI: http://{host}:{port}/", flush=True)
    print(f"results directory: {Path(config.results_root).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_BODY",
    "LabServer",
    "ServerConfig",
    "create_server",
    "is_loopback",
    "serve",
]
