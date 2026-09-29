"""Antigravity (Google's agent IDE and its ``agy`` CLI) as a second source of sessions.

What Antigravity keeps on disk, under ``~/.gemini/antigravity`` (IDE) and
``~/.gemini/antigravity-cli`` (CLI):

* ``brain/<conversation>/.system_generated/logs/``: a JSONL log with one line per finished step
  (``overview.txt`` for the IDE, ``transcript_full.jsonl`` / ``transcript.jsonl`` for the CLI).
  User prompts, model replies and thinking, and every tool call with its arguments. It is
  tailed like a Claude Code transcript.
* ``brain/<conversation>/task.md``: the agent's markdown checklist, shown as its todo list.
* trajectory summaries, as protobuf: in the IDE's VS Code state database, and in the CLI's
  ``conversation_summaries.db``. They give the title, workspace, whether the agent is running,
  the tool step waiting for your approval, and (CLI) which conversation spawned which subagent.

The full conversations themselves (``conversations/*.pb``) are encrypted. None of this is
documented: field numbers were read from real data and enums from the ``agy`` binary, so
everything is decoded defensively and anything unknown is skipped.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import tools
from .model import Activity, Agent, FileTouch, Session, call_sig, parse_ts

IDE = "antigravity"
CLI = "antigravity-cli"
SUMMARIES_KEY = "antigravityUnifiedStateSync.trajectorySummaries"
INITIAL_TAIL_BYTES = 4 * 1024 * 1024
MAX_STEPS = 3000

# CortexStepType / CortexStepStatus / CascadeRunStatus values, as found in the agy binary.
STEP_TYPES = {14: "USER_INPUT", 15: "PLANNER_RESPONSE", 17: "ERROR_MESSAGE"}
STEP_STATUSES = {
    1: "PENDING",
    2: "RUNNING",
    3: "DONE",
    6: "CANCELED",
    7: "ERROR",
    8: "GENERATING",
    9: "WAITING",
    11: "QUEUED",
    12: "INTERRUPTED",
}
RUN_IDLE = 1
RUN_ACTIVE = {2, 4}  # running, busy
RUN_STATUS_NAMES = {
    "CASCADE_RUN_STATUS_IDLE": 1,
    "CASCADE_RUN_STATUS_RUNNING": 2,
    "CASCADE_RUN_STATUS_BUSY": 4,
}


def gemini_dir() -> Path:
    return Path.home() / ".gemini"


def ide_state_db() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Antigravity" / "User" / "globalStorage" / "state.vscdb"


def uri_to_path(uri: str) -> str:
    if not uri.startswith("file:"):
        return uri
    path = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///c:/... on Windows
        path = path[1:]
    return path


# ------------------------------------------------------------------ protobuf wire format
Fields = dict[int, list[Any]]


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def decode(buf: bytes) -> Fields:
    """Decode one protobuf message into ``{field: [values]}``; nested messages stay bytes."""
    out: Fields = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        value: Any
        if num == 0:
            raise ValueError("field 0")
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            size, i = _varint(buf, i)
            value = bytes(buf[i : i + size])
            i += size
        elif wire == 1:
            value, i = bytes(buf[i : i + 8]), i + 8
        elif wire == 5:
            value, i = bytes(buf[i : i + 4]), i + 4
        else:
            raise ValueError(f"wire type {wire}")
        if i > n:
            raise ValueError("truncated")
        out.setdefault(num, []).append(value)
    return out


def _msg(f: Fields, num: int) -> Fields:
    vals = f.get(num)
    if not vals or not isinstance(vals[0], bytes):
        return {}
    try:
        return decode(vals[0])
    except ValueError:
        return {}


def _str(f: Fields, num: int) -> str:
    vals = f.get(num)
    if not vals or not isinstance(vals[0], bytes):
        return ""
    return vals[0].decode("utf-8", "replace")


def _int(f: Fields, num: int) -> int:
    vals = f.get(num)
    return vals[0] if vals and isinstance(vals[0], int) else 0


def _ts(f: Fields, num: int) -> float | None:
    t = _msg(f, num)
    secs = _int(t, 1)
    return secs + _int(t, 2) / 1e9 if secs else None


# ------------------------------------------------------------------ steps
@dataclass
class Call:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Step:
    type: str
    status: str = "DONE"
    ts: float | None = None
    text: str = ""
    thinking: str = ""
    calls: list[Call] = field(default_factory=list)


def _decode_args(args: Any) -> dict[str, Any]:
    """Tool arguments; in the logs each value is itself JSON-encoded (``"\\"/path\\""``)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in args.items():
        if isinstance(value, str) and value[:1] in ('"', "{", "["):
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        out[key] = value
    return out


def step_from_log(obj: dict[str, Any]) -> Step | None:
    """One line of ``overview.txt`` / ``transcript.jsonl``."""
    stype = obj.get("type")
    if not isinstance(stype, str):
        return None
    step = Step(
        type=stype,
        status=str(obj.get("status") or "DONE"),
        ts=parse_ts(obj.get("created_at")),
        text=str(obj.get("content") or ""),
        thinking=str(obj.get("thinking") or ""),
    )
    index = obj.get("step_index", "")
    for i, call in enumerate(obj.get("tool_calls") or []):
        if isinstance(call, dict) and call.get("name"):
            cid = str(call.get("id") or f"{index}:{i}")
            step.calls.append(Call(cid, str(call["name"]), _decode_args(call.get("args"))))
    return step


def step_from_proto(raw: bytes) -> Step | None:
    """A ``CortexStep`` message, as embedded in trajectory summaries."""
    try:
        f = decode(raw)
    except ValueError:
        return None
    meta = _msg(f, 5)
    step = Step(
        type=STEP_TYPES.get(_int(f, 1), "TOOL"),
        status=STEP_STATUSES.get(_int(f, 4), "DONE"),
        ts=_ts(meta, 1),
    )
    call = _msg(meta, 4)
    if call and _str(call, 2):
        step.calls.append(Call(_str(call, 1), _str(call, 2), _decode_args(_str(call, 3))))
    return step


# ------------------------------------------------------------------ summaries
@dataclass
class Summary:
    id: str
    title: str = ""
    updated: float | None = None
    created: float | None = None
    run_status: int = 0
    workspace: str = ""
    steps: list[Step] = field(default_factory=list)  # latest task, message and pending step
    parent: str = ""
    agent_name: str = ""
    depth: int = 0


def parse_summary(conv_id: str, raw: bytes) -> Summary:
    s = Summary(conv_id)
    try:
        f = decode(raw)
    except ValueError:
        return s
    s.title = _str(f, 1)
    s.updated = _ts(f, 3)
    s.run_status = _int(f, 5)
    s.created = _ts(f, 7)
    s.workspace = uri_to_path(_str(_msg(f, 9), 1))
    for num in (8, 12, 14):
        for holder in f.get(num, []):
            with contextlib.suppress(ValueError):
                inner = decode(holder).get(1)
                step = step_from_proto(inner[0]) if inner else None
                if step:
                    s.steps.append(step)
    s.steps.sort(key=lambda st: st.ts or 0)
    return s


def parse_summary_list(value: str | bytes) -> list[Summary]:
    """The IDE's ``trajectorySummaries``: base64 of ``{id, {base64(summary)}}`` entries."""
    out = []
    for entry in decode(base64.b64decode(value)).get(1, []):
        with contextlib.suppress(ValueError):
            e = decode(entry)
            conv_id = _str(e, 1)
            inner = _str(_msg(e, 2), 1)
            if conv_id and inner:
                out.append(parse_summary(conv_id, base64.b64decode(inner)))
    return out


# ------------------------------------------------------------------ tool calls
READ_TOOLS = {"view_file", "read_file", "view_file_outline", "view_code_item", "read_notebook"}
WRITE_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "edit_file",
    "edit_notebook",
}
FILE_TOOLS = READ_TOOLS | WRITE_TOOLS
# name -> (verb, category); categories pick the same icons/colours as tools.CATEGORIES
TOOLS: dict[str, tuple[str, str]] = {
    **dict.fromkeys(READ_TOOLS, ("Reading", "read")),
    "write_to_file": ("Writing", "edit"),
    "replace_file_content": ("Editing", "edit"),
    "multi_replace_file_content": ("Editing", "edit"),
    "edit_file": ("Editing", "edit"),
    "edit_notebook": ("Editing notebook", "edit"),
    "run_command": ("Running", "shell"),
    "shell_exec": ("Running", "shell"),
    "command_status": ("Checking command", "shell"),
    "send_command_input": ("Typing into command", "shell"),
    "read_terminal": ("Reading terminal", "shell"),
    "grep_search": ("Searching", "search"),
    "codebase_search": ("Searching code", "search"),
    "find_by_name": ("Finding files", "search"),
    "list_dir": ("Listing", "search"),
    "read_url_content": ("Fetching", "web"),
    "open_browser_url": ("Opening", "web"),
    "search_web": ("Searching the web", "web"),
    "browser_subagent": ("Browsing", "agent"),
    "invoke_subagent": ("Spawning subagents", "agent"),
    "define_subagent": ("Defining subagent", "agent"),
    "manage_subagents": ("Managing subagents", "agent"),
    "send_message": ("Messaging", "agent"),
    "task_boundary": ("Task", "plan"),
    "manage_task": ("Updating tasks", "plan"),
    "schedule": ("Scheduling", "plan"),
    "ask_question": ("Asking you", "ask"),
    "generate_image": ("Generating image", "other"),
}
PATH_KEYS = ("AbsolutePath", "TargetFile", "File", "FilePath", "Path", "DirectoryPath")


def _arg(args: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def summarize_tool(name: str, args: dict[str, Any], cwd: str = "") -> tools.ToolSummary:
    verb, cat = TOOLS.get(name, ("", ""))
    if not verb:
        cat = "mcp" if name.startswith("mcp") else "other"
        verb = name.replace("_", " ").strip().capitalize() or "Tool"
    path = uri_to_path(_arg(args, *PATH_KEYS))

    if name in ("run_command", "shell_exec"):
        cmd = _arg(args, "CommandLine", "Command")
        first = cmd.strip().splitlines()[0] if cmd.strip() else ""
        return tools.ToolSummary(verb, tools.shorten(first, 70), cmd, cat)
    if name in ("grep_search", "codebase_search", "find_by_name"):
        query = _arg(args, "Query", "Pattern")
        where = uri_to_path(_arg(args, "SearchPath", "SearchDirectory"))
        target = query if name == "find_by_name" else f'"{tools.shorten(query, 40)}"'
        if where:
            target += f" in {tools.relpath(where, cwd)}"
        return tools.ToolSummary(verb, target, query, cat)
    if name in ("read_url_content", "open_browser_url"):
        url = _arg(args, "Url")
        parsed = urlparse(url)
        return tools.ToolSummary(verb, tools.shorten(parsed.netloc + parsed.path, 60), url, cat)
    if name == "task_boundary":
        task = _arg(args, "TaskName") or verb
        status = _arg(args, "TaskStatus")
        return tools.ToolSummary(task, tools.shorten(status, 70), _arg(args, "TaskSummary"), cat)
    if name == "browser_subagent":
        task = _arg(args, "TaskName", "Task")
        return tools.ToolSummary(verb, tools.shorten(task, 60), _arg(args, "Task"), cat)
    if path:
        return tools.ToolSummary(verb, tools.relpath(path, cwd), path, cat)
    target = _arg(args, "toolSummary", "Query", "query", "Message", "Prompt", "CommandId")
    if not target:
        target = next(
            (v for k, v in args.items() if isinstance(v, str) and v.strip() and k != "toolAction"),
            "",
        )
    return tools.ToolSummary(verb, tools.shorten(target, 60), "", cat)


# ------------------------------------------------------------------ building agents
TODO_LINE = re.compile(r"^\s*[-*]\s+`?\[([ xX/~-])\]`?\s+(.+?)\s*$")
TODO_STATUS = {"x": "completed", "X": "completed", "/": "in_progress"}
USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
MODEL_CHANGE = re.compile(r"setting `Model Selection` from .*? to (.+?)\. ")


def read_todos(path: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    todos = []
    for line in text.splitlines():
        m = TODO_LINE.match(line)
        if m and m.group(1) not in "~-":
            content = m.group(2)
            status = TODO_STATUS.get(m.group(1), "pending")
            todos.append({"content": content, "status": status, "active": content})
    return todos[:60]


def fill_agent(agent: Agent, steps: list[Step], summary: Summary | None = None) -> None:
    """Replay steps into ``agent`` (activities, files, phase), the way Agent.ingest does."""
    last_calls: list[Activity] = []
    last: Step | None = None
    for st in steps:
        ts = st.ts or agent.last_ts or time.time()
        agent.started = min(agent.started or ts, ts)
        agent.last_ts = max(agent.last_ts or 0, ts)
        if st.type == "USER_INPUT":
            _user_input(agent, st.text, ts)
        elif st.type == "PLANNER_RESPONSE":
            if st.thinking.strip():
                agent.last_thought = _add(agent, Activity("thinking", ts, text=st.thinking))
            if st.text.strip():
                agent.last_thought = _add(agent, Activity("text", ts, text=st.text))
            last_calls = [_tool(agent, call, ts) for call in st.calls]
        elif st.type == "ERROR_MESSAGE" and st.text.strip():
            _add(agent, Activity("error", ts, text=st.text))
        elif st.calls and st.status != "WAITING":  # a tool step from a summary
            for call in st.calls:
                _tool(agent, call, ts)
        else:
            continue
        if last_calls and st.type != "PLANNER_RESPONSE":
            last_calls = []
        last = st

    # Only finished steps are logged: the calls of the latest reply are still running.
    blocked = False
    for act in last_calls:
        if act.tool == "notify_user":
            blocked = True
        elif act.tool != "task_boundary":
            act.status, act.ended = "running", None
            agent.pending[act.tool_id] = act
    if agent.pending:
        agent.phase = "tool"
    elif last is not None and last.type == "USER_INPUT":
        agent.phase = "thinking"
    else:
        agent.phase = "waiting" if agent.kind == "main" or blocked else "done"

    if summary is not None:
        _apply_summary(agent, summary)


def _user_input(agent: Agent, text: str, ts: float) -> None:
    model = MODEL_CHANGE.search(text)
    if model:
        agent.model = model.group(1).strip()
    m = USER_REQUEST.search(text)
    text = m.group(1) if m else text
    if not text.strip():
        return
    if agent.kind == "sub" and not agent.task:
        agent.task = text
    agent.last_prompt = text
    _add(agent, Activity("prompt", ts, text=text))


def _add(agent: Agent, act: Activity) -> Activity:
    agent.activities.append(act)
    return act


def _tool(agent: Agent, call: Call, ts: float) -> Activity:
    if call.name == "notify_user":
        msg = _arg(call.args, "Message")
        act = Activity("text", ts, text=msg, tool=call.name)
        if msg:
            agent.last_thought = _add(agent, act)
        return act
    s = summarize_tool(call.name, call.args, agent.cwd)
    act = Activity(
        "tool",
        ts,
        tool=call.name,
        verb=s.verb,
        target=s.target,
        detail=s.detail,
        category=s.category,
        status="ok",
        tool_id=call.id,
        ended=ts,
        sig=call_sig(call.name, call.args),
    )
    _add(agent, act)
    if call.name != "task_boundary":
        agent.tool_count += 1
    cwd = _arg(call.args, "Cwd")
    if cwd and not agent.cwd:
        agent.cwd = cwd
    if call.name in FILE_TOOLS:
        path = uri_to_path(_arg(call.args, *PATH_KEYS))
        if path:
            touch = agent.files.setdefault(path, FileTouch(path))
            if call.name in WRITE_TOOLS:
                touch.writes += 1
            else:
                touch.reads += 1
            touch.ts = ts
    return act


def _apply_summary(agent: Agent, summary: Summary) -> None:
    """Trajectory summaries know if the agent runs and what waits for approval, when fresh."""
    newest = agent.last_ts or 0
    if not summary.updated or summary.updated < newest - 1:
        return  # the log has moved on since this summary was written
    agent.last_ts = max(newest, summary.updated)
    if summary.run_status == RUN_IDLE:
        for act in agent.pending.values():
            act.status, act.ended = "ok", act.ts
        agent.pending.clear()
        agent.phase = "waiting" if agent.kind == "main" else "done"
    elif summary.run_status in RUN_ACTIVE:
        for st in summary.steps:
            if st.status == "WAITING" and st.calls and (st.ts or 0) >= newest - 1:
                act = _tool(agent, st.calls[0], st.ts or newest)
                act.status, act.ended = "running", None
                agent.pending[act.tool_id] = act
                agent.phase = "waiting"
                return
        if agent.phase in ("waiting", "done"):
            agent.phase = "thinking"


# ------------------------------------------------------------------ sources
def _sig(*paths: Path) -> tuple[float, ...]:
    out: list[float] = []
    for p in paths:
        try:
            st = p.stat()
            out += [st.st_mtime, st.st_size]
        except OSError:
            out += [0, 0]
    return tuple(out)


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)


@dataclass
class _Log:
    path: Path
    offset: int = 0
    buffer: bytes = b""
    mtime: float = 0.0
    steps: list[Step] = field(default_factory=list)

    def read(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size < self.offset:  # rewritten
            self.offset, self.buffer, self.steps = 0, b"", []
        if st.st_size == self.offset:
            return False
        skip_partial = False
        if self.offset == 0 and st.st_size > INITIAL_TAIL_BYTES:
            self.offset = st.st_size - INITIAL_TAIL_BYTES
            skip_partial = True
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(st.st_size - self.offset)
        except OSError:
            return False
        self.offset += len(chunk)
        self.mtime = st.st_mtime
        lines = (self.buffer + chunk).split(b"\n")
        self.buffer = lines.pop()
        if skip_partial and lines:
            lines.pop(0)
        for raw in lines:
            with contextlib.suppress(ValueError):
                obj = json.loads(raw)
                step = step_from_log(obj) if isinstance(obj, dict) else None
                if step:
                    self.steps.append(step)
        del self.steps[:-MAX_STEPS]
        return True


class _Source:
    """Conversations of one Antigravity app: step logs under ``brain/``, plus its summaries."""

    name = ""
    log_names: tuple[str, ...] = ()

    def __init__(
        self, home: Path, since: float, project_filter: str | None, max_sessions: int
    ) -> None:
        self.home = home
        self.since = since
        self.project_filter = project_filter.lower() if project_filter else None
        self.max_sessions = max_sessions
        self._summaries: dict[str, Summary] = {}
        self._summary_sig: tuple[Any, ...] = ()
        self._logs: dict[str, _Log] = {}
        self._sessions: list[Session] = []
        self._last_poll = self._last_scan = 0.0
        self._dirty = False

    @property
    def brain(self) -> Path:
        return self.home / "brain"

    # subclasses
    def _summary_paths(self) -> tuple[Path, ...]:
        raise NotImplementedError

    def _load_summaries(self) -> dict[str, Summary]:
        raise NotImplementedError

    def _log_path(self, conv: Path) -> Path | None:
        logs = conv / ".system_generated" / "logs"
        return next((logs / n for n in self.log_names if (logs / n).is_file()), None)

    def _cutoff(self) -> float:
        return time.time() - self.since if self.since > 0 else 0

    def _scan(self) -> None:
        sig = _sig(*self._summary_paths())
        if sig != self._summary_sig:
            self._summary_sig = sig
            with contextlib.suppress(sqlite3.Error, ValueError, OSError):
                self._summaries = self._load_summaries()
                self._dirty = True
        cutoff = self._cutoff()
        wanted = {cid for cid, s in self._summaries.items() if (s.updated or 0) >= cutoff}
        with contextlib.suppress(OSError):
            for conv in self.brain.iterdir():
                if conv.name in self._logs or not conv.is_dir():
                    continue
                path = self._log_path(conv)
                if path is None:
                    continue
                with contextlib.suppress(OSError):
                    if conv.name in wanted or path.stat().st_mtime >= cutoff:
                        self._logs[conv.name] = _Log(path)

    def poll(self, min_interval: float = 0.8) -> bool:
        now = time.monotonic()
        if now - self._last_poll < min_interval:
            return False
        self._last_poll = now
        if now - self._last_scan > 2.0 or not min_interval:
            self._last_scan = now
            self._scan()
        for log in self._logs.values():
            self._dirty |= log.read()
        if not self._dirty:
            return False
        self._dirty = False
        self._sessions = self._build()
        return True

    def sessions(self) -> list[Session]:
        return self._sessions

    def _build(self) -> list[Session]:
        cutoff = self._cutoff()
        ids = set(self._logs) | {
            cid for cid, s in self._summaries.items() if (s.updated or 0) >= cutoff
        }

        def root_of(cid: str) -> str:
            seen = {cid}
            parent = self._summaries.get(cid, Summary(cid)).parent
            while parent and parent in ids and parent not in seen:
                cid = parent
                seen.add(cid)
                parent = self._summaries.get(cid, Summary(cid)).parent
            return cid

        sessions: dict[str, Session] = {}
        subs: list[tuple[str, str]] = []
        for cid in ids:
            root = root_of(cid)
            if root != cid:
                subs.append((cid, root))
                continue
            summary = self._summaries.get(cid)
            main = Agent(id=cid, session_id=cid, kind="main", source=self.name)
            self._fill(main, summary)
            project = main.cwd or "no workspace"
            sessions[cid] = Session(id=cid, project_dir=project, main=main, source=self.name)
        for cid, root in subs:
            session = sessions.get(root)
            if session is None:
                continue
            summary = self._summaries.get(cid, Summary(cid))
            sub = Agent(
                id=cid,
                session_id=root,
                kind="sub",
                source=self.name,
                parent_id=summary.parent,
                agent_type=summary.agent_name or "subagent",
                description=summary.title,
                depth=max(summary.depth, 1),
            )
            sub.cwd = session.main.cwd
            self._fill(sub, summary)
            session.subagents[cid] = sub

        out = []
        for session in sessions.values():
            session.link_subagents()
            if session.last_ts < cutoff:
                continue
            if self.project_filter and self.project_filter not in session.main.cwd.lower():
                continue
            out.append(session)
        out.sort(key=lambda s: s.last_ts, reverse=True)
        return out[: self.max_sessions]

    def _fill(self, agent: Agent, summary: Summary | None) -> None:
        if summary is not None:
            agent.cwd = summary.workspace or agent.cwd
            agent.title = summary.title
            agent.started = summary.created
        log = self._logs.get(agent.id)
        fill_agent(agent, log.steps if log else summary.steps if summary else [], summary)
        agent.todos = read_todos(self.brain / agent.id / "task.md") or agent.todos
        if not agent.cwd and agent.files:  # no workspace known: where its files live
            with contextlib.suppress(ValueError):
                agent.cwd = os.path.commonpath([os.path.dirname(f) for f in agent.files])


class IdeSource(_Source):
    """The Antigravity IDE. Its summaries live in the IDE's VS Code state database, which the
    IDE flushes every so often, so the running/idle status can lag behind the step log."""

    name = IDE
    log_names = ("overview.txt",)

    def __init__(
        self,
        home: Path | None = None,
        state_db: Path | None = None,
        since: float = 6 * 3600,
        project_filter: str | None = None,
        max_sessions: int = 40,
    ) -> None:
        super().__init__(home or gemini_dir() / "antigravity", since, project_filter, max_sessions)
        self.state_db = state_db or ide_state_db()

    def _summary_paths(self) -> tuple[Path, ...]:
        return (self.state_db, self.state_db.with_name(self.state_db.name + "-wal"))

    def _load_summaries(self) -> dict[str, Summary]:
        if not self.state_db.is_file():
            return {}
        with contextlib.closing(_connect(self.state_db)) as db:
            row = db.execute(
                "select value from ItemTable where key = ?", (SUMMARIES_KEY,)
            ).fetchone()
        if not row or not row[0]:
            return {}
        return {s.id: s for s in parse_summary_list(row[0])}


class CliSource(_Source):
    """The Antigravity CLI (``agy``), whose summaries also link subagents to their parent."""

    name = CLI
    log_names = ("transcript_full.jsonl", "transcript.jsonl")

    def __init__(
        self,
        home: Path | None = None,
        since: float = 6 * 3600,
        project_filter: str | None = None,
        max_sessions: int = 40,
    ) -> None:
        home = home or gemini_dir() / "antigravity-cli"
        super().__init__(home, since, project_filter, max_sessions)

    @property
    def index(self) -> Path:
        return self.home / "conversation_summaries.db"

    def _summary_paths(self) -> tuple[Path, ...]:
        return (self.index, self.index.with_name(self.index.name + "-wal"))

    def _load_summaries(self) -> dict[str, Summary]:
        if not self.index.is_file():
            return {}
        query = (
            "select conversation_id, title, preview, workspace_uris, status, agent_name,"
            " parent_conversation_id, nesting_depth, raw_summary from conversation_summaries"
            " where app_data_dir in ('antigravity-cli', '')"
        )
        with contextlib.closing(_connect(self.index)) as db:
            rows = db.execute(query).fetchall()
        out = {}
        for cid, title, preview, uris, status, agent_name, parent, depth, raw in rows:
            s = parse_summary(cid, raw) if raw else Summary(cid)
            s.title = s.title or title or preview or ""
            s.run_status = RUN_STATUS_NAMES.get(status or "", s.run_status)
            s.parent, s.agent_name, s.depth = parent or "", agent_name or "", int(depth or 0)
            if not s.workspace:
                with contextlib.suppress(ValueError, TypeError, IndexError):
                    s.workspace = uri_to_path(json.loads(uris)[0])
            out[cid] = s
        return out


def default_sources(
    since: float, project_filter: str | None = None, max_sessions: int = 40
) -> list[_Source]:
    return [
        IdeSource(since=since, project_filter=project_filter, max_sessions=max_sessions),
        CliSource(since=since, project_filter=project_filter, max_sessions=max_sessions),
    ]
