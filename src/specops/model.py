"""In-memory model of Claude Code sessions and agents, built from transcript events.

Claude Code writes one JSON object per line to ``~/.claude/projects/<project>/<session>.jsonl``
and, for subagents, to ``<session>/subagents/agent-<id>.jsonl``. The format is internal and
undocumented, so every field access here is defensive: unknown events are ignored rather than
crashing the viewer.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import tools

MAX_ACTIVITIES = 400
# Fields that say who an agent is, as opposed to what was learnt from its transcript.
AGENT_IDENTITY = (
    "id",
    "session_id",
    "kind",
    "path",
    "parent_tool_id",
    "parent_id",
    "agent_type",
    "description",
    "depth",
)

# Phases an agent can be in. "Working" phases are shown as live.
WORKING = {"thinking", "writing", "tool"}

# After this many seconds without new events a "working" agent is presumed gone
# (process killed, laptop suspended, ...). Tools get longer because builds/tests can be slow.
IDLE_AFTER = 180.0
IDLE_AFTER_TOOL = 1800.0

INTERRUPT_MARKERS = ("[Request interrupted by user", "[Request cancelled by user")


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text_of(content: Any) -> str:
    """Flatten a message/tool_result ``content`` (str or list of blocks) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _is_noise(text: str) -> bool:
    t = text.lstrip()
    return (
        not t
        or t.startswith("<system-reminder>")
        or t.startswith("<local-command-")
        or t.startswith("<command-message>")
        or t.startswith("Caveat:")
    )


@dataclass
class Activity:
    kind: str  # prompt | thinking | text | tool | error | command | interrupt
    ts: float
    text: str = ""
    tool: str = ""
    verb: str = ""
    target: str = ""
    detail: str = ""
    category: str = ""
    status: str = ""  # tool: running | ok | error
    result: str = ""
    tool_id: str = ""
    ended: float | None = None
    agent_ref: str = ""  # tool spawned this subagent

    def to_dict(self, compact: bool = False) -> dict[str, Any]:
        d = {
            "kind": self.kind,
            "ts": self.ts,
            "text": self.text[-360:] if compact else self.text[:6000],
        }
        if self.kind == "tool":
            d.update(
                id=self.tool_id,
                tool=self.tool,
                verb=self.verb,
                target=self.target,
                detail="" if compact else self.detail[:3000],
                category=self.category,
                status=self.status,
                result="" if compact else self.result[:1500],
                ended=self.ended,
                agent_ref=self.agent_ref,
            )
        return d


@dataclass
class FileTouch:
    path: str
    reads: int = 0
    writes: int = 0
    ts: float = 0.0


@dataclass
class Agent:
    id: str
    session_id: str
    kind: str  # "main" | "sub"
    path: str = ""
    parent_tool_id: str = ""
    parent_id: str = ""
    agent_type: str = ""
    description: str = ""
    depth: int = 0
    task: str = ""
    cwd: str = ""
    branch: str = ""
    model: str = ""
    title: str = ""
    phase: str = "idle"
    started: float | None = None
    last_ts: float | None = None
    activities: deque[Activity] = field(default_factory=lambda: deque(maxlen=MAX_ACTIVITIES))
    pending: dict[str, Activity] = field(default_factory=dict)
    files: dict[str, FileTouch] = field(default_factory=dict)
    todos: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    tool_count: int = 0
    last_thought: Activity | None = None
    last_prompt: str = ""

    def reset(self) -> None:
        """Forget everything ingested (the transcript was rewritten), keeping who this agent is."""
        fresh = Agent(**{name: getattr(self, name) for name in AGENT_IDENTITY})
        self.__dict__.update(fresh.__dict__)

    # ------------------------------------------------------------------ ingest
    def ingest(self, obj: dict[str, Any]) -> None:
        etype = obj.get("type")
        ts = parse_ts(obj.get("timestamp"))
        if ts is not None:
            if self.started is None:
                self.started = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        ts = ts or self.last_ts or time.time()
        if obj.get("cwd"):
            self.cwd = obj["cwd"]
        if obj.get("gitBranch"):
            self.branch = obj["gitBranch"]

        if etype == "assistant":
            self._ingest_assistant(obj, ts)
        elif etype == "user":
            self._ingest_user(obj, ts)
        elif etype == "ai-title":
            self.title = obj.get("aiTitle") or self.title
        elif etype == "custom-title":
            self.title = obj.get("customTitle") or self.title
        elif etype == "summary" and not self.title:
            self.title = obj.get("summary") or ""
        elif etype == "system" and obj.get("subtype") == "turn_duration" and not self.pending:
            self.phase = "waiting" if self.kind == "main" else "done"

    def _ingest_assistant(self, obj: dict[str, Any], ts: float) -> None:
        msg = obj.get("message") or {}
        if msg.get("model") and not str(msg["model"]).startswith("<"):
            self.model = msg["model"]
        usage = msg.get("usage")
        if isinstance(usage, dict) and msg.get("id"):
            self.usage[msg["id"]] = {
                "in": int(usage.get("input_tokens") or 0),
                "out": int(usage.get("output_tokens") or 0),
                "cache": int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0),
            }
        if obj.get("isApiErrorMessage"):
            self._add(Activity("error", ts, text=_text_of(msg.get("content"))))
            self.phase = "error"
            return

        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("thinking", "redacted_thinking"):
                act = Activity("thinking", ts, text=block.get("thinking") or "")
                self._add(act)
                if act.text.strip():
                    self.last_thought = act
                self.phase = "thinking"
            elif btype == "text":
                text = block.get("text") or ""
                if not text.strip():
                    continue
                act = Activity("text", ts, text=text)
                self._add(act)
                self.last_thought = act
                self.phase = "writing"
            elif btype == "tool_use":
                self._start_tool(block, ts)

        stop = msg.get("stop_reason")
        if stop in ("end_turn", "stop_sequence", "max_tokens", "refusal") and not self.pending:
            self.phase = "waiting" if self.kind == "main" else "done"

    def _start_tool(self, block: dict[str, Any], ts: float) -> None:
        name = block.get("name") or "?"
        args = block.get("input") or {}
        s = tools.summarize(name, args, self.cwd)
        act = Activity(
            "tool",
            ts,
            tool=name,
            verb=s.verb,
            target=s.target,
            detail=s.detail,
            category=s.category,
            status="running",
            tool_id=block.get("id") or "",
        )
        self._add(act)
        self.tool_count += 1
        if act.tool_id:
            self.pending[act.tool_id] = act
        self.phase = "tool"

        path = tools.file_path_of(name, args if isinstance(args, dict) else {})
        if path:
            touch = self.files.setdefault(path, FileTouch(path))
            if name in tools.FILE_TOOLS_WRITE:
                touch.writes += 1
            else:
                touch.reads += 1
            touch.ts = ts
        if name == "TodoWrite" and isinstance(args, dict):
            self.todos = [
                {
                    "content": str(t.get("content", "")),
                    "status": str(t.get("status", "pending")),
                    "active": str(t.get("activeForm", "")),
                }
                for t in args.get("todos") or []
                if isinstance(t, dict)
            ]

    def _ingest_user(self, obj: dict[str, Any], ts: float) -> None:
        msg = obj.get("message") or {}
        content = msg.get("content")
        if content is None:
            content = obj.get("content")
        is_meta = bool(obj.get("isMeta"))

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._finish_tool(block, obj, ts)
        if is_meta:
            return

        text = (
            _text_of(content)
            if not isinstance(content, list)
            else _text_of(
                [b for b in content if isinstance(b, dict) and b.get("type") in ("text", "image")]
            )
        )
        if not text.strip():
            return
        if text.startswith(INTERRUPT_MARKERS):
            self._add(Activity("interrupt", ts, text="Interrupted by user"))
            for act in self.pending.values():
                act.status, act.ended = "error", ts
            self.pending.clear()
            self.phase = "interrupted"
            return
        if "<command-name>" in text:
            name = text.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
            self._add(Activity("command", ts, text=name.strip()))
            return
        if _is_noise(text):
            return
        if self.kind == "sub" and not self.task:
            self.task = text
        self.last_prompt = text
        self._add(Activity("prompt", ts, text=text))
        self.phase = "thinking"

    def _finish_tool(self, block: dict[str, Any], obj: dict[str, Any], ts: float) -> None:
        tool_id = block.get("tool_use_id") or ""
        act = self.pending.pop(tool_id, None)
        if act is None:
            return
        act.status = "error" if block.get("is_error") else "ok"
        act.ended = ts
        act.result = _text_of(block.get("content")).strip()
        result = obj.get("toolUseResult")
        if isinstance(result, dict) and result.get("agentId"):
            act.agent_ref = str(result["agentId"])
        if not self.pending and self.phase == "tool":
            self.phase = "thinking"

    def _add(self, act: Activity) -> None:
        self.activities.append(act)

    # ----------------------------------------------------------------- derived
    def status(self, now: float) -> str:
        age = now - (self.last_ts or 0)
        if self.phase == "tool" and age > IDLE_AFTER_TOOL:
            return "idle"
        if self.phase in ("thinking", "writing") and age > IDLE_AFTER:
            return "idle"
        if self.phase == "waiting" and age > 6 * 3600:
            return "idle"
        return self.phase

    def current(self) -> Activity | None:
        if self.pending:
            return max(self.pending.values(), key=lambda a: a.ts)
        return self.activities[-1] if self.activities else None

    def tokens(self) -> dict[str, int]:
        total = {"in": 0, "out": 0, "cache": 0}
        for u in self.usage.values():
            for k in total:
                total[k] += u.get(k, 0)
        return total

    def name(self) -> str:
        if self.kind == "main":
            return "Claude"
        return self.agent_type or "subagent"

    def brief(self, now: float) -> dict[str, Any]:
        """Just enough to draw a session in a list."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name(),
            "description": self.description,
            "status": self.status(now),
            "last_ts": self.last_ts,
        }

    def to_dict(self, now: float, detail: bool = True) -> dict[str, Any]:
        cur = self.current()
        acts = list(self.activities)[-150:] if detail else []
        files = sorted(self.files.values(), key=lambda f: f.ts, reverse=True)
        file_count = len(files)
        if not detail:
            files = files[:6]
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "name": self.name(),
            "type": self.agent_type,
            "title": self.title,
            "description": self.description,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "task": self.task[:4000] if detail else self.task[:300],
            "cwd": self.cwd,
            "branch": self.branch,
            "model": self.model,
            "status": self.status(now),
            "started": self.started,
            "last_ts": self.last_ts,
            "current": cur.to_dict(compact=not detail) if cur else None,
            "thought": self.last_thought.to_dict(compact=not detail) if self.last_thought else None,
            "activities": [a.to_dict() for a in acts],
            "files": [
                {
                    "path": f.path,
                    "rel": tools.relpath(f.path, self.cwd),
                    "reads": f.reads,
                    "writes": f.writes,
                    "ts": f.ts,
                }
                for f in files
            ],
            "file_count": file_count,
            "todos": self.todos,
            "tokens": self.tokens(),
            "tool_count": self.tool_count,
        }


@dataclass
class Session:
    id: str
    project_dir: str
    main: Agent
    subagents: dict[str, Agent] = field(default_factory=dict)

    @property
    def agents(self) -> list[Agent]:
        return [self.main, *self.subagents.values()]

    @property
    def last_ts(self) -> float:
        return max((a.last_ts or 0) for a in self.agents)

    def link_subagents(self) -> None:
        """Resolve each subagent's parent (main agent or another subagent) via tool ids."""
        owners: dict[str, Agent] = {}
        for agent in self.agents:
            for act in agent.activities:
                if act.tool_id:
                    owners[act.tool_id] = agent
        for sub in self.subagents.values():
            parent = owners.get(sub.parent_tool_id, self.main)
            if parent is sub:
                parent = self.main
            sub.parent_id = parent.id
            if sub.parent_tool_id in owners:
                for act in parent.activities:
                    if act.tool_id == sub.parent_tool_id:
                        act.agent_ref = sub.id

    def project_name(self) -> str:
        cwd = self.main.cwd
        if cwd:
            return os.path.basename(cwd.rstrip("/\\")) or cwd
        # Fallback: the directory name is the cwd with separators replaced by "-".
        return self.project_dir.rsplit("-", 1)[-1] or self.project_dir

    def status(self, now: float) -> str:
        statuses = [a.status(now) for a in self.agents]
        for s in ("tool", "thinking", "writing"):
            if s in statuses:
                return "working"
        main = self.main.status(now)
        return main if main not in ("done",) else "waiting"

    def title(self) -> str:
        if self.main.title:
            return self.main.title
        return tools.shorten(self.main.last_prompt or "(no prompt yet)", 70)

    def to_dict(self, now: float, detail: bool = False) -> dict[str, Any]:
        agents = sorted(self.subagents.values(), key=lambda a: a.started or 0)
        return {
            "id": self.id,
            "project": self.project_name(),
            "project_dir": self.project_dir,
            "cwd": self.main.cwd,
            "branch": self.main.branch,
            "title": self.title(),
            "status": self.status(now),
            "last_ts": self.last_ts,
            "started": self.main.started,
            "working": sum(1 for a in self.agents if a.status(now) in WORKING),
            "detail": detail,
            "agent_count": len(agents) + 1,
            "agents": [a.to_dict(now, detail) for a in [self.main, *agents]]
            if detail
            else [a.brief(now) for a in [self.main, *agents]],
        }
