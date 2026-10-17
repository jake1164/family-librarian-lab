"""A destination that drops the connection mid-request must read as "not reachable yet", not crash the scenario.

CWA-L-07 failed with "Remote end closed connection without response": CWA's web process was refreshing its database
session right after the ingest finished, and closed the lab's OPDS poll without answering. urllib only wraps errors
raised while *sending* in URLError; a close while reading the response escapes as a bare http.client error, so the
poll helpers (which retry on status 0) never saw it.
"""

from __future__ import annotations

import socket
import threading

from family_librarian_lab import clients


def _server_that_closes_without_answering() -> tuple[str, threading.Thread]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.close()
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}/", thread


def test_a_connection_closed_without_a_response_reads_as_unreachable():
    url, thread = _server_that_closes_without_answering()

    assert clients._http(url, timeout=5) == (0, b"")

    thread.join(timeout=5)
