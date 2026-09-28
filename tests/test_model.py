from __future__ import annotations

import json
import os
from typing import Any

from specops.model import IDLE_AFTER, IDLE_AFTER_TOOL, Agent, Session, parse_ts

from .conftest import FIXTURES, PROJECT, SESSION_ID

T0 = parse_ts("2026-09-28T10:00:00Z") or 0.0


def ts(offset: float = 0) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(T0 + offset, tz=timezone.utc).isoformat()


def user(content: Any, at: float = 0, **extra: Any) -> dict[str, Any]:
    message = {"role": "user", "content": content}
    return {"type": "user", "message": message, "timestamp": ts(at), **extra}


def assistant(*blocks: dict[str, Any], at: float = 0, stop: str | None = None, mid: str = "m"):
    message = {"id": mid, "model": "claude-opus-5-5", "content": list(blocks), "stop_reason": stop}
    return {"type": "assistant", "timestamp": ts(at), "message": message}


def tool_use(tid: str, name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def tool_result(tid: str, text: str = "ok", error: bool = False) -> list[dict[str, Any]]:
    return [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}]


def main_agent() -> Agent:
    return Agent(id="s", session_id="s", kind="main")


def load(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- phases
def test_prompt_then_tool_then_reply_phases() -> None:
    a = main_agent()
    a.ingest(user("hello", cwd="/w"))
    assert a.phase == "thinking" and a.cwd == "/w" and a.last_prompt == "hello"

    a.ingest(assistant(tool_use("t1", "Read", file_path="/w/x.py"), at=1, stop="tool_use"))
    assert a.phase == "tool" and "t1" in a.pending
    assert a.current() is a.pending["t1"]

    a.ingest(user(tool_result("t1", "contents"), at=2))
    assert a.phase == "thinking" and not a.pending
    done = a.activities[-1]
    assert (done.status, done.result, done.ended) == ("ok", "contents", T0 + 2)

    a.ingest(assistant({"type": "text", "text": "All done"}, at=3, stop="end_turn"))
    assert a.phase == "waiting"
    assert a.last_thought is not None and a.last_thought.text == "All done"


def test_subagent_finishes_as_done_and_records_task() -> None:
    a = Agent(id="x", session_id="s", kind="sub")
    a.ingest(user("Do the thing"))
    assert a.task == "Do the thing"
    a.ingest(assistant({"type": "text", "text": "Did it"}, stop="end_turn"))
    assert a.phase == "done"


def test_end_turn_with_pending_tools_keeps_working() -> None:
    a = main_agent()
    a.ingest(assistant(tool_use("t1", "Bash", command="sleep 9"), stop="end_turn"))
    assert a.phase == "tool"
    a.ingest({"type": "system", "subtype": "turn_duration", "timestamp": ts(1)})
    assert a.phase == "tool"


def test_turn_duration_marks_waiting() -> None:
    a = main_agent()
    a.ingest(user("hi"))
    a.ingest({"type": "system", "subtype": "turn_duration", "timestamp": ts(1)})
    assert a.phase == "waiting"


def test_interrupt_fails_pending_tools() -> None:
    a = main_agent()
    a.ingest(assistant(tool_use("t1", "Bash", command="make")))
    a.ingest(user([{"type": "text", "text": "[Request interrupted by user for tool use]"}], at=5))
    assert a.phase == "interrupted" and not a.pending
    tool = next(x for x in a.activities if x.kind == "tool")
    assert (tool.status, tool.ended) == ("error", T0 + 5)
    assert a.activities[-1].kind == "interrupt"


def test_api_error() -> None:
    a = main_agent()
    a.ingest({**assistant({"type": "text", "text": "overloaded"}), "isApiErrorMessage": True})
    assert a.phase == "error" and a.activities[-1].kind == "error"


def test_tool_error_result() -> None:
    a = main_agent()
    a.ingest(assistant(tool_use("t1", "Bash", command="false")))
    a.ingest(user(tool_result("t1", "exit 1", error=True)))
    assert a.activities[-1].status == "error"


def test_unknown_tool_result_is_ignored() -> None:
    a = main_agent()
    a.ingest(user(tool_result("nope")))
    assert not a.activities and a.phase == "idle"


# --------------------------------------------------------------------------- noise & meta
def test_meta_and_noise_are_not_prompts() -> None:
    a = main_agent()
    a.ingest(user("<system-reminder>x</system-reminder>"))
    a.ingest(user("real one", isMeta=True))
    a.ingest(user("Caveat: the messages below..."))
    a.ingest(user("   "))
    assert not a.activities and a.last_prompt == ""


def test_slash_commands_become_command_activities() -> None:
    a = main_agent()
    a.ingest(user("<command-name>/compact</command-name><command-args></command-args>"))
    assert a.activities[-1].kind == "command" and a.activities[-1].text == "/compact"
    assert a.last_prompt == ""


def test_titles() -> None:
    a = main_agent()
    a.ingest({"type": "summary", "summary": "From summary"})
    assert a.title == "From summary"
    a.ingest({"type": "ai-title", "aiTitle": "AI title"})
    a.ingest({"type": "summary", "summary": "ignored once titled"})
    assert a.title == "AI title"
    a.ingest({"type": "custom-title", "customTitle": "Mine"})
    assert a.title == "Mine"


def test_synthetic_model_names_are_ignored() -> None:
    a = main_agent()
    a.ingest(assistant({"type": "text", "text": "x"}))
    event = assistant({"type": "text", "text": "y"})
    event["message"]["model"] = "<synthetic>"
    a.ingest(event)
    assert a.model == "claude-opus-5-5"


# --------------------------------------------------------------------------- bookkeeping
def test_files_todos_and_usage() -> None:
    a = main_agent()
    a.ingest(assistant(tool_use("r1", "Read", file_path="/w/a.py")))
    a.ingest(assistant(tool_use("e1", "Edit", file_path="/w/a.py")))
    a.ingest(assistant(tool_use("e2", "Write", file_path="/w/b.py")))
    touch = a.files["/w/a.py"]
    assert (touch.reads, touch.writes) == (1, 1)
    assert a.files["/w/b.py"].writes == 1

    a.ingest(
        assistant(
            tool_use(
                "td",
                "TodoWrite",
                todos=[
                    {"content": "A", "status": "completed"},
                    {"content": "B", "activeForm": "Bing"},
                ],
            )
        )
    )
    assert a.todos == [
        {"content": "A", "status": "completed", "active": ""},
        {"content": "B", "status": "pending", "active": "Bing"},
    ]
    assert a.tool_count == 4


def test_usage_is_counted_once_per_message_id() -> None:
    a = main_agent()
    for block in ({"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "hi"}):
        event = assistant(block, mid="msg_same")
        usage = {"input_tokens": 3, "output_tokens": 10, "cache_read_input_tokens": 5}
        event["message"]["usage"] = usage
        a.ingest(event)
    assert a.tokens() == {"in": 3, "out": 10, "cache": 5}


def test_status_goes_idle_after_silence() -> None:
    a = main_agent()
    a.ingest(user("hi"))
    assert a.status(T0 + IDLE_AFTER - 1) == "thinking"
    assert a.status(T0 + IDLE_AFTER + 1) == "idle"
    a.ingest(assistant(tool_use("t", "Bash", command="make")))
    assert a.status(T0 + IDLE_AFTER + 1) == "tool"  # slow builds are fine
    assert a.status(T0 + IDLE_AFTER_TOOL + 1) == "idle"


def test_activity_to_dict_compact_trims() -> None:
    a = main_agent()
    a.ingest(assistant({"type": "text", "text": "x" * 1000}))
    act = a.activities[-1]
    assert len(act.to_dict(compact=True)["text"]) == 360
    assert len(act.to_dict()["text"]) == 1000


# --------------------------------------------------------------------------- fixture session
def fixture_session() -> Session:
    project = FIXTURES / "projects" / PROJECT
    main = Agent(id=SESSION_ID, session_id=SESSION_ID, kind="main")
    for event in load(project / f"{SESSION_ID}.jsonl"):
        main.ingest(event)
    sub = Agent(id="a1b2c3", session_id=SESSION_ID, kind="sub", parent_tool_id="toolu_agent1")
    for event in load(project / SESSION_ID / "subagents" / "agent-a1b2c3.jsonl"):
        sub.ingest(event)
    session = Session(id=SESSION_ID, project_dir=PROJECT, main=main, subagents={"a1b2c3": sub})
    session.link_subagents()
    return session


def test_fixture_session_end_to_end() -> None:
    s = fixture_session()
    now = T0 + 30
    assert s.title() == "Fix flaky checkout test"
    assert s.project_name() == "shop"
    assert s.status(now) == "waiting"
    assert s.main.last_prompt == "The checkout test is flaky, can you fix it?"
    assert [x.kind for x in s.main.activities][:2] == ["command", "prompt"]
    assert s.main.branch == "main" and s.main.model == "claude-opus-5-5"
    assert s.main.tokens()["out"] == 80 + 40 + 60 + 90 + 30  # msg_01 counted once

    sub = s.subagents["a1b2c3"]
    assert sub.parent_id == SESSION_ID and sub.status(now) == "done"
    assert sub.task.startswith("Run tests/test_checkout.py")
    spawn = next(x for x in s.main.activities if x.tool == "Agent")
    assert spawn.agent_ref == "a1b2c3"
    assert spawn.result == "3 of 50 runs failed on the payment mock."

    d = s.to_dict(now, detail=True)
    assert d["agent_count"] == 2 and d["working"] == 0
    assert d["agents"][0]["files"][0]["rel"] == os.path.join("tests", "test_checkout.py")
    brief = s.to_dict(now)
    assert set(brief["agents"][0]) == {"id", "kind", "name", "description", "status", "last_ts"}
    json.dumps(d)  # snapshots must be JSON serialisable


def test_nested_subagent_links_to_spawning_subagent() -> None:
    main = main_agent()
    main.ingest(assistant(tool_use("spawn1", "Agent", description="outer")))
    outer = Agent(id="outer", session_id="s", kind="sub", parent_tool_id="spawn1")
    outer.ingest(assistant(tool_use("spawn2", "Agent", description="inner")))
    inner = Agent(id="inner", session_id="s", kind="sub", parent_tool_id="spawn2")
    orphan = Agent(id="orphan", session_id="s", kind="sub", parent_tool_id="unknown")
    s = Session("s", "p", main, {"outer": outer, "inner": inner, "orphan": orphan})
    s.link_subagents()
    assert (outer.parent_id, inner.parent_id, orphan.parent_id) == ("s", "outer", "s")
    assert outer.activities[-1].agent_ref == "inner"


def test_session_status_is_working_if_any_agent_works() -> None:
    s = fixture_session()
    s.subagents["a1b2c3"].ingest(user("more work", at=40))
    assert s.status(T0 + 41) == "working"
