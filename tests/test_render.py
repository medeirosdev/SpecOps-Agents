from __future__ import annotations

import os
import time

import pytest
from rich.console import Console
from rich.text import Text

from specops.hive import Hive
from specops.tui import render as r

from .conftest import SESSION_ID


@pytest.mark.parametrize(
    ("sec", "text"),
    [(-5, "0s"), (0, "0s"), (59, "59s"), (61, "1m 01s"), (3600, "1h 00m"), (3 * 86400, "3d")],
)
def test_dur(sec: float, text: str) -> None:
    assert r.dur(sec) == text


def test_ago() -> None:
    assert r.ago(None) == ""
    assert r.ago(time.time()) == "just now"
    assert r.ago(time.time() - 125) == "2m ago"


@pytest.mark.parametrize(
    ("n", "text"),
    [(None, "0"), (999, "999"), (1500, "1.5k"), (25_000, "25k"), (2_500_000, "2.5M")],
)
def test_tokens(n: int | None, text: str) -> None:
    assert r.tokens(n) == text


@pytest.mark.parametrize(
    ("m", "text"),
    [
        ("claude-opus-5-5", "opus 5.5"),
        ("claude-sonnet-5", "sonnet 5"),
        ("claude-haiku-4-5-20251001", "haiku 4.5"),
        ("claude-sonnet-4-20250514", "sonnet 4"),
        ("gpt-x", "gpt-x"),
        ("", ""),
    ],
)
def test_model(m: str, text: str) -> None:
    assert r.model(m) == text


def test_escape_and_one_line() -> None:
    assert r.escape("[b]x") == r"\[b]x"
    for text in ("[b]x", "a\\[b]c", "path\\", "x]y"):
        assert Text.from_markup(f"[red]{r.escape(text)}[/]").plain == text
    assert r.one_line("a\nb  c", 4) == "a b…"


def render_text(renderable, width: int = 100) -> str:
    console = Console(width=width, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def session(projects) -> dict:
    h = Hive(root=projects, since=0)
    h.poll()
    return h.snapshot(SESSION_ID)["sessions"][0]


STATUSES = ["thinking", "tool", "writing", "waiting", "done", "idle", "error", "interrupted"]


@pytest.mark.parametrize("status", STATUSES)
def test_card_renders_every_status(session: dict, status: str) -> None:
    agent = dict(session["agents"][0], status=status)
    text = render_text(r.card_body(agent, wide=True))
    assert "tools" in text
    assert r.STATUS_LABEL[status] in r.card_subtitle(agent)


def test_card_contents(session: dict) -> None:
    main = dict(session["agents"][0], status="waiting")  # the fixture is old enough to be idle
    text = render_text(r.card_body(main, wide=True))
    assert "Waiting for your next message" in text
    assert "Fixed: the payment mock" in text
    assert "Fixing the race" in text  # in-progress todo shows its active form
    assert "1/2" in text


def test_card_title_escapes_markup(session: dict) -> None:
    agent = dict(session["agents"][1], description="[red]not markup")
    assert r"\[red]" in r.card_title(agent)


@pytest.mark.parametrize("tab", ["timeline", "thoughts", "tools", "files"])
def test_timeline_tabs(session: dict, tab: str) -> None:
    main = session["agents"][0]
    text = render_text(r.timeline(main, tab), width=140)
    if tab == "files":
        assert os.path.join("tests", "test_checkout.py") in text and "W1" in text
    elif tab == "tools":
        assert "Spawning general-purpose" in text and "Said" not in text
    elif tab == "thoughts":
        assert "race on the payment mock" in text and "Reading" not in text
    else:
        assert "You" in text and "Command" in text and "subagent" in text


def test_subagent_timeline_shows_bash_and_failure(session: dict) -> None:
    sub = session["agents"][1]
    text = render_text(r.timeline(sub), width=140)
    assert "Task" in text and "$ for i in" in text and "failed" in text


def test_session_prompt_and_finished_row(session: dict) -> None:
    text = render_text(r.session_prompt(session, 40))
    assert "shop" in text and "Fix flaky" in text
    row = render_text(r.finished_prompt(session["agents"][1]), width=140)
    assert "general-purpose" in row and "1 tools" in row


@pytest.mark.parametrize(
    ("n", "text"), [(None, ""), (0.004, "<$0.01"), (1.234, "$1.23"), (1234.5, "$1,234")]
)
def test_usd(n: float | None, text: str) -> None:
    assert r.usd(n) == text


def test_context_text() -> None:
    assert r.context_text({"context": 0, "window": 200_000}) is None
    t = r.context_text({"context": 900_000, "window": 1_000_000, "cache_hit": 0.914}, width=10)
    assert t is not None
    assert t.plain == "ctx " + "━" * 10 + " 900k/1.0M · 91% cached"


def test_alerts_text() -> None:
    assert r.alerts_text({"alerts": []}) is None
    alerts = [
        {"kind": "loop", "text": "Same call 3×", "verb": "Running", "target": "npm test"},
        {"kind": "slow", "text": "No result for", "since": time.time() - 61, "target": "dev"},
    ]
    t = r.alerts_text({"alerts": alerts})
    assert t is not None
    assert t.plain.splitlines() == [
        "↻ Same call 3×  Running npm test",
        "◷ No result for 1m 01s  dev",
    ]


def test_cost_label() -> None:
    assert r.cost_label({"cost": None}) == ""
    assert r.cost_label({"cost": 1.5}) == "≈$1.50"
    assert r.cost_label({"cost": 1.5, "partial": True}) == "≥$1.50"
