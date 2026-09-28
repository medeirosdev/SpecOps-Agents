"""Antigravity sources, fed with synthetic logs, summaries and state databases."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest

from specops import antigravity as ag
from specops.hive import Hive

NOW = time.time()


# ------------------------------------------------------------------ tiny protobuf encoder
def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        if not n:
            return out + bytes([b])
        out += bytes([b | 0x80])


def fld(num: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def ts(num: int, secs: float) -> bytes:
    return fld(num, fld(1, int(secs)) + fld(2, 5))


def proto_step(status: int, when: float, name: str, args: dict) -> bytes:
    call = fld(1, "call-1") + fld(2, name) + fld(3, json.dumps(args))
    return fld(1, 21) + fld(4, status) + fld(5, ts(1, when) + fld(4, call))


def summary(title: str, updated: float, run: int, workspace: str, steps=()) -> bytes:
    out = fld(1, title) + fld(2, 7) + ts(3, updated) + fld(5, run) + ts(7, updated - 300)
    out += fld(9, fld(1, Path(workspace).as_uri()))
    for num, step in steps:
        out += fld(num, fld(1, step) + fld(2, 3))
    return out


# ------------------------------------------------------------------ fixtures
def log_line(i: int, stype: str, when: float, **extra) -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    base = {"step_index": i, "source": "MODEL", "type": stype, "status": "DONE"}
    return {**base, "created_at": stamp, **extra}


def write_log(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def conversation(t0: float, tool_last: bool = False) -> list[dict]:
    request = (
        "<USER_REQUEST>\nAdd a dark mode\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\n"
        "The user changed setting `Model Selection` from None to Gemini 3 Pro (High). No need"
    )
    lines = [
        log_line(0, "USER_INPUT", t0, source="USER_EXPLICIT", content=request),
        log_line(
            3,
            "PLANNER_RESPONSE",
            t0 + 5,
            content="Looking at the theme first.",
            tool_calls=[
                {"name": "view_file", "args": {"AbsolutePath": '"/w/app/src/theme.ts"'}},
                {"name": "grep_search", "args": {"Query": '"--color"', "SearchPath": '"/w/app"'}},
            ],
        ),
        log_line(
            6,
            "PLANNER_RESPONSE",
            t0 + 9,
            tool_calls=[
                {
                    "name": "replace_file_content",
                    "args": {"TargetFile": '"/w/app/src/theme.ts"', "EndLine": "12"},
                }
            ],
        ),
    ]
    if not tool_last:
        lines.append(log_line(9, "PLANNER_RESPONSE", t0 + 14, content="Dark mode is in."))
    return lines


def make_state_db(path: Path, summaries: dict[str, bytes]) -> None:
    entries = b"".join(
        fld(1, fld(1, cid) + fld(2, fld(1, base64.b64encode(raw).decode())))
        for cid, raw in summaries.items()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table if not exists ItemTable (key text unique, value blob)")
        db.execute(
            "insert or replace into ItemTable values (?, ?)",
            (ag.SUMMARIES_KEY, base64.b64encode(entries).decode()),
        )


def ide(tmp_path: Path) -> ag.IdeSource:
    return ag.IdeSource(home=tmp_path / "antigravity", state_db=tmp_path / "state.vscdb", since=0)


def only_session(src: ag._Source):
    assert src.poll(min_interval=0)
    [session] = src.sessions()
    return session, session.to_dict(time.time(), detail=True)


# ------------------------------------------------------------------ tests
def test_decode_rejects_garbage() -> None:
    assert ag.decode(fld(1, 150) + fld(2, "hi")) == {1: [150], 2: [b"hi"]}
    with pytest.raises(ValueError):
        ag.decode(b"\x0a\x05ab")  # length runs past the end


def test_ide_log_becomes_a_timeline(tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000001"
    brain = tmp_path / "antigravity" / "brain" / conv
    write_log(brain / ".system_generated" / "logs" / "overview.txt", conversation(NOW - 60))
    (brain / "task.md").write_text("# Tasks\n- `[x]` Map tokens\n- `[/]` Add palette\n- [ ] Test\n")
    make_state_db(tmp_path / "state.vscdb", {conv: summary("Dark Mode", NOW - 45, 1, "/w/app")})

    _, d = only_session(ide(tmp_path))
    main = d["agents"][0]
    assert (d["source"], d["project"], d["title"], d["cwd"]) == (
        "antigravity",
        "app",
        "Dark Mode",
        "/w/app",
    )
    assert main["name"] == "Antigravity" and main["model"] == "Gemini 3 Pro (High)"
    assert [a["kind"] for a in main["activities"]] == [
        "prompt",
        "text",
        "tool",
        "tool",
        "tool",
        "text",
    ]
    assert main["activities"][0]["text"] == "Add a dark mode"
    read, grep, edit = (a for a in main["activities"] if a["kind"] == "tool")
    assert (read["verb"], read["target"], read["status"]) == ("Reading", "src/theme.ts", "ok")
    assert (grep["verb"], grep["target"]) == ("Searching", '"--color" in .')
    assert edit["verb"] == "Editing" and main["tool_count"] == 3
    assert main["files"][0] == {**main["files"][0], "rel": "src/theme.ts", "reads": 1, "writes": 1}
    assert [t["status"] for t in main["todos"]] == ["completed", "in_progress", "pending"]
    assert d["status"] == "waiting"


def test_unfinished_reply_is_running_until_summary_says_idle(tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000002"
    log = tmp_path / "antigravity" / "brain" / conv / ".system_generated" / "logs" / "overview.txt"
    write_log(log, conversation(NOW - 20, tool_last=True))
    src = ide(tmp_path)
    _, d = only_session(src)  # no summary at all: the log alone says a tool is running
    assert d["status"] == "working" and d["agents"][0]["current"]["verb"] == "Editing"

    make_state_db(tmp_path / "state.vscdb", {conv: summary("t", NOW - 5, 1, "/w/app")})
    _, d = only_session(src)
    assert d["status"] == "waiting" and d["agents"][0]["current"]["status"] == "ok"


def test_step_waiting_for_approval(tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000003"
    log = tmp_path / "antigravity" / "brain" / conv / ".system_generated" / "logs" / "overview.txt"
    write_log(log, conversation(NOW - 30))
    waiting = proto_step(9, NOW - 3, "run_command", {"CommandLine": "rm -rf build"})
    make_state_db(
        tmp_path / "state.vscdb", {conv: summary("t", NOW - 2, 2, "/w/app", [(8, waiting)])}
    )
    _, d = only_session(ide(tmp_path))
    cur = d["agents"][0]["current"]
    assert d["agents"][0]["status"] == "waiting"
    assert (cur["verb"], cur["target"], cur["status"]) == ("Running", "rm -rf build", "running")


def test_summary_without_log_still_shows_latest_task(tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000004"
    task = proto_step(
        3, NOW - 50, "task_boundary", {"TaskName": "Planning", "TaskStatus": "Reading"}
    )
    make_state_db(
        tmp_path / "state.vscdb", {conv: summary("Old chat", NOW - 40, 1, "/w/api", [(14, task)])}
    )
    _, d = only_session(ide(tmp_path))
    cur = d["agents"][0]["current"]
    assert (d["title"], d["project"], cur["verb"], cur["target"]) == (
        "Old chat",
        "api",
        "Planning",
        "Reading",
    )


def test_log_is_tailed(tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000005"
    log = tmp_path / "antigravity" / "brain" / conv / ".system_generated" / "logs" / "overview.txt"
    write_log(log, conversation(NOW - 60)[:1])
    src = ide(tmp_path)
    _, d = only_session(src)
    assert d["agents"][0]["status"] == "thinking"
    with log.open("a") as fh:
        fh.write(json.dumps(log_line(3, "PLANNER_RESPONSE", NOW - 50, content="Done.")) + "\n")
    _, d = only_session(src)
    assert d["agents"][0]["activities"][-1]["text"] == "Done." and d["status"] == "waiting"
    assert not src.poll(min_interval=0)  # nothing new


def test_cli_links_subagents(tmp_path: Path) -> None:
    home = tmp_path / "antigravity-cli"
    parent, child = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    for cid, lines in ((parent, conversation(NOW - 60)), (child, conversation(NOW - 40)[:2])):
        logs = home / "brain" / cid / ".system_generated" / "logs"
        write_log(logs / "transcript_full.jsonl", lines)
        write_log(logs / "transcript.jsonl", lines[:1])  # the full one wins
    with sqlite3.connect(home / "conversation_summaries.db") as db:
        db.execute(
            "create table conversation_summaries (conversation_id text, title text, preview text,"
            " workspace_uris text, status text, agent_name text, parent_conversation_id text,"
            " nesting_depth integer, raw_summary blob, app_data_dir text)"
        )
        rows = [
            (parent, "Dark mode", "", '["file:///w/app"]', "CASCADE_RUN_STATUS_RUNNING", "", "", 0),
            (
                child,
                "Audit colours",
                "",
                "[]",
                "CASCADE_RUN_STATUS_RUNNING",
                "DeepCoder",
                parent,
                1,
            ),
            ("cccccccc-ide", "IDE chat", "", "[]", "", "", "", 0),
        ]
        for row in rows:
            app = "antigravity" if row[0].endswith("ide") else "antigravity-cli"
            raw = summary(row[1], NOW - 10, 1, "/w/app")
            db.execute(
                "insert into conversation_summaries values (?,?,?,?,?,?,?,?,?,?)", (*row, raw, app)
            )

    _, d = only_session(ag.CliSource(home=home, since=0))
    main, sub = d["agents"]
    assert (d["source"], d["title"], d["cwd"], d["agent_count"]) == (
        "antigravity-cli",
        "Dark mode",
        "/w/app",
        2,
    )
    assert len(main["activities"]) == 6
    assert (sub["name"], sub["description"], sub["parent_id"], sub["depth"]) == (
        "DeepCoder",
        "Audit colours",
        parent,
        1,
    )
    assert sub["status"] == "tool"  # its last reply's calls are still running


def test_summarize_tool_fallbacks() -> None:
    s = ag.summarize_tool("run_command", {"CommandLine": "npm test\nnpm run lint"})
    assert (s.verb, s.target, s.category) == ("Running", "npm test", "shell")
    s = ag.summarize_tool("read_url_content", {"Url": "https://example.com/docs?x=1"})
    assert (s.verb, s.target) == ("Fetching", "example.com/docs")
    s = ag.summarize_tool("brand_new_tool", {"toolAction": "x", "toolSummary": "Doing a thing"})
    assert (s.verb, s.target, s.category) == ("Brand new tool", "Doing a thing", "other")


def test_hive_merges_sources(projects: Path, tmp_path: Path) -> None:
    conv = "c0ffee00-0000-0000-0000-000000000006"
    log = tmp_path / "antigravity" / "brain" / conv / ".system_generated" / "logs" / "overview.txt"
    write_log(log, conversation(time.time() - 30))
    hive = Hive(root=projects, since=0, sources=[ide(tmp_path)])
    assert hive.poll()
    snap = hive.snapshot(detail=conv)
    assert snap["sources"] == ["claude", "antigravity"]
    assert {s["source"] for s in snap["sessions"]} == {"claude", "antigravity"}
    assert next(s for s in snap["sessions"] if s["id"] == conv)["detail"]


def test_broken_state_db_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state.vscdb").write_bytes(b"not a database")
    src = ide(tmp_path)
    assert not src.poll(min_interval=0) and src.sessions() == []
