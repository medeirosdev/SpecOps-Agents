"""Turn raw tool calls into short, human-friendly descriptions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

# Tool name -> category. Categories drive the icon/colour used by both frontends.
CATEGORIES: dict[str, str] = {
    "Read": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "Bash": "shell",
    "BashOutput": "shell",
    "KillShell": "shell",
    "Monitor": "shell",
    "Grep": "search",
    "Glob": "search",
    "LS": "search",
    "ToolSearch": "search",
    "Agent": "agent",
    "Task": "agent",
    "SendMessage": "agent",
    "SubagentHandback": "agent",
    "TaskStop": "agent",
    "WebFetch": "web",
    "WebSearch": "web",
    "TodoWrite": "plan",
    "EnterPlanMode": "plan",
    "ExitPlanMode": "plan",
    "Skill": "plan",
    "AskUserQuestion": "ask",
}

FILE_TOOLS_WRITE = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
FILE_TOOLS_READ = {"Read"}


@dataclass(frozen=True)
class ToolSummary:
    verb: str
    target: str
    detail: str
    category: str


def category_for(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    return CATEGORIES.get(name, "other")


def shorten(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def relpath(path: str, cwd: str | None) -> str:
    if not path:
        return ""
    if cwd:
        try:
            rel = os.path.relpath(path, cwd)
        except ValueError:  # different drives on Windows
            return path
        if not rel.startswith(".."):
            return rel
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def file_path_of(name: str, args: dict[str, Any]) -> str | None:
    if name in FILE_TOOLS_READ or name in FILE_TOOLS_WRITE:
        return args.get("file_path") or args.get("notebook_path") or None
    return None


def _first_string(args: dict[str, Any]) -> str:
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


def summarize(name: str, args: Any, cwd: str | None = None) -> ToolSummary:
    """Describe a tool call as ``verb`` + ``target`` (+ a longer ``detail``)."""
    if not isinstance(args, dict):
        args = {}
    cat = category_for(name)
    path = file_path_of(name, args)
    rel = relpath(path, cwd) if path else ""

    if name == "Read":
        return ToolSummary("Reading", rel, path or "", cat)
    if name in ("Edit", "MultiEdit"):
        return ToolSummary("Editing", rel, path or "", cat)
    if name == "Write":
        return ToolSummary("Writing", rel, path or "", cat)
    if name == "NotebookEdit":
        return ToolSummary("Editing notebook", rel, path or "", cat)
    if name == "Bash":
        cmd = args.get("command", "")
        label = args.get("description") or (cmd.strip().splitlines()[0] if cmd.strip() else "")
        return ToolSummary("Running", shorten(label, 70), cmd, cat)
    if name == "Grep":
        where = args.get("path") or args.get("glob") or ""
        target = f'"{shorten(args.get("pattern", ""), 40)}"'
        if where:
            target += f" in {relpath(where, cwd)}"
        return ToolSummary("Searching", target, args.get("pattern", ""), cat)
    if name == "Glob":
        return ToolSummary("Finding files", args.get("pattern", ""), args.get("path", ""), cat)
    if name in ("Agent", "Task"):
        kind = args.get("subagent_type") or "agent"
        return ToolSummary(
            f"Spawning {kind}",
            shorten(args.get("description", ""), 60),
            args.get("prompt", ""),
            cat,
        )
    if name == "SubagentHandback":
        return ToolSummary("Handing back report", "", args.get("message", ""), cat)
    if name == "SendMessage":
        return ToolSummary("Messaging", str(args.get("to", "")), args.get("message", ""), cat)
    if name == "WebFetch":
        url = args.get("url", "")
        parsed = urlparse(url)
        return ToolSummary("Fetching", shorten(parsed.netloc + parsed.path, 60), url, cat)
    if name == "WebSearch":
        return ToolSummary("Searching the web", shorten(args.get("query", ""), 60), "", cat)
    if name == "TodoWrite":
        todos = args.get("todos") or []
        return ToolSummary("Planning", f"{len(todos)} todos", "", cat)
    if name == "ToolSearch":
        return ToolSummary("Loading tools", shorten(args.get("query", ""), 60), "", cat)
    if name == "Skill":
        return ToolSummary(
            "Using skill", str(args.get("skill", "")), args.get("args", "") or "", cat
        )
    if name == "AskUserQuestion":
        qs = args.get("questions") or []
        first = qs[0].get("question", "") if qs and isinstance(qs[0], dict) else ""
        return ToolSummary("Asking you", shorten(first, 70), "", cat)
    if name == "ExitPlanMode":
        return ToolSummary("Presenting plan", "", args.get("plan", "") or "", cat)
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "mcp"
        tool = "__".join(parts[2:]) or server
        return ToolSummary(f"{server} · {tool}", shorten(_first_string(args), 60), "", cat)
    return ToolSummary(name, shorten(_first_string(args), 60), "", cat)
