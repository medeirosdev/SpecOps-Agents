"""Headless smoke tests for the Textual app, driven through ``App.run_test``."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from textual.widgets import Static

from specops.hive import Hive
from specops.tui.app import AgentScreen, SpecOpsApp

from .conftest import PROJECT, SESSION_ID


def run(coro) -> None:
    asyncio.run(coro)


def test_shows_session_and_opens_agent_timeline(projects: Path) -> None:
    hive = Hive(root=projects, since=0)

    async def scenario() -> None:
        app = SpecOpsApp(hive)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            assert app.selected == SESSION_ID

            await pilot.press("a")  # focus the main agent card
            await pilot.press("enter")  # used to crash: the screen painted before composing
            await pilot.pause(0.3)
            assert isinstance(app.screen, AgentScreen)
            head = app.screen.query_one("#agent-head", Static)
            assert "Claude" in str(head.render())

            for key in "2341":
                await pilot.press(key)
                await pilot.pause(0.05)
            assert app.screen.tab == "timeline"

            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, AgentScreen)

            await pilot.press("f")
            assert app.follow is False
        hive.stop()

    run(scenario())


def test_finished_subagent_opens_from_list(projects: Path) -> None:
    hive = Hive(root=projects, since=0)

    async def scenario() -> None:
        app = SpecOpsApp(hive)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#finished").focus()
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, AgentScreen)
            assert app.screen.agent_id == "a1b2c3"
        hive.stop()

    run(scenario())


def test_empty_hive(tmp_path: Path) -> None:
    hive = Hive(root=tmp_path / "missing", since=0)

    async def scenario() -> None:
        app = SpecOpsApp(hive)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.3)
            empty = app.query_one("#empty", Static)
            assert empty.display and "All quiet on the field" in str(empty.render())
        hive.stop()

    run(scenario())


def test_follow_waits_while_a_timeline_is_open(projects: Path) -> None:
    hive = Hive(root=projects, since=0)

    async def scenario() -> None:
        app = SpecOpsApp(hive)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("a", "enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, AgentScreen) and app.follow

            now = datetime.now(timezone.utc).isoformat()
            event = {"type": "user", "message": {"content": "go"}, "cwd": "/w", "timestamp": now}
            (projects / PROJECT / "busy.jsonl").write_text(json.dumps(event) + "\n")
            hive._last_scan = 0.0  # discover the new file now, not in 2s
            hive.poll()
            await pilot.pause(0.5)
            assert app.selected == SESSION_ID  # the open timeline keeps its session

            await pilot.press("escape")
            await pilot.pause(0.5)
            assert app.selected == "busy"  # and follow resumes once it is closed
        hive.stop()

    run(scenario())
