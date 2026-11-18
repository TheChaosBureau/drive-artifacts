# SPDX-License-Identifier: Apache-2.0
"""The fake Drive behind a real socket, for tests that run the CLI as a subprocess."""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from tests.fake_drive import CutStream, FakeDrive


def _handler_class(fake: FakeDrive) -> type[BaseHTTPRequestHandler]:
    # The server is threaded; the fake (fault list, counters, files) is not.
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass

        def _hang_up(self) -> None:
            # close() alone would not do: rfile still holds a reference to the socket,
            # so the client would wait for a response until its read timeout.
            # shutdown() sends the FIN right away.
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            path = self.path
            for prefix in ("/api", "/oauth"):
                path = path.removeprefix(prefix)
            url = f"http://{self.headers['Host']}{path}"
            request = httpx.Request(
                self.command, url, headers=dict(self.headers.items()), content=body
            )
            try:
                with lock:
                    response = fake.handler(request)
            except httpx.ConnectError:
                self._hang_up()  # an injected drop
                return
            # an injected cut: the full Content-Length, the first bytes, then the hang-up
            cut = response.stream if isinstance(response.stream, CutStream) else None
            payload = cut.full if cut is not None else response.content
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                if key.lower() == "location":
                    value = value.replace(
                        f"http://{self.headers['Host']}/upload",
                        f"http://{self.headers['Host']}/api/upload",
                    )
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if cut is not None:
                self.wfile.write(payload[: cut.cut])
                self._hang_up()
                return
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815 - stdlib dispatch names

    return Handler


@contextmanager
def serve(fake: FakeDrive) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
