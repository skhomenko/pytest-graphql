"""A real local HTTP server for M5a's response matrix (loopback is allowed;
see ``tests/unit/conftest.py``).

``HttpxTransport`` is exercised against actual sockets here, not only a fake,
because response classification (C3) depends on real HTTP framing: status
line, headers, and a body read over the wire.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple


class PlannedResponse(NamedTuple):
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    delay: float = 0.0
    body_delay: float = 0.0


Responder = Callable[[bytes, dict[str, str]], PlannedResponse]


def fixed(
    status: int,
    *,
    content_type: str | None = None,
    body: bytes = b"",
    delay: float = 0.0,
    body_delay: float = 0.0,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> Responder:
    """A responder that always returns the same planned response.

    ``delay`` stalls before the status line and headers are sent at all, so
    it produces a connect/header-phase timeout. ``body_delay`` stalls after
    the headers (with their real ``Content-Length``) are already flushed, so
    it produces a timeout while the body is being streamed -- the case a
    client only sees once it starts reading via ``iter_bytes()``.
    """
    headers = extra_headers
    if content_type is not None:
        headers = (*headers, ("Content-Type", content_type))
    planned = PlannedResponse(
        status=status, headers=headers, body=body, delay=delay, body_delay=body_delay
    )
    return lambda _body, _headers: planned


@contextmanager
def local_server(responder: Responder) -> Iterator[str]:
    """Start a real HTTP server on loopback and yield its ``/graphql`` URL."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            headers = {key.lower(): value for key, value in self.headers.items()}
            planned = responder(body, headers)
            if planned.delay:
                time.sleep(planned.delay)
            self.send_response(planned.status)
            for name, value in planned.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(planned.body)))
            self.end_headers()
            if planned.body_delay:
                self.wfile.flush()
                time.sleep(planned.body_delay)
            self.wfile.write(planned.body)

        def do_POST(self) -> None:
            self._handle()

        def do_GET(self) -> None:
            self._handle()

        def log_message(self, _log_format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/graphql"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def closed_port_url() -> str:
    """A loopback URL with nothing listening, for a real connection-refused test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/graphql"
