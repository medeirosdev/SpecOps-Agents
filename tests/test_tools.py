from __future__ import annotations

import os

import pytest

from specops import tools


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Read", "read"),
        ("Edit", "edit"),
        ("Bash", "shell"),
        ("Grep", "search"),
        ("Agent", "agent"),
        ("Task", "agent"),
        ("WebFetch", "web"),
        ("TodoWrite", "plan"),
        ("AskUserQuestion", "ask"),
        ("mcp__github__create_issue", "mcp"),
        ("SomethingNew", "other"),
    ],
)
def test_category_for(name: str, category: str) -> None:
    assert tools.category_for(name) == category


def test_shorten_collapses_whitespace_and_truncates() -> None:
    assert tools.shorten("a\n  b\tc") == "a b c"
    assert tools.shorten("x" * 10, 5) == "xxxx…"


def test_relpath_prefers_cwd_then_home() -> None:
    assert tools.relpath("/work/app/src/a.py", "/work/app") == os.path.join("src", "a.py")
    home = os.path.expanduser("~")
    assert tools.relpath(os.path.join(home, "x.txt"), "/elsewhere") == "~" + os.sep + "x.txt"
    assert tools.relpath("/etc/hosts", "/work/app") == "/etc/hosts"
    assert tools.relpath("", "/work") == ""


def test_summarize_file_tools_use_relative_paths() -> None:
    s = tools.summarize("Read", {"file_path": "/work/app/src/a.py"}, "/work/app")
    assert (s.verb, s.target, s.detail, s.category) == (
        "Reading",
        os.path.join("src", "a.py"),
        "/work/app/src/a.py",
        "read",
    )
    assert tools.summarize("Write", {"file_path": "/work/app/b.py"}, "/work/app").verb == "Writing"


def test_summarize_bash_prefers_description() -> None:
    s = tools.summarize("Bash", {"command": "pytest -q\nexit", "description": "Run tests"})
    assert (s.verb, s.target, s.detail) == ("Running", "Run tests", "pytest -q\nexit")
    assert tools.summarize("Bash", {"command": "ls -la\npwd"}).target == "ls -la"


def test_summarize_agent_spawn() -> None:
    s = tools.summarize(
        "Agent", {"subagent_type": "Explore", "description": "Map the API", "prompt": "Go"}
    )
    assert (s.verb, s.target, s.detail) == ("Spawning Explore", "Map the API", "Go")
    assert s.category == "agent"


def test_summarize_mcp_and_unknown_tools() -> None:
    s = tools.summarize("mcp__github__create_issue", {"title": "Bug", "n": 3})
    assert (s.verb, s.target, s.category) == ("github · create_issue", "Bug", "mcp")
    s = tools.summarize("Mystery", {"n": 1, "q": " hello "})
    assert (s.verb, s.target) == ("Mystery", "hello")


def test_summarize_tolerates_bad_input() -> None:
    assert tools.summarize("Read", None).verb == "Reading"
    assert tools.summarize("Grep", "not a dict").verb == "Searching"
    assert tools.summarize("AskUserQuestion", {"questions": ["oops"]}).target == ""


def test_summarize_misc() -> None:
    assert tools.summarize("TodoWrite", {"todos": [{}, {}]}).target == "2 todos"
    fetch = tools.summarize("WebFetch", {"url": "https://docs.example.com/a/b?x=1"})
    assert fetch.target == "docs.example.com/a/b"
    grep = tools.summarize("Grep", {"pattern": "TODO", "path": "/work/app/src"}, "/work/app")
    assert grep.target == '"TODO" in src'
