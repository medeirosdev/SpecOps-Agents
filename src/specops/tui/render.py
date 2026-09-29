"""Rich renderables for the terminal UI.

Pure functions of snapshot dicts, so they're easy to test.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.table import Table
from rich.text import Text

STATUS_COLOR = {
    "thinking": "#b69cff",
    "tool": "#f2a65a",
    "writing": "#6cb4ff",
    "waiting": "#4fd3e0",
    "done": "#5ad19a",
    "idle": "#7a7064",
    "error": "#ff6b6b",
    "interrupted": "#ff9d5c",
    "working": "#f2a65a",
}
STATUS_LABEL = {
    "thinking": "thinking",
    "tool": "working",
    "writing": "writing",
    "waiting": "waiting for you",
    "done": "done",
    "idle": "idle",
    "error": "error",
    "interrupted": "interrupted",
    "working": "working",
}
CAT_COLOR = {
    "read": "#6cb4ff",
    "edit": "#f2a65a",
    "shell": "#5ad19a",
    "search": "#b69cff",
    "agent": "#ff7a8a",
    "web": "#4fd3e0",
    "plan": "#c3d65a",
    "ask": "#ffcf5a",
    "mcp": "#f58fd0",
    "other": "#9d9385",
}
# Single-width glyphs only: emoji widths vary between terminals and break alignment.
CAT_GLYPH = {
    "read": "≡",
    "edit": "✎",
    "shell": "❯",
    "search": "⌕",
    "agent": "✦",
    "web": "◍",
    "plan": "☰",
    "ask": "?",
    "mcp": "⌁",
    "other": "•",
}
KIND_GLYPH = {
    "thinking": "∴",
    "text": "❝",
    "prompt": "›",
    "error": "✗",
    "interrupt": "■",
    "command": "/",
}
KIND_COLOR = {
    "thinking": "#b69cff",
    "text": "#6cb4ff",
    "prompt": "#f2a65a",
    "error": "#ff6b6b",
    "interrupt": "#ff9d5c",
    "command": "#c3d65a",
}
WORKING = {"thinking", "tool", "writing"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MUTED = "#8a7f70"
DIM = "#6f665a"
TEXT_2 = "#bfb4a4"
ACCENT = "#f2a65a"


def spinner() -> str:
    return SPINNER[int(time.time() * 10) % len(SPINNER)]


def dur(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}m {sec % 60:02d}s"
    h = m // 60
    if h < 48:
        return f"{h}h {m % 60:02d}m"
    return f"{h // 24}d"


def ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = time.time() - ts
    if s < 10:
        return "just now"
    return re.sub(r" \d+s$", "", dur(s)) + " ago"


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def tokens(n: int | None) -> str:
    n = n or 0
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k" if n < 10_000 else f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def usd(n: float | None) -> str:
    if n is None:
        return ""
    if n < 0.01:
        return "<$0.01"
    return f"${n:.2f}" if n < 100 else f"${round(n):,}"


def cost_label(x: dict[str, Any]) -> str:
    """``≈$1.23``, or ``≥$1.23`` when only the end of a long transcript was read."""
    if x.get("cost") is None:
        return ""
    return ("≥" if x.get("partial") else "≈") + usd(x["cost"])


def model(m: str) -> str:
    x = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-|$)", m or "")
    if not x:
        return m or ""
    return f"{x[1]} {x[2]}" + (f".{x[3]}" if x[3] else "")


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path and path.startswith(home) else path or ""


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_dot(status: str) -> Text:
    color = STATUS_COLOR.get(status, DIM)
    glyph = spinner() if status in WORKING or status == "working" else "●"
    return Text(glyph, style=color)


def status_badge(status: str) -> Text:
    color = STATUS_COLOR.get(status, DIM)
    glyph = spinner() + " " if status in WORKING else "● "
    return Text(glyph + STATUS_LABEL.get(status, status), style=f"bold {color}")


# --------------------------------------------------------------------------- sessions
SOURCE_TAG = {"antigravity": "AG", "antigravity-cli": "AG CLI"}
SOURCE_NAME = {"antigravity": "Antigravity IDE", "antigravity-cli": "Antigravity CLI"}


def session_prompt(s: dict[str, Any], width: int = 30) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append_text(status_dot(s["status"]))
    t.append(" ")
    name_w = max(8, width - 14)
    t.append(one_line(s["project"], name_w), style="bold")
    when = ago(s["last_ts"])
    pad = max(1, width - 2 - min(len(s["project"]), name_w) - len(when))
    t.append(" " * pad + when, style=MUTED)
    t.append("\n  ")
    alerts = s.get("alerts") or 0
    if alerts:
        t.append(f"↻{alerts} ", style=f"bold {STATUS_COLOR['interrupted']}")
    label = SOURCE_TAG.get(s.get("source", ""), "")
    if label:
        t.append(label + " ", style=f"bold {CAT_COLOR['web']}")
    used = len(label) + bool(label) + (len(f"↻{alerts} ") if alerts else 0)
    t.append(one_line(s["title"], width - 3 - used), style=TEXT_2)
    if s.get("agent_count", 1) > 1:
        t.append("\n  ")
        for a in s["agents"][: width - 4]:
            t.append("▪", style=STATUS_COLOR.get(a["status"], DIM))
    return t


# --------------------------------------------------------------------------- agent card
def now_line(a: dict[str, Any]) -> Table:
    status = a["status"]
    cur = a.get("current") or {}
    since = a.get("last_ts")
    if status == "tool" and cur.get("kind") == "tool":
        cat = cur.get("category") or "other"
        glyph, color = CAT_GLYPH.get(cat, "•"), CAT_COLOR.get(cat, MUTED)
        verb, target, since = cur.get("verb", ""), cur.get("target", ""), cur.get("ts")
    elif status == "thinking":
        glyph, color, verb, target = (
            "∴",
            STATUS_COLOR["thinking"],
            "Thinking" + "." * (int(time.time() * 2) % 4),
            "",
        )
    elif status == "writing":
        glyph, color, verb, target = "❝", STATUS_COLOR["writing"], "Writing a reply", ""
    elif status == "waiting":
        glyph, color, verb, target = (
            "›",
            STATUS_COLOR["waiting"],
            "Waiting for your next message",
            "",
        )
    elif status == "done":
        glyph, color, verb = "✓", STATUS_COLOR["done"], "Finished"
        target = (
            f"in {dur(a['last_ts'] - a['started'])}"
            if a.get("started") and a.get("last_ts")
            else ""
        )
        since = None
    elif status == "interrupted":
        glyph, color, verb, target = "■", STATUS_COLOR["interrupted"], "Interrupted", ""
    elif status == "error":
        glyph, color, verb, target = "✗", STATUS_COLOR["error"], "Hit an error", ""
    else:
        glyph, color, verb, target = "◌", DIM, "Idle", ""

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(verb, style=f"bold {color}" if status in WORKING else "bold")
    if target:
        line.append("  " + target, style=TEXT_2)
    elapsed = dur(time.time() - since) if since and status not in ("done",) else ""
    grid.add_row(Text(glyph, style=f"bold {color}"), line, Text(elapsed, style=MUTED))
    return grid


ALERT_GLYPH = {"loop": "↻", "failing": "✗", "churn": "✎", "slow": "◷"}


def alerts_text(a: dict[str, Any]) -> Text | None:
    """Signs the agent is stuck, one per line."""
    alerts = a.get("alerts") or []
    if not alerts:
        return None
    out = Text()
    for i, x in enumerate(alerts):
        color = STATUS_COLOR["error" if x["kind"] == "failing" else "interrupted"]
        if i:
            out.append("\n")
        out.append(ALERT_GLYPH.get(x["kind"], "!") + " ", style=f"bold {color}")
        text = x["text"]
        if x.get("since"):
            text += " " + dur(time.time() - x["since"])
        out.append(text, style=f"bold {color}")
        what = " ".join(p for p in (x.get("verb"), x.get("target")) if p)
        if what:
            out.append("  " + one_line(what, 80), style=TEXT_2)
    return out


def context_text(a: dict[str, Any], width: int = 20) -> Text | None:
    """How full the context window is, plus cache hits and cost."""
    used, window = a.get("context") or 0, a.get("window") or 0
    if not used or not window:
        return None
    share = min(1.0, used / window)
    color = (
        STATUS_COLOR["error"]
        if share >= 0.8
        else STATUS_COLOR["tool"]
        if share >= 0.5
        else CAT_COLOR["read"]
    )
    filled = round(width * share)
    out = Text.assemble(
        ("ctx ", MUTED),
        ("━" * filled, color),
        ("━" * (width - filled), "#3a342c"),
        (f" {tokens(used)}/{tokens(window)}", color if share >= 0.8 else MUTED),
    )
    if a.get("cache_hit") is not None:
        out.append(f" · {round(a['cache_hit'] * 100)}% cached", style=MUTED)
    return out


def thought_text(a: dict[str, Any], limit: int = 260) -> Text | None:
    t = a.get("thought")
    if not t or not t.get("text"):
        return None
    said = t.get("kind") == "text"
    out = Text()
    out.append("❝ " if said else "∴ ", style=KIND_COLOR["text" if said else "thinking"])
    out.append(one_line(t["text"], limit), style=f"italic {TEXT_2}")
    out.append(f"  {ago(t.get('ts'))}", style=DIM)
    return out


def folder_text(a: dict[str, Any], limit: int = 5) -> Text:
    out = Text(no_wrap=False)
    out.append("▸ ", style=ACCENT)
    out.append(tilde(a.get("cwd", "")), style=f"{TEXT_2}")
    files = a.get("files") or []
    if files:
        out.append("\n  ")
        for f in files[:limit]:
            name = "/".join(re.split(r"[\\/]", f["rel"])[-2:])
            out.append(name, style=CAT_COLOR["edit"] if f["writes"] else MUTED)
            out.append("✎ " if f["writes"] else "  ", style=CAT_COLOR["edit"])
        more = (a.get("file_count") or 0) - len(files[:limit])
        if more > 0:
            out.append(f"+{more}", style=DIM)
    return out


def todos_render(a: dict[str, Any], limit: int = 6) -> RenderableType | None:
    todos = a.get("todos") or []
    if not todos:
        return None
    done = sum(1 for t in todos if t["status"] == "completed")
    width = 24
    filled = round(width * done / len(todos))
    lines = [
        Text.assemble(
            ("━" * filled, STATUS_COLOR["done"]),
            ("━" * (width - filled), "#3a342c"),
            (f" {done}/{len(todos)}", MUTED),
        )
    ]
    for t in todos[:limit]:
        if t["status"] == "completed":
            lines.append(
                Text.assemble(("✓ ", STATUS_COLOR["done"]), (t["content"], f"strike {MUTED}"))
            )
        elif t["status"] == "in_progress":
            lines.append(
                Text.assemble(
                    (spinner() + " ", STATUS_COLOR["tool"]),
                    (t.get("active") or t["content"], "bold"),
                )
            )
        else:
            lines.append(Text.assemble(("○ ", DIM), (t["content"], TEXT_2)))
    return Group(*lines)


def trail(a: dict[str, Any], n: int = 28) -> Text:
    acts = [x for x in a.get("activities") or [] if x["kind"] == "tool" or x.get("text")][-n:]
    out = Text()
    for x in acts:
        if x["kind"] == "tool":
            color = (
                STATUS_COLOR["error"]
                if x.get("status") == "error"
                else CAT_COLOR.get(x.get("category") or "other")
            )
            out.append("▇", style=color)
        elif x["kind"] == "text":
            out.append("▅", style=KIND_COLOR["text"])
        else:
            out.append("▃", style=KIND_COLOR.get(x["kind"], MUTED))
    return out


def card_body(a: dict[str, Any], wide: bool = False) -> RenderableType:
    parts: list[RenderableType] = []
    alerts = alerts_text(a)
    if alerts:
        parts.append(alerts)
    parts.append(now_line(a))
    thought = thought_text(a, 360 if wide else 200)
    if thought:
        parts.append(thought)
    parts.append(folder_text(a, 8 if wide else 4))
    todos = todos_render(a)
    if todos:
        parts.append(todos)
    context = context_text(a, 24 if wide else 14)
    if context:
        parts.append(context)
    foot = Table.grid(expand=True)
    foot.add_column()
    foot.add_column(justify="right")
    counts = f"{a.get('tool_count', 0)} tools"
    if a.get("source", "claude") not in SOURCE_NAME:  # Antigravity doesn't log token usage
        counts += f" · {tokens((a.get('tokens') or {}).get('out'))} tok"
    if a.get("cost") is not None:
        counts += f" · {cost_label(a)}"
    foot.add_row(Text(counts, style=MUTED), trail(a, 40 if wide else 22))
    parts.append(foot)
    return Group(*parts)


def card_title(a: dict[str, Any]) -> str:
    color = STATUS_COLOR.get(a["status"], DIM)
    name = a["name"]
    m = model(a.get("model", ""))
    title = f"[b {color}]{name}[/]"
    if m:
        title += f" [{MUTED}]{m}[/]"
    desc = a.get("title") if a["kind"] == "main" else a.get("description")
    if desc:
        title += f" [{TEXT_2}]· {escape(one_line(desc, 48))}[/]"
    return title


def card_subtitle(a: dict[str, Any]) -> str:
    color = STATUS_COLOR.get(a["status"], DIM)
    glyph = spinner() if a["status"] in WORKING else "●"
    return f"[{color}]{glyph} {STATUS_LABEL.get(a['status'], a['status'])}[/]"


# --------------------------------------------------------------------------- finished row
def finished_prompt(a: dict[str, Any]) -> Text:
    took = dur(a["last_ts"] - a["started"]) if a.get("started") and a.get("last_ts") else ""
    color = STATUS_COLOR.get(a["status"], DIM)
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("✓ " if a["status"] == "done" else "● ", style=color)
    t.append(f"{a['name']:<16}", style="bold")
    t.append(one_line(a.get("description") or a.get("task", ""), 50), style=TEXT_2)
    cost = f" · {cost_label(a)}" if a.get("cost") is not None else ""
    t.append(
        f"   {a.get('tool_count', 0)} tools · {took}{cost} · {ago(a.get('last_ts'))}", style=MUTED
    )
    return t


# --------------------------------------------------------------------------- timeline
def timeline(a: dict[str, Any], tab: str = "timeline") -> RenderableType:
    acts = a.get("activities") or []
    if tab == "files":
        table = Table.grid(padding=(0, 2))
        table.add_column(ratio=1)
        table.add_column(justify="right")
        for f in a.get("files") or []:
            badges = Text()
            if f["reads"]:
                badges.append(f" R{f['reads']} ", style=f"{CAT_COLOR['read']}")
            if f["writes"]:
                badges.append(f" W{f['writes']} ", style=f"bold {CAT_COLOR['edit']}")
            table.add_row(Text(f["rel"], style=TEXT_2), badges)
        return table if a.get("files") else Text("No files touched yet.", style=MUTED)

    rows = Table.grid(padding=(0, 1), expand=True)
    rows.add_column(width=8, style=DIM)
    rows.add_column(width=1)
    rows.add_column(ratio=1)
    rows.add_column(justify="right", no_wrap=True)
    last = len(acts) - 1
    for i, x in enumerate(acts):
        kind = x["kind"]
        if tab == "thoughts" and not (kind in ("thinking", "text") and x.get("text")):
            continue
        if tab == "tools" and kind != "tool":
            continue
        if (
            kind == "thinking"
            and not x.get("text")
            and not (i == last and a["status"] == "thinking")
        ):
            continue
        right = Text()
        if kind == "tool":
            cat = x.get("category") or "other"
            glyph = Text(CAT_GLYPH.get(cat, "•"), style=CAT_COLOR.get(cat))
            body = Text()
            body.append(x.get("verb", ""), style="bold")
            if x.get("target"):
                body.append("  " + x["target"], style=TEXT_2)
            if x.get("agent_ref"):
                body.append("  ✦ subagent", style=CAT_COLOR["agent"])
            if x.get("status") == "running":
                right = Text(
                    f"{spinner()} {dur(time.time() - x['ts'])}", style=STATUS_COLOR["tool"]
                )
            elif x.get("ended"):
                style = STATUS_COLOR["error"] if x.get("status") == "error" else MUTED
                right = Text(
                    ("failed " if x.get("status") == "error" else "") + dur(x["ended"] - x["ts"]),
                    style=style,
                )
            if x.get("tool") == "Bash" and x.get("detail"):
                body.append("\n$ " + one_line(x["detail"], 300), style=MUTED)
            if x.get("result") and tab == "tools":
                body.append("\n" + one_line(x["result"], 240), style=DIM)
        else:
            glyph = Text(KIND_GLYPH.get(kind, "•"), style=KIND_COLOR.get(kind, MUTED))
            label = {
                "thinking": "Thought" if x.get("text") else "Thinking…",
                "text": "Said",
                "prompt": "You" if a["kind"] == "main" else "Task",
                "error": "Error",
                "interrupt": "Interrupted",
                "command": "Command",
            }.get(kind, kind)
            body = Text()
            body.append(label, style=f"bold {KIND_COLOR.get(kind, '')}")
            text = x.get("text", "").strip()
            if text:
                limit = 1200 if kind in ("thinking", "text") else 500
                style = f"italic {TEXT_2}" if kind == "thinking" else TEXT_2
                body.append(
                    "\n" + (text if len(text) <= limit else text[: limit - 1] + "…"), style=style
                )
        rows.add_row(clock(x["ts"]), glyph, body, right)
    return rows
