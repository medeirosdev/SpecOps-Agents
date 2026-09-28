from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from specops.hive import Hive
from specops.web.server import serve

from .conftest import SESSION_ID


@pytest.fixture
def base_url(projects) -> Iterator[str]:
    hive = Hive(root=projects, since=0)
    hive.poll()
    server = serve(hive, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read()
    except urllib.error.HTTPError as err:
        return err.code, "", b""


@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/", "text/html"),
        ("/index.html", "text/html"),
        ("/static/app.js", "javascript"),
        ("/static/style.css", "text/css"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_files(base_url: str, path: str, ctype: str) -> None:
    status, content_type, body = get(base_url + path)
    assert status == 200 and ctype in content_type and body


@pytest.mark.parametrize(
    "path",
    [
        "/static/../hive.py",
        "/static/%2e%2e/hive.py",
        "/static/.hidden",
        "/static/missing.js",
        "/static/sub/app.js",
        "/nope",
    ],
)
def test_not_found(base_url: str, path: str) -> None:
    assert get(base_url + path)[0] == 404


def test_state_endpoint(base_url: str) -> None:
    status, ctype, body = get(base_url + "/api/state")
    snap = json.loads(body)
    assert status == 200 and ctype == "application/json"
    assert [s["id"] for s in snap["sessions"]] == [SESSION_ID]
    assert snap["sessions"][0]["detail"] is False

    snap = json.loads(get(f"{base_url}/api/state?session={SESSION_ID}")[2])
    assert snap["sessions"][0]["detail"] is True
    assert snap["sessions"][0]["agents"][0]["activities"]


def test_stream_sends_a_snapshot_immediately(base_url: str) -> None:
    with urllib.request.urlopen(f"{base_url}/api/stream?session={SESSION_ID}", timeout=5) as resp:
        assert resp.headers["Content-Type"] == "text/event-stream"
        first = resp.readline().decode()
    assert first.startswith("data: ")
    snap = json.loads(first.removeprefix("data: "))
    assert snap["sessions"][0]["detail"] is True


def test_serve_skips_busy_ports(projects) -> None:
    hive = Hive(root=projects, since=0)
    first = serve(hive, port=0)
    try:
        taken = first.server_address[1]
        second = serve(hive, port=taken)
        try:
            assert second.server_address[1] != taken
        finally:
            second.server_close()
    finally:
        first.server_close()


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("localhost", 200),
        ("127.0.0.1:7777", 200),
        ("[::1]:7777", 200),
        ("evil.example", 403),
        ("evil.example:7777", 403),
        ("localhost.evil.example", 403),
    ],
)
def test_host_header_blocks_dns_rebinding(base_url: str, host: str, status: int) -> None:
    req = urllib.request.Request(base_url + "/api/state", headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.status
    except urllib.error.HTTPError as err:
        code = err.code
    assert code == status


def test_serve_does_not_overflow_the_port_range(projects) -> None:
    hive = Hive(root=projects, since=0)
    try:
        server = serve(hive, port=65535)
    except OSError as exc:  # busy is fine; OverflowError (a crash) is not
        assert "65535-65535" in str(exc)
    else:
        server.server_close()
