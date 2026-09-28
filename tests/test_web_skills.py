"""The skills API over HTTP: every write guard, tried by the request it should stop."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from specops.hive import Hive
from specops.skills import Library, Target
from specops.web.auth import Auth
from specops.web.server import Skills, serve


class Server:
    def __init__(self, port: int, codes: list[str], skills: Skills) -> None:
        self.port = port
        self.codes = codes
        self.skills = skills
        self.origin = f"http://127.0.0.1:{port}"

    def request(
        self, method: str, path: str, body: object = None, headers: dict | None = None
    ) -> tuple[int, dict, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body)
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        return resp.status, dict(resp.getheaders()), payload

    def post(self, path: str, body: object, token: str | None = None, **over: str) -> tuple:
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": self.origin,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "X-SpecOps": "1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for key, value in over.items():
            name = key.replace("_", "-")
            if value is None:
                headers.pop(name, None)
            else:
                headers[name] = value
        status, _, payload = self.request("POST", path, body, headers)
        return status, payload

    def unlock(self) -> str:
        status, payload = self.post("/api/auth/pair", {"code": self.codes[-1]})
        assert status == 200, payload
        return payload["token"]


def start(projects: Path, tmp_path: Path, writable: bool = True) -> Iterator[Server]:
    codes: list[str] = []
    library = Library(
        home=tmp_path / "specops",
        targets=lambda: [
            Target("claude", "global", tmp_path / "claude-skills"),
            Target("antigravity", "global", tmp_path / "gemini-skills"),
        ],
    )
    skills = Skills(
        library,
        Auth(on_code=codes.append) if writable else None,
        "" if writable else "started with --read-only",
    )
    hive = Hive(root=projects, since=0)
    server = serve(hive, host="127.0.0.1", port=0, skills=skills)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield Server(server.server_address[1], codes, skills)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def srv(projects: Path, tmp_path: Path) -> Iterator[Server]:
    yield from start(projects, tmp_path)


SKILL = {"name": "notes", "description": "Write release notes", "body": "1. Read the log."}


def test_full_flow(srv: Server, tmp_path: Path) -> None:
    status, _, state = srv.request("GET", "/api/skills")
    assert status == 200 and state["writable"] and not state["auth"]["unlocked"]

    token = srv.unlock()
    assert srv.post("/api/skills/create", SKILL, token)[0] == 200
    status, _ = srv.post(
        "/api/skills/publish", {"name": "notes", "target": "global:claude", "on": True}, token
    )
    assert status == 200 and (tmp_path / "claude-skills" / "notes" / "SKILL.md").is_file()

    _, _, state = srv.request("GET", "/api/skills", headers={"Authorization": f"Bearer {token}"})
    assert state["auth"]["unlocked"] and [s["name"] for s in state["library"]] == ["notes"]
    assert [a["action"] for a in state["audit"]] == ["publish", "create"]
    assert state["audit"][0]["actor"] == "web 127.0.0.1"

    assert srv.post("/api/auth/lock", {}, token)[0] == 200
    assert srv.post("/api/skills/delete", {"name": "notes"}, token)[0] == 401


def test_writes_need_a_token(srv: Server) -> None:
    status, payload = srv.post("/api/skills/create", SKILL)
    assert status == 401 and "unlock" in payload["error"]
    assert srv.post("/api/skills/create", SKILL, "forged-token")[0] == 401


@pytest.mark.parametrize(
    ("override", "status"),
    [
        ({"Origin": None}, 403),  # no Origin at all
        ({"Origin": "http://evil.example"}, 403),  # another site
        ({"Origin": "http://127.0.0.1:1"}, 403),  # another local port
        ({"Origin": "null"}, 403),  # sandboxed iframe / file://
        ({"Sec_Fetch_Site": "cross-site"}, 403),
        ({"Sec_Fetch_Site": "same-site"}, 403),
        ({"X_SpecOps": None}, 403),  # a plain form or fetch without the custom header
        ({"Content_Type": "text/plain"}, 415),  # a "simple" request that skips preflight
        ({"Content_Type": "application/x-www-form-urlencoded"}, 415),
        ({"Host": "evil.example"}, 403),  # DNS rebinding
    ],
)
def test_cross_site_requests_are_refused(srv: Server, override: dict, status: int) -> None:
    token = srv.unlock()
    got, _ = srv.post("/api/skills/create", SKILL, token, **override)
    assert got == status
    _, _, state = srv.request("GET", "/api/skills")
    assert state["library"] == []  # nothing was written


def test_pairing_is_guarded_too(srv: Server) -> None:
    code = srv.codes[-1]
    assert srv.post("/api/auth/pair", {"code": code}, Origin="http://evil.example")[0] == 403
    assert srv.post("/api/auth/pair", {"code": code}, X_SpecOps=None)[0] == 403
    assert srv.post("/api/auth/pair", {"code": code})[0] == 200  # still unused, still valid


def test_brute_force_locks_pairing(srv: Server) -> None:
    for _ in range(4):
        assert srv.post("/api/auth/pair", {"code": "AAAAA-AAAAA"})[0] == 401
    status, payload = srv.post("/api/auth/pair", {"code": "AAAAA-AAAAA"})
    assert status == 429 and payload["retry_after"] == 300
    assert srv.post("/api/auth/pair", {"code": srv.codes[-1]})[0] == 429


def test_bad_bodies(srv: Server) -> None:
    token = srv.unlock()
    assert srv.post("/api/skills/create", b"{not json", token)[0] == 400
    assert srv.post("/api/skills/create", [1, 2], token)[0] == 400
    # Bigger than socket buffers: the client is still sending when the server refuses, which
    # must still end in a clean answer, not a connection reset (it did on macOS).
    big = {**SKILL, "body": "x" * 5_000_000}
    for _ in range(3):
        assert srv.post("/api/skills/create", big, token)[0] == 413
        assert srv.post("/api/skills/create", big, token, Origin="http://evil.example")[0] == 403
    evil = {**SKILL, "name": "../../escape"}
    assert srv.post("/api/skills/create", evil, token)[0] == 400
    assert srv.post("/api/skills/nope", {}, token)[0] == 404


def test_read_only_mode(projects: Path, tmp_path: Path) -> None:
    for srv in start(projects, tmp_path, writable=False):
        _, _, state = srv.request("GET", "/api/skills")
        assert state["writable"] is False and state["reason"] == "started with --read-only"
        status, payload = srv.post("/api/auth/pair", {"code": "whatever"})
        assert status == 403 and "read-only" in payload["error"]


def test_security_headers(srv: Server) -> None:
    for path in ("/", "/static/app.js", "/api/skills"):
        _, headers, _ = srv.request("GET", path)
        assert headers["X-Frame-Options"] == "DENY"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers
