"""Discover Claude Code transcripts on disk and follow them as they grow."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import Agent, Session

# On first sight of a big transcript only the tail is parsed, so huge sessions load instantly.
INITIAL_TAIL_BYTES = 4 * 1024 * 1024


def default_root() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "projects"


@dataclass
class _Tail:
    path: Path
    agent: Agent
    session: Session
    offset: int = 0
    buffer: bytes = b""
    mtime: float = 0.0


class Hive:
    """Thread-safe registry of sessions, fed by polling transcript files.

    Call :meth:`poll` periodically (or :meth:`start` for a background thread) and read
    :meth:`snapshot` from any thread.
    """

    def __init__(
        self,
        root: Path | None = None,
        since: float = 6 * 3600,
        project_filter: str | None = None,
        max_sessions: int = 40,
    ) -> None:
        self.root = Path(root) if root else default_root()
        self.since = since
        self.project_filter = project_filter.lower() if project_filter else None
        self.max_sessions = max_sessions
        self.sessions: dict[str, Session] = {}
        self._tails: dict[Path, _Tail] = {}
        self._lock = threading.RLock()
        self._last_scan = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[], None]] = []
        self.version = 0

    # --------------------------------------------------------------- discovery
    def _candidates(self) -> list[tuple[Path, float]]:
        if not self.root.is_dir():
            return []
        cutoff = time.time() - self.since if self.since > 0 else 0
        found: list[tuple[Path, float]] = []
        for project in self.root.iterdir():
            if not project.is_dir():
                continue
            if self.project_filter and self.project_filter not in project.name.lower():
                continue
            for path in project.glob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    found.append((path, mtime))
        found.sort(key=lambda item: item[1], reverse=True)
        return found[: self.max_sessions]

    def _scan(self) -> bool:
        changed = False
        for path, _ in self._candidates():
            session_id = path.stem
            session = self.sessions.get(session_id)
            if session is None:
                main = Agent(id=session_id, session_id=session_id, kind="main", path=str(path))
                session = Session(id=session_id, project_dir=path.parent.name, main=main)
                self.sessions[session_id] = session
                self._tails[path] = _Tail(path, main, session)
                changed = True
            sub_dir = path.parent / session_id / "subagents"
            if sub_dir.is_dir():
                for sub_path in sub_dir.glob("agent-*.jsonl"):
                    if sub_path not in self._tails:
                        self._add_subagent(session, sub_path)
                        changed = True
        return changed

    def _add_subagent(self, session: Session, path: Path) -> None:
        agent_id = path.stem.removeprefix("agent-")
        meta: dict[str, Any] = {}
        meta_path = path.with_name(path.stem + ".meta.json")
        with contextlib.suppress(OSError, ValueError):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        agent = Agent(
            id=agent_id,
            session_id=session.id,
            kind="sub",
            path=str(path),
            parent_tool_id=str(meta.get("toolUseId") or ""),
            agent_type=str(meta.get("agentType") or ""),
            description=str(meta.get("description") or ""),
            depth=int(meta.get("spawnDepth") or 1),
        )
        session.subagents[agent_id] = agent
        self._tails[path] = _Tail(path, agent, session)

    # ----------------------------------------------------------------- tailing
    def _read(self, tail: _Tail) -> bool:
        try:
            st = tail.path.stat()
        except OSError:
            return False
        if st.st_size < tail.offset:  # truncated / rewritten
            tail.offset, tail.buffer = 0, b""
            tail.agent.reset()
        if st.st_size == tail.offset:
            return False
        skip_partial = False
        if tail.offset == 0 and st.st_size > INITIAL_TAIL_BYTES:
            tail.offset = st.st_size - INITIAL_TAIL_BYTES
            skip_partial = True
        try:
            with tail.path.open("rb") as fh:
                fh.seek(tail.offset)
                chunk = fh.read(st.st_size - tail.offset)
        except OSError:
            return False
        tail.offset += len(chunk)
        tail.mtime = st.st_mtime
        data = tail.buffer + chunk
        lines = data.split(b"\n")
        tail.buffer = lines.pop()  # incomplete last line (or b"")
        if skip_partial and lines:
            lines.pop(0)
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                try:
                    tail.agent.ingest(obj)
                except Exception:  # never let one odd event kill the viewer
                    continue
        return True

    def poll(self) -> bool:
        """Scan for new files (every ~2s) and read new lines. Returns True if anything changed."""
        with self._lock:
            changed = False
            now = time.monotonic()
            if now - self._last_scan > 2.0:
                self._last_scan = now
                changed |= self._scan()
            touched: set[str] = set()
            for tail in list(self._tails.values()):
                if self._read(tail):
                    touched.add(tail.session.id)
            for sid in touched:
                self.sessions[sid].link_subagents()
            if changed or touched:
                self.version += 1
                changed = True
        if changed:
            for listener in list(self._listeners):
                listener()
        return changed

    # --------------------------------------------------------------- lifecycle
    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def start(self, interval: float = 0.4) -> None:
        if self._thread:
            return

        def loop() -> None:
            while not self._stop.is_set():
                with contextlib.suppress(Exception):
                    self.poll()
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="specops-hive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, detail: str | None = None) -> dict[str, Any]:
        """Everything the UIs need. Only the ``detail`` session carries full timelines."""
        now = time.time()
        with self._lock:
            sessions = sorted(self.sessions.values(), key=lambda s: s.last_ts, reverse=True)
            return {
                "version": self.version,
                "now": now,
                "root": str(self.root),
                "home": str(Path.home()),
                "sessions": [s.to_dict(now, detail=s.id == detail) for s in sessions],
            }
