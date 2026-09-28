"""Tiny dependency-free web server: static UI + JSON snapshot + Server-Sent Events stream.

It also serves the skill library. Reads are open like the rest of the dashboard; every write goes
through :meth:`Handler._guard_write`, which stacks these checks (any failure rejects it):

* writing is only enabled on a loopback bind, and not with ``--read-only``;
* ``Host`` must be localhost (DNS rebinding) and ``Origin`` exactly this server's origin, with
  ``Sec-Fetch-Site: same-origin`` when the browser sends it (cross-site requests);
* the body must be ``application/json`` with a custom ``X-SpecOps`` header, which a page on
  another origin can't send without a CORS preflight this server never answers;
* a session token from :class:`~specops.web.auth.Auth`, which only the terminal's pairing code
  gives out.

Every response forbids framing and sniffing, and the page gets a strict Content-Security-Policy
(no inline or third-party scripts), so an escaping bug can't be turned into a token theft.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

from ..hive import Hive
from ..skills import Library, SkillError
from .auth import Auth, AuthError

STATIC = resources.files("specops.web") / "static"
HEARTBEAT = 5.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_REQUEST = 512 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class Skills:
    """What the server needs for the skills tab. ``auth`` is None when editing is disabled."""

    def __init__(self, library: Library, auth: Auth | None, reason: str = "") -> None:
        self.library = library
        self.auth = auth
        self.reason = reason  # why editing is disabled, shown in the UI


class HTTPError(Exception):
    def __init__(self, status: int, message: str, retry_after: float = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def make_handler(
    hive: Hive, changed: threading.Condition, skills: Skills | None = None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "specops"

        def log_message(self, format: str, *args: object) -> None:  # silence access logs
            pass

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            super().end_headers()

        def do_GET(self) -> None:
            if not self._host_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "unexpected Host header")
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            session = (query.get("session") or [None])[0]
            if url.path == "/api/state":
                self._json(hive.snapshot(session))
            elif url.path == "/api/skills":
                self._skills_state()
            elif url.path == "/api/stream":
                self._stream(session)
            elif url.path in ("/", "/index.html"):
                self._static("index.html")
            elif url.path.startswith("/static/"):
                self._static(url.path.removeprefix("/static/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        # ------------------------------------------------------------ skills
        def _skills_state(self) -> None:
            if skills is None:
                self._json({"enabled": False})
                return
            lib = skills.library
            auth = skills.auth
            token = self._token()
            self._json(
                {
                    "enabled": True,
                    "writable": auth is not None,
                    "reason": skills.reason,
                    "auth": auth.status(token) if auth else {"unlocked": False},
                    "library": lib.list(),
                    "external": lib.external(),
                    "projects": lib.projects(),
                    "targets": [t.to_dict() for t in lib.targets()],
                    "audit": lib.audit(),
                    "home": str(lib.home),
                }
            )

        def do_POST(self) -> None:
            try:
                self._guard_write()
                body = self._read_json()
                route = urlparse(self.path).path
                if route == "/api/auth/pair":
                    assert skills is not None and skills.auth is not None
                    token, expires = skills.auth.pair(str(body.get("code", "")), self._client())
                    self._json({"token": token, "expires_in": expires})
                    return
                self._authorize()
                if route == "/api/auth/lock":
                    assert skills is not None and skills.auth is not None
                    skills.auth.revoke(self._token())
                    self._json({"ok": True})
                    return
                self._json(self._skill_action(route, body))
            except HTTPError as err:
                self._error(err.status, str(err), err.retry_after)
            except AuthError as err:
                self._error(err.status, str(err), err.retry_after)
            except SkillError as err:
                self._error(err.status, str(err))
            except OSError as err:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"filesystem error: {err.strerror}")

        def _skill_action(self, route: str, body: dict[str, Any]) -> Any:
            assert skills is not None
            lib = skills.library
            actor = f"web {self._client()}"
            name = str(body.get("name", ""))
            force = body.get("force") is True
            if route in ("/api/skills/create", "/api/skills/update"):
                desc, text = body.get("description", ""), body.get("body", "")
                return lib.save(name, desc, text, route.endswith("create"), actor)
            if route == "/api/skills/delete":
                lib.delete(name, force, actor)
                return {"ok": True}
            if route == "/api/skills/publish":
                on = body.get("on") is True
                return lib.set_published(name, str(body.get("target", "")), on, force, actor)
            if route == "/api/skills/import":
                return lib.import_skill(str(body.get("target", "")), name, actor)
            raise HTTPError(HTTPStatus.NOT_FOUND, "no such action")

        def _guard_write(self) -> None:
            if skills is None or skills.auth is None:
                reason = skills.reason if skills else "skills are not enabled"
                raise HTTPError(HTTPStatus.FORBIDDEN, reason or "editing is disabled")
            if not self._host_allowed():
                raise HTTPError(HTTPStatus.FORBIDDEN, "unexpected Host header")
            port = self.server.server_address[1]
            allowed = {f"http://{h}:{port}" for h in ("localhost", "127.0.0.1", "[::1]")}
            if self.headers.get("Origin") not in allowed:
                raise HTTPError(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                raise HTTPError(HTTPStatus.FORBIDDEN, "cross-site request refused")
            if self.headers.get("X-SpecOps") != "1":
                raise HTTPError(HTTPStatus.FORBIDDEN, "missing X-SpecOps header")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HTTPError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required") from None
            if length < 0 or length > MAX_REQUEST:
                raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            try:
                data = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "invalid JSON") from None
            if not isinstance(data, dict):
                raise HTTPError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return data

        def _token(self) -> str | None:
            header = self.headers.get("Authorization") or ""
            scheme, _, token = header.partition(" ")
            return token.strip() if scheme.lower() == "bearer" and token.strip() else None

        def _authorize(self) -> None:
            assert skills is not None and skills.auth is not None
            skills.auth.check(self._token())

        def _client(self) -> str:
            return str(self.client_address[0])

        def _error(self, status: int, message: str, retry_after: float = 0) -> None:
            body = json.dumps({"error": message, "retry_after": round(retry_after)}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if retry_after:
                self.send_header("Retry-After", str(round(retry_after)))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


def serve(
    hive: Hive, host: str = "127.0.0.1", port: int = 7777, skills: Skills | None = None
) -> ThreadingHTTPServer:
    """Bind a server (trying the next ports if busy). Caller runs ``serve_forever``."""
    changed = threading.Condition()

    def notify() -> None:
        with changed:
            changed.notify_all()

    hive.on_change(notify)
    if skills is not None and skills.auth is not None and host not in LOCAL_HOSTS:
        skills.auth, skills.reason = None, "editing is disabled on a non-local interface"
    handler = make_handler(hive, changed, skills)
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
