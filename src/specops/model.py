"""In-memory model of Claude Code sessions and agents, built from transcript events.

Claude Code writes one JSON object per line to ``~/.claude/projects/<project>/<session>.jsonl``
and, for subagents, to ``<session>/subagents/agent-<id>.jsonl``. The format is internal and
undocumented, so every field access here is defensive: unknown events are ignored rather than
crashing the viewer.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import pricing, tools

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
    "source",
)

# Name shown for the main agent of a session, by where the session comes from.
MAIN_NAMES = {"claude": "Claude", "antigravity": "Antigravity", "antigravity-cli": "Antigravity"}

# Phases an agent can be in. "Working" phases are shown as live.
WORKING = {"thinking", "writing", "tool"}

# After this many seconds without new events a "working" agent is presumed gone
# (process killed, laptop suspended, ...). Tools get longer because builds/tests can be slow.
IDLE_AFTER = 180.0
IDLE_AFTER_TOOL = 1800.0

INTERRUPT_MARKERS = ("[Request interrupted by user", "[Request cancelled by user")

# Signs that a working agent is stuck. Each looks at its most recent tool calls.
LOOP_WINDOW = 10  # the same call this many times with no edit in between...
LOOP_REPEATS = 3  # ...at least this often
FAIL_STREAK = 3  # this many failed calls in a row
SAME_FAIL_WINDOW = 15  # the same call failing...
SAME_FAIL_REPEATS = 3  # ...this often, even with edits in between
CHURN_WINDOW = 12  # one file edited...
CHURN_EDITS = 6  # ...this often
SLOW_TOOL = 600.0  # a call with no result after this many seconds
# Tools that are meant to be called over and over (polling) or to take long (waiting on
# a subagent or on you), so repeating them or waiting on them says nothing.
POLLING_TOOLS = {"BashOutput", "Monitor", "TaskOutput", "TodoWrite", "ScheduleWakeup"}
PATIENT_CATEGORIES = {"agent", "ask"}

# Agents talking to each other: a tool call on the sender's side, and on the receiver's side a
# message Claude Code wraps in <agent-message> (Antigravity's receiving side isn't logged).
MESSAGE_TOOLS = {"SendMessage", "send_message"}
AGENT_MESSAGE = re.compile(r'<agent-message from="([^"]*)">\n?(.*?)\n?</agent-message>', re.S)
MAX_MESSAGES = 300
# What the Agent tool returns at once for a subagent run in the background.
ASYNC_LAUNCH = "Async agent launched"


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


def message_of(name: str, args: Any) -> tuple[str, str] | None:
    """``(recipient, text)`` when a tool call sends a message to another agent."""
    if name not in MESSAGE_TOOLS or not isinstance(args, dict):
        return None
    to = next((args[k] for k in ("to", "recipient", "Recipient") if args.get(k)), "")
    text = next((args[k] for k in ("message", "content", "Message") if args.get(k)), "")
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return str(to), text


def call_sig(name: str, args: Any) -> str:
    """Identifies a tool call by name and input, so identical calls can be spotted."""
    return f"{name}:{hash(json.dumps(args, sort_keys=True, default=str))}"


def _alert(kind: str, text: str, act: Activity) -> dict[str, Any]:
    return {"kind": kind, "text": text, "verb": act.verb, "target": act.target}


@dataclass
class Activity:
    kind: str  # prompt | thinking | text | tool | error | command | interrupt | message
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
    peer: str = ""  # message: the agent it was received from; tool: the agent it was sent to
    sig: str = ""  # tool name + input, to spot identical calls

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
        if self.peer:
            d["peer"] = self.peer
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
    source: str = "claude"
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
    usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    context: int = 0  # tokens in the prompt of the latest request
    partial: bool = False  # only the end of the transcript was read, so totals fall short
    tool_count: int = 0
    last_thought: Activity | None = None
    last_prompt: str = ""
    _usage_version: int = field(default=0, repr=False)
    _totals: tuple[int, dict[str, Any]] | None = field(default=None, repr=False)

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
        elif etype == "attachment":
            att = obj.get("attachment") or {}
            if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
                self._receive(att["prompt"], ts)
        elif etype == "system" and obj.get("subtype") == "turn_duration" and not self.pending:
            self.phase = "waiting" if self.kind == "main" else "done"

    def _ingest_assistant(self, obj: dict[str, Any], ts: float) -> None:
        msg = obj.get("message") or {}
        if msg.get("model") and not str(msg["model"]).startswith("<"):
            self.model = msg["model"]
        usage = msg.get("usage")
        if isinstance(usage, dict) and msg.get("id"):
            self._ingest_usage(msg["id"], usage)
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

    def _ingest_usage(self, msg_id: str, usage: dict[str, Any]) -> None:
        creation = usage.get("cache_creation")
        write = int(usage.get("cache_creation_input_tokens") or 0)
        write_1h = 0
        if isinstance(creation, dict):
            write_1h = min(write, int(creation.get("ephemeral_1h_input_tokens") or 0))
        entry = {
            "model": self.model,
            "in": int(usage.get("input_tokens") or 0),
            "out": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_5m": write - write_1h,
            "cache_write_1h": write_1h,
            "fast": usage.get("speed") == "fast",
        }
        self.usage[msg_id] = entry
        self.context = entry["in"] + entry["cache_read"] + write
        self._usage_version += 1

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
            sig=call_sig(name, args),
        )
        message = message_of(name, args)
        if message:
            act.peer, act.text = message
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
            if isinstance(content, str):
                self._receive(content, ts)
            return

        text = (
            _text_of(content)
            if not isinstance(content, list)
            else _text_of(
                [b for b in content if isinstance(b, dict) and b.get("type") in ("text", "image")]
            )
        )
        if not text.strip() or self._receive(text, ts):
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

    def _receive(self, text: str, ts: float) -> bool:
        """Record the messages other agents sent this one, if ``text`` carries any."""
        found = AGENT_MESSAGE.findall(text) if "<agent-message" in text else []
        for sender, body in found:
            self._add(Activity("message", ts, text=body.strip(), peer=sender))
        return bool(found)

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

    def final_text(self) -> Activity | None:
        """The last thing a finished agent said: its answer."""
        if self.phase not in ("done", "waiting"):
            return None
        return next((a for a in reversed(self.activities) if a.kind == "text"), None)

    def current(self) -> Activity | None:
        if self.pending:
            return max(self.pending.values(), key=lambda a: a.ts)
        return self.activities[-1] if self.activities else None

    def tokens(self) -> dict[str, int]:
        return self._usage_totals()["tokens"]

    def cost(self) -> float | None:
        """API-equivalent USD for this agent, or None if no request had a known price."""
        return self._usage_totals()["cost"]

    def _usage_totals(self) -> dict[str, Any]:
        if self._totals and self._totals[0] == self._usage_version:
            return self._totals[1]
        tokens = {"in": 0, "out": 0, "cache": 0, "cache_read": 0, "cache_write": 0}
        usd: float | None = None
        for u in self.usage.values():
            write = u["cache_write_5m"] + u["cache_write_1h"]
            tokens["in"] += u["in"]
            tokens["out"] += u["out"]
            tokens["cache_read"] += u["cache_read"]
            tokens["cache_write"] += write
            tokens["cache"] += u["cache_read"] + write
            spent = pricing.cost(
                u["model"],
                input=u["in"],
                output=u["out"],
                cache_read=u["cache_read"],
                cache_write_5m=u["cache_write_5m"],
                cache_write_1h=u["cache_write_1h"],
                fast=u["fast"],
            )
            if spent is not None:
                usd = (usd or 0.0) + spent
        totals = {"tokens": tokens, "cost": usd}
        self._totals = (self._usage_version, totals)
        return totals

    def cache_hit(self) -> float | None:
        """Share of prompt tokens served from the cache."""
        t = self.tokens()
        prompt = t["in"] + t["cache"]
        return t["cache_read"] / prompt if prompt else None

    def alerts(self, now: float) -> list[dict[str, Any]]:
        """Signs that a working agent is going in circles or hanging, most telling first."""
        if self.status(now) not in WORKING:
            return []
        calls = [a for a in self.activities if a.kind == "tool"][-max(SAME_FAIL_WINDOW, 20) :]
        found: list[dict[str, Any]] = []

        # The same call again and again, with nothing edited in between.
        unchanged: list[Activity] = []
        for act in reversed(calls):
            if act.category == "edit" or len(unchanged) == LOOP_WINDOW:
                break
            unchanged.append(act)
        repeats = Counter(a.sig for a in unchanged if a.sig and a.tool not in POLLING_TOOLS)
        loop_sig, n = repeats.most_common(1)[0] if repeats else ("", 0)
        if n >= LOOP_REPEATS:
            act = next(a for a in unchanged if a.sig == loop_sig)
            found.append(_alert("loop", f"Same call {n}× with no edits in between", act))
        else:
            loop_sig = ""

        # The same call failing over and over, even if the agent edits between tries.
        recent = calls[-SAME_FAIL_WINDOW:]
        failures = Counter(a.sig for a in recent if a.status == "error" and a.sig != loop_sig)
        fail_sig, n = failures.most_common(1)[0] if failures else ("", 0)
        if n >= SAME_FAIL_REPEATS:
            act = next(a for a in reversed(recent) if a.sig == fail_sig)
            found.append(_alert("failing", f"Failed {n} times", act))

        # Everything failing lately, whatever it is.
        streak = 0
        for act in reversed(calls):
            if act.status == "running":
                continue
            if act.status != "error":
                break
            streak += 1
        if streak >= FAIL_STREAK and not found:
            found.append({"kind": "failing", "text": f"Last {streak} tool calls failed"})

        # One file rewritten again and again.
        edits = Counter(
            a.detail for a in calls[-CHURN_WINDOW:] if a.category == "edit" and a.detail
        )
        path, n = edits.most_common(1)[0] if edits else ("", 0)
        if n >= CHURN_EDITS:
            text = f"Edited {n} times in the last {min(len(calls), CHURN_WINDOW)} calls"
            found.append({"kind": "churn", "text": text, "target": tools.relpath(path, self.cwd)})

        # A call that never came back. Antigravity only logs a call once the next step starts,
        # so a call of its latest reply can look unanswered long after it finished.
        for act in self.pending.values() if self.source == "claude" else ():
            if act.category in PATIENT_CATEGORIES or act.tool in POLLING_TOOLS:
                continue
            if now - act.ts > SLOW_TOOL:
                found.append(_alert("slow", "No result for", act) | {"since": act.ts})
        return found

    def name(self) -> str:
        if self.kind == "main":
            return MAIN_NAMES.get(self.source, "Claude")
        return self.agent_type or "subagent"

    def brief(self, now: float) -> dict[str, Any]:
        """Just enough to draw a session in a list."""
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "name": self.name(),
            "description": self.description,
            "status": self.status(now),
            "last_ts": self.last_ts,
            "alerts": len(self.alerts(now)),
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
            "source": self.source,
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
            "context": self.context,
            "window": pricing.window_for(self.model, self.context),
            "cost": self.cost(),
            "partial": self.partial,
            "cache_hit": self.cache_hit(),
            "alerts": self.alerts(now),
            "tool_count": self.tool_count,
        }


@dataclass
class Session:
    id: str
    project_dir: str
    main: Agent
    subagents: dict[str, Agent] = field(default_factory=dict)
    source: str = "claude"

    @property
    def agents(self) -> list[Agent]:
        return [self.main, *self.subagents.values()]

    @property
    def last_ts(self) -> float:
        return max((a.last_ts or 0) for a in self.agents)

    def link_subagents(self) -> None:
        """Resolve each subagent's parent (main agent or another subagent) via tool ids.

        A subagent with no spawning tool id keeps the parent it was created with, if known.
        """
        owners: dict[str, Agent] = {}
        for agent in self.agents:
            for act in agent.activities:
                if act.tool_id:
                    owners[act.tool_id] = agent
        for sub in self.subagents.values():
            if not sub.parent_tool_id and sub.parent_id:
                if sub.parent_id != self.main.id and sub.parent_id not in self.subagents:
                    sub.parent_id = self.main.id
                continue
            parent = owners.get(sub.parent_tool_id, self.main)
            if parent is sub:
                parent = self.main
            sub.parent_id = parent.id
            if sub.parent_tool_id in owners:
                for act in parent.activities:
                    if act.tool_id == sub.parent_tool_id:
                        act.agent_ref = sub.id

    def conversation(self) -> list[dict[str, Any]]:
        """What the agents of this session said to each other, oldest first.

        ``task`` is the work an agent handed a subagent and ``result`` what came back; ``message``
        is a direct message. One logged on both sides (sent and received) is listed once.
        ``from``/``to`` are agent ids, or the raw name when the peer isn't in this session.
        """
        agents = {a.id: a for a in self.agents}
        out: list[dict[str, Any]] = []
        sent: Counter[tuple[str, str, str]] = Counter()
        received: list[tuple[Agent, Activity]] = []
        tasked: set[str] = set()

        def add(kind: str, ts: float | None, sender: str, to: str, text: str) -> None:
            out.append({"ts": ts or 0, "kind": kind, "from": sender, "to": to, "text": text[:4000]})

        for agent in agents.values():
            for act in agent.activities:
                if act.kind == "message":
                    received.append((agent, act))
                elif act.kind == "tool" and act.agent_ref in agents and act.agent_ref != agent.id:
                    child = agents[act.agent_ref]
                    tasked.add(child.id)
                    add("task", act.ts, agent.id, child.id, child.task or act.detail)
                    if act.result.startswith(ASYNC_LAUNCH):  # the answer comes later
                        final = child.final_text()
                        if final:
                            add("result", final.ts, child.id, agent.id, final.text)
                    elif act.status != "running" and act.result:
                        add("result", act.ended or act.ts, child.id, agent.id, act.result)
                elif act.kind == "tool" and act.peer:
                    add("message", act.ts, agent.id, act.peer, act.text)
                    sent[(agent.id, act.peer, act.text.strip())] += 1
        for agent, act in received:
            key = (act.peer, agent.id, act.text.strip())
            if sent[key]:
                sent[key] -= 1
                continue
            add("message", act.ts, act.peer, agent.id, act.text)
        # Subagents linked by parent only (Antigravity): their first prompt is the task.
        for sub in self.subagents.values():
            parent = sub.parent_id or self.main.id
            if sub.id not in tasked and sub.task and parent in agents:
                add("task", sub.started, parent, sub.id, sub.task)
        out.sort(key=lambda m: m["ts"])
        return out[-MAX_MESSAGES:]

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

    def cost(self) -> float | None:
        known = [c for c in (a.cost() for a in self.agents) if c is not None]
        return sum(known) if known else None

    def title(self) -> str:
        if self.main.title:
            return self.main.title
        return tools.shorten(self.main.last_prompt or "(no prompt yet)", 70)

    def to_dict(self, now: float, detail: bool = False) -> dict[str, Any]:
        agents = sorted(self.subagents.values(), key=lambda a: a.started or 0)
        return {
            "id": self.id,
            "source": self.source,
            "project": self.project_name(),
            "project_dir": self.project_dir,
            "cwd": self.main.cwd,
            "branch": self.main.branch,
            "title": self.title(),
            "status": self.status(now),
            "last_ts": self.last_ts,
            "started": self.main.started,
            "working": sum(1 for a in self.agents if a.status(now) in WORKING),
            "alerts": sum(len(a.alerts(now)) for a in self.agents),
            "cost": self.cost(),
            "partial": any(a.partial for a in self.agents),
            "detail": detail,
            "agent_count": len(agents) + 1,
            "conversation": self.conversation() if detail else [],
            "agents": [a.to_dict(now, detail) for a in [self.main, *agents]]
            if detail
            else [a.brief(now) for a in [self.main, *agents]],
        }
