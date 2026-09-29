"""A fake colony: writes realistic Claude Code transcripts so SpecOps Agents can be tried without Claude.

The demo exercises the real pipeline end to end: it appends JSONL events to files in a temporary
``projects`` directory and the normal :class:`~specops.hive.Hive` follows them.
"""

from __future__ import annotations

import json
import random
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:24]


class Transcript:
    """Appends Claude Code style events to one JSONL file."""

    def __init__(self, path: Path, session_id: str, cwd: str, agent_id: str | None = None):
        self.path = path
        self.session_id = session_id
        self.cwd = cwd
        self.agent_id = agent_id
        self.model = "claude-opus-5-5" if agent_id is None else "claude-sonnet-5"
        self.clock: float | None = None  # fixed clock for back-dated history
        # Tokens in the prompt so far; it grows with every turn, like a real conversation.
        self.context = random.randint(60_000, 140_000) if agent_id is None else 12_000
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, obj: dict[str, Any]) -> None:
        base = {
            "timestamp": _iso(self.clock or time.time()),
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "gitBranch": "main",
            "uuid": str(uuid.uuid4()),
            "isSidechain": self.agent_id is not None,
        }
        if self.agent_id:
            base["agentId"] = self.agent_id
        base.update(obj)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")

    def _assistant(self, block: dict[str, Any], stop: str | None = None) -> None:
        self.emit(
            {
                "type": "assistant",
                "message": {
                    "id": _id("msg_"),
                    "role": "assistant",
                    "model": self.model,
                    "content": [block],
                    "stop_reason": stop,
                    "usage": self._usage(),
                },
            }
        )

    def _usage(self) -> dict[str, int]:
        if self.context > 700_000:  # as if the conversation had been compacted
            self.context = random.randint(30_000, 50_000)
        fresh, out = random.randint(300, 3000), random.randint(40, 900)
        usage = {
            "input_tokens": random.randint(2, 40),
            "output_tokens": out,
            "cache_read_input_tokens": self.context,
            "cache_creation_input_tokens": fresh,
        }
        self.context += fresh + out
        return usage

    def title(self, text: str) -> None:
        self.emit({"type": "ai-title", "aiTitle": text})

    def prompt(self, text: str) -> None:
        self.emit({"type": "user", "message": {"role": "user", "content": text}})

    def think(self, text: str) -> None:
        self._assistant({"type": "thinking", "thinking": text, "signature": "demo"})

    def say(self, text: str, final: bool = False) -> None:
        self._assistant({"type": "text", "text": text}, stop="end_turn" if final else None)

    def tool(self, name: str, **args: Any) -> str:
        tool_id = _id("toolu_")
        self._assistant(
            {"type": "tool_use", "id": tool_id, "name": name, "input": args}, stop="tool_use"
        )
        return tool_id

    def result(self, tool_id: str, text: str, error: bool = False, extra: Any = None) -> None:
        obj: dict[str, Any] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": text,
                        "is_error": error,
                    }
                ],
            },
        }
        if extra is not None:
            obj["toolUseResult"] = extra
        self.emit(obj)

    def quick(self, name: str, out: str = "ok", **args: Any) -> None:
        """A tool call that returns immediately."""
        self.result(self.tool(name, **args), out)

    def turn_end(self) -> None:
        self.emit({"type": "system", "subtype": "turn_duration", "durationMs": 1})


# --------------------------------------------------------------------------- scripts

Step = tuple  # (kind, *args)

EXPLORE_THEME: list[Step] = [
    ("think", "Theme tokens could live in CSS variables or a JS theme object. Grep for both."),
    (
        "tool",
        "Grep",
        {"pattern": "--color-|theme\\.", "path": "src"},
        "src/styles/tokens.css\nsrc/theme.ts\nsrc/components/Chart.tsx",
    ),
    (
        "tool",
        "Read",
        {"file_path": "{cwd}/src/styles/tokens.css"},
        "  1\t:root {\n  2\t  --color-bg: #fff;\n  3\t  --color-fg: #111;",
    ),
    ("tool", "Read", {"file_path": "{cwd}/src/theme.ts"}, "export const theme = { ... }"),
    (
        "think",
        "Tokens are plain CSS custom properties on :root. A [data-theme=dark] override block would cover most of the UI.",
    ),
    ("tool", "Glob", {"pattern": "src/**/*.module.css"}, "12 files"),
    (
        "say",
        "Theme lives in `src/styles/tokens.css` (18 CSS variables on `:root`). 3 components hard-code hex colours: Chart, Badge, Sidebar.",
    ),
]

AUDIT_CHARTS: list[Step] = [
    (
        "think",
        "Charts are probably drawn with a canvas lib that won't read CSS variables automatically.",
    ),
    (
        "tool",
        "Read",
        {"file_path": "{cwd}/src/components/Chart.tsx"},
        "import { Chart } from 'chart.js'...",
    ),
    (
        "tool",
        "Grep",
        {"pattern": "#[0-9a-fA-F]{6}", "path": "src/components"},
        "Chart.tsx:14: '#3b82f6'\nChart.tsx:15: '#10b981'",
    ),
    (
        "think",
        "Chart.tsx hard-codes series colours. Reading them from getComputedStyle at render time and re-rendering on theme change fixes it.",
    ),
    (
        "tool",
        "Bash",
        {"command": "npm ls chart.js", "description": "Check chart.js version"},
        "chart.js@4.4.1",
    ),
    (
        "say",
        "Chart.tsx hard-codes 4 series colours; chart.js 4 needs a re-render on theme change. Suggest a `useThemeColors()` hook.",
    ),
]

FIND_SETTINGS: list[Step] = [
    ("tool", "Grep", {"pattern": "localStorage", "path": "src"}, "src/lib/settings.ts:8"),
    (
        "tool",
        "Read",
        {"file_path": "{cwd}/src/lib/settings.ts"},
        "export function loadSettings() { ... }",
    ),
    (
        "think",
        "Settings are persisted in localStorage under 'dash.settings'. Add a `theme` key with 'system' as default.",
    ),
    (
        "say",
        "Persist the choice in `src/lib/settings.ts` (key `dash.settings`); default to `prefers-color-scheme`.",
    ),
]

REGRESSION_TEST: list[Step] = [
    (
        "think",
        "Reproduce the pagination bug: page 2 repeats the last row of page 1 (off-by-one on offset).",
    ),
    (
        "tool",
        "Read",
        {"file_path": "{cwd}/tests/test_users.py"},
        "def test_list_users(client): ...",
    ),
    (
        "tool",
        "Edit",
        {"file_path": "{cwd}/tests/test_users.py", "old_string": "...", "new_string": "..."},
        "The file has been updated.",
    ),
    (
        "tool",
        "Bash",
        {"command": "pytest tests/test_users.py -q", "description": "Run the new regression test"},
        "F.\n1 failed, 1 passed",
        True,
    ),
    ("think", "Good, the test fails for the right reason. That's the regression captured."),
    ("say", "Added `test_pagination_has_no_overlap`; it fails on main as expected."),
]


class Demo:
    def __init__(self, speed: float = 1.0):
        self.speed = speed
        self.dir = Path(tempfile.mkdtemp(prefix="specops-demo-"))
        self.root = self.dir / "projects"
        self._stop = threading.Event()

    # -- helpers -------------------------------------------------------------
    def nap(self, lo: float, hi: float) -> None:
        self._stop.wait(random.uniform(lo, hi) / self.speed)

    def session(self, cwd: str) -> Transcript:
        sid = str(uuid.uuid4())
        project = self.root / cwd.replace("/", "-")
        return Transcript(project / f"{sid}.jsonl", sid, cwd)

    def sub(
        self, parent: Transcript, agent_type: str, description: str, tool_id: str
    ) -> Transcript:
        agent_id = "a" + uuid.uuid4().hex[:16]
        folder = parent.path.parent / parent.session_id / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        meta = {
            "agentType": agent_type,
            "description": description,
            "toolUseId": tool_id,
            "spawnDepth": 1,
        }
        (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        return Transcript(
            folder / f"agent-{agent_id}.jsonl", parent.session_id, parent.cwd, agent_id
        )

    def run_steps(self, t: Transcript, steps: list[Step]) -> str:
        last = ""
        for step in steps:
            if self._stop.is_set():
                return last
            kind = step[0]
            if kind == "think":
                t.think(step[1])
                self.nap(1.5, 3.5)
            elif kind == "say":
                last = step[1]
                t.say(step[1], final=True)
            elif kind == "tool":
                name, args, out = step[1], dict(step[2]), step[3]
                error = len(step) > 4 and step[4]
                for k, v in args.items():
                    if isinstance(v, str):
                        args[k] = v.replace("{cwd}", t.cwd)
                tid = t.tool(name, **args)
                self.nap(1.2, 4.0 if name == "Bash" else 2.2)
                t.result(tid, out, error=error)
                self.nap(0.4, 1.2)
        return last

    def spawn(
        self, parent: Transcript, agent_type: str, desc: str, steps: list[Step]
    ) -> threading.Thread:
        tid = parent.tool(
            "Agent",
            description=desc,
            subagent_type=agent_type,
            prompt=f"{desc}. Report findings concisely.",
        )
        child = self.sub(parent, agent_type, desc, tid)

        def work() -> None:
            self.nap(0.3, 0.8)
            child.prompt(f"{desc}. Report findings concisely.")
            self.nap(0.5, 1.5)
            report = self.run_steps(child, steps)
            parent.result(
                tid, report or "done", extra={"agentId": child.agent_id, "status": "completed"}
            )

        th = threading.Thread(target=work, daemon=True)
        th.start()
        self.nap(0.4, 1.0)
        return th

    # -- scenarios -------------------------------------------------------------
    def dashboard(self) -> None:
        t = self.session("/home/you/code/dashboard")
        while not self._stop.is_set():
            t.prompt("Add a dark mode toggle to the dashboard, and make sure the charts respect it")
            t.title("Dark mode for the dashboard")
            self.nap(0.8, 1.5)
            t.think(
                "They want a dark mode toggle. Before touching anything I need to know three things: "
                "where the theme tokens live, how the charts pick colours, and where user settings are "
                "persisted. Those are independent, so I'll fan out to three agents in parallel."
            )
            self.nap(2, 3)
            todo = [
                {
                    "content": "Map theme tokens",
                    "status": "in_progress",
                    "activeForm": "Mapping theme tokens",
                },
                {
                    "content": "Add dark palette",
                    "status": "pending",
                    "activeForm": "Adding dark palette",
                },
                {
                    "content": "Make charts theme-aware",
                    "status": "pending",
                    "activeForm": "Fixing charts",
                },
                {"content": "Persist the choice", "status": "pending", "activeForm": "Persisting"},
                {
                    "content": "Run the test suite",
                    "status": "pending",
                    "activeForm": "Running tests",
                },
            ]
            t.quick("TodoWrite", todos=todo)
            t.say("I'll map the codebase first, splitting the exploration across three agents.")
            self.nap(0.6, 1.2)
            threads = [
                self.spawn(t, "Explore", "Map theme & CSS tokens", EXPLORE_THEME),
                self.spawn(t, "general-purpose", "Audit chart colours", AUDIT_CHARTS),
                self.spawn(t, "Explore", "Find settings persistence", FIND_SETTINGS),
            ]
            for th in threads:
                while th.is_alive() and not self._stop.is_set():
                    th.join(0.2)
            if self._stop.is_set():
                return
            t.think(
                "All three reports are in. Plan: add a [data-theme=dark] block to tokens.css, a "
                "useThemeColors() hook for the charts, and a `theme` key in settings."
            )
            self.nap(2, 3)
            for i, (name, path) in enumerate(
                [
                    ("Edit", "src/styles/tokens.css"),
                    ("Write", "src/hooks/useThemeColors.ts"),
                    ("Edit", "src/components/Chart.tsx"),
                    ("Edit", "src/lib/settings.ts"),
                    ("Edit", "src/components/Header.tsx"),
                ]
            ):
                for item in todo[: min(i + 2, 4)]:
                    item["status"] = "completed"
                todo[min(i + 1, 3)]["status"] = "in_progress"
                tid = t.tool(
                    name, file_path=f"{t.cwd}/{path}", old_string="…", new_string="…", content="…"
                )
                self.nap(1.2, 2.4)
                t.result(tid, "The file has been updated successfully.")
                if i == 2:
                    t.quick("TodoWrite", todos=todo)
            todo[3]["status"], todo[4]["status"] = "completed", "in_progress"
            tid = t.tool("Bash", command="npm test -- --run", description="Run the test suite")
            self.nap(4, 6)
            t.result(tid, "Test Files  14 passed (14)\n     Tests  96 passed (96)")
            todo[4]["status"] = "completed"
            t.quick("TodoWrite", todos=todo)
            self.nap(0.5, 1)
            t.say(
                "Dark mode is in: a toggle in the header (defaults to your OS setting), a dark "
                "palette in `tokens.css`, and charts now re-render with theme colours. 96 tests pass.",
                final=True,
            )
            t.turn_end()
            self.nap(14, 20)

    def api(self) -> None:
        t = self.session("/home/you/code/api-server")
        self.nap(3, 5)
        while not self._stop.is_set():
            t.prompt("GET /users?page=2 returns the last user of page 1 again. Can you fix it?")
            t.title("Fix duplicated row in pagination")
            self.nap(1, 2)
            t.think("Classic off-by-one. Let me look at how offset is computed before guessing.")
            self.nap(1.5, 2.5)
            tid = t.tool("Grep", pattern="offset", path="app/")
            self.nap(1, 1.5)
            t.result(tid, "app/routes/users.py:31: offset = (page * size) - 1")
            tid = t.tool("Read", file_path=f"{t.cwd}/app/routes/users.py")
            self.nap(1, 2)
            t.result(tid, "31\t    offset = (page * size) - 1")
            t.think(
                "Found it: offset should be (page - 1) * size. I'll get a regression test written first."
            )
            self.nap(1.5, 2)
            th = self.spawn(t, "general-purpose", "Write regression test", REGRESSION_TEST)
            while th.is_alive() and not self._stop.is_set():
                th.join(0.2)
            tid = t.tool(
                "Edit",
                file_path=f"{t.cwd}/app/routes/users.py",
                old_string="(page * size) - 1",
                new_string="(page - 1) * size",
            )
            self.nap(1, 2)
            t.result(tid, "The file has been updated successfully.")
            tid = t.tool("Bash", command="pytest -q", description="Run all tests")
            self.nap(3, 5)
            t.result(tid, "42 passed in 3.1s")
            t.say(
                "Fixed: offset was `(page * size) - 1`, now `(page - 1) * size`. Added a regression test; 42 tests pass.",
                final=True,
            )
            t.turn_end()
            self.nap(18, 26)

    def migrate(self) -> None:
        """An agent retrying the same failing command: shows the loop warning."""
        t = self.session("/home/you/code/billing")
        self.nap(6, 9)
        refused = (
            "sqlalchemy.exc.OperationalError: connection to server at "
            '"localhost" (127.0.0.1), port 5432 failed: Connection refused'
        )
        while not self._stop.is_set():
            t.prompt("Run the new invoices migration against the local database")
            t.title("Apply the invoices migration")
            self.nap(1, 2)
            for attempt in range(3):
                if self._stop.is_set():
                    return
                t.think(
                    "Apply it with alembic."
                    if attempt == 0
                    else "Connection refused. Probably a blip, trying again."
                )
                self.nap(1.5, 2.5)
                tid = t.tool(
                    "Bash", command="alembic upgrade head", description="Apply the migration"
                )
                self.nap(1.5, 2.5)
                t.result(tid, refused, error=True)
            t.think(
                "Third identical failure. Retrying won't help: nothing is listening on 5432, "
                "so Postgres isn't running. I should stop and ask rather than keep trying."
            )
            self.nap(8, 12)
            t.say(
                "Postgres isn't running on `localhost:5432`, so the migration can't connect. "
                "Start it with `docker compose up -d db` and I'll apply the migration.",
                final=True,
            )
            t.turn_end()
            self.nap(25, 35)

    def history(self) -> None:
        """A finished session from earlier today, for a less empty sidebar."""
        t = self.session("/home/you/code/blog")
        t.clock = time.time() - 3 * 3600
        t.title("Write release notes for v2.3")
        t.prompt("Write release notes for v2.3 from the git log")
        tid = t.tool(
            "Bash", command="git log v2.2..HEAD --oneline", description="List commits since v2.2"
        )
        t.result(tid, "a1b2c3 feat: rss feed\n...")
        tid = t.tool("Write", file_path=f"{t.cwd}/CHANGELOG.md", content="…")
        t.result(tid, "File created successfully.")
        t.say("Release notes written to CHANGELOG.md.", final=True)
        t.turn_end()

    def start(self) -> Path:
        self.history()
        for target in (self.dashboard, self.api, self.migrate):
            threading.Thread(target=target, daemon=True).start()
        return self.root

    def stop(self) -> None:
        self._stop.set()
