"""Tiny dependency-free web server: static UI + JSON snapshot + Server-Sent Events stream."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse, urlsplit

from ..hive import Hive

STATIC = resources.files("specops.web") / "static"
HEARTBEAT = 5.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def make_handler(hive: Hive, changed: threading.Condition) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "specops"

        def log_message(self, format: str, *args: object) -> None:  # silence access logs
            pass

        def do_GET(self) -> None:
            if not self._host_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            session = (query.get("session") or [None])[0]
            if url.path == "/api/state":
                self._json(hive.snapshot(session))
            elif url.path == "/api/stream":
                self._stream(session)
            elif url.path in ("/", "/index.html"):
                self._static("index.html")
            elif url.path.startswith("/static/"):
                self._static(url.path.removeprefix("/static/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _host_allowed(self) -> bool:
            # Blocks DNS rebinding: a page on evil.example resolving to 127.0.0.1 would otherwise
            # be able to read transcripts. On a non-local bind the user opted into exposure.
            if self.server.server_address[0] not in LOCAL_HOSTS:
                return True
            hostname = urlsplit("//" + (self.headers.get("Host") or "")).hostname
            return hostname in LOCAL_HOSTS

        def _json(self, data: object) -> None:
            body = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name: str) -> None:
            if "/" in name or "\\" in name or name.startswith("."):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            file = STATIC / name
            if not file.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = file.read_bytes()
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, session: str | None) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            seen = -1
            last_sent = 0.0
            try:
                while True:
                    with changed:
                        if hive.version == seen:
                            changed.wait(timeout=1.0)
                    now = time.monotonic()
                    if hive.version != seen or now - last_sent > HEARTBEAT:
                        seen = hive.version
                        last_sent = now
                        payload = json.dumps(hive.snapshot(session), separators=(",", ":"))
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def serve(hive: Hive, host: str = "127.0.0.1", port: int = 7777) -> ThreadingHTTPServer:
    """Bind a server (trying the next ports if busy). Caller runs ``serve_forever``."""
    changed = threading.Condition()

    def notify() -> None:
        with changed:
            changed.notify_all()

    hive.on_change(notify)
    handler = make_handler(hive, changed)
    last_error: OSError | None = None
    last_port = min(port + 19, 65535)
    for candidate in range(port, last_port + 1):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
            continue
        server.daemon_threads = True
        return server
    raise OSError(f"no free port in {port}-{last_port}") from last_error
