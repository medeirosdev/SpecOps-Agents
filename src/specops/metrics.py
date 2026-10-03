"""Usage metrics: how agents compare, by profile, model, source or project.

Everything is computed from the sessions the hive has read, so it covers the same window the
dashboard shows (``--since``). Activities are kept per agent up to a limit, so error and retry
counts of very long agents cover their recent calls only; token and cost totals cover what was
read of each transcript (see ``partial``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .model import MESSAGE_TOOLS, POLLING_TOOLS, Agent, Session

GROUPS = ("profile", "model", "source", "project", "origin")
RETRY_WINDOW = 10  # a call identical to one of the previous this-many calls, with no edit between

SOURCE_NAMES = {
    "claude": "Claude Code",
    "antigravity": "Antigravity",
    "antigravity-cli": "Antigravity",
}


def _retries(agent: Agent) -> int:
    """Tool calls that repeat one of the few calls before them with no edit in between."""
    recent: list[str] = []
    count = 0
    for act in agent.activities:
        if act.kind != "tool":
            continue
        if act.category == "edit":
            recent.clear()
            continue
        if act.tool in POLLING_TOOLS or not act.sig:
            continue
        if act.sig in recent:
            count += 1
        recent = [*recent, act.sig][-RETRY_WINDOW:]
    return count


def agent_row(session: Session, agent: Agent, profiles: dict[str, str]) -> dict[str, Any]:
    """One agent's numbers, plus the labels it is grouped by."""
    tools = [a for a in agent.activities if a.kind == "tool"]
    tokens = agent.tokens()
    name = agent.name() if agent.kind == "main" else agent.agent_type or "subagent"
    origin = profiles.get(name) or ("main" if agent.kind == "main" else "built-in")
    return {
        "session": session.id,
        "agent": agent.id,
        "kind": agent.kind,
        "profile": f"{name} (main)" if agent.kind == "main" else name,
        "origin": origin,  # "profile" / "team" (from SpecOps), "main" or "built-in"
        "model": agent.model or "unknown",
        "source": SOURCE_NAMES.get(agent.source, agent.source),
        "project": session.project_name(),
        "started": agent.started,
        "duration": max(0.0, (agent.last_ts or 0) - (agent.started or agent.last_ts or 0)),
        "tokens_in": tokens["in"] + tokens["cache"],
        "tokens_out": tokens["out"],
        "cache_read": tokens["cache_read"],
        "cost": agent.cost(),
        "tool_calls": agent.tool_count,
        "tool_errors": sum(1 for a in tools if a.status == "error"),
        "retries": _retries(agent),
        "messages": sum(1 for a in tools if a.tool in MESSAGE_TOOLS),
        "spawned": sum(1 for a in tools if a.agent_ref),
        "files_written": sum(1 for f in agent.files.values() if f.writes),
        "context": agent.context,
        "partial": agent.partial,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    calls = sum(r["tool_calls"] for r in rows)
    errors = sum(r["tool_errors"] for r in rows)
    prompt = sum(r["tokens_in"] for r in rows)
    cost = sum(costs) if costs else None
    return {
        "agents": n,
        "sessions": len({r["session"] for r in rows}),
        "tokens_in": prompt,
        "tokens_out": sum(r["tokens_out"] for r in rows),
        "cache_hit": sum(r["cache_read"] for r in rows) / prompt if prompt else None,
        "cost": cost,
        "cost_per_agent": cost / len(costs) if costs else None,
        "tool_calls": calls,
        "calls_per_agent": calls / n if n else 0,
        "error_rate": errors / calls if calls else 0,
        "retries": sum(r["retries"] for r in rows),
        "messages": sum(r["messages"] for r in rows),
        "spawned": sum(r["spawned"] for r in rows),
        "files_written": sum(r["files_written"] for r in rows),
        "avg_duration": sum(r["duration"] for r in rows) / n if n else 0,
        "partial": any(r["partial"] for r in rows),
    }


def collect(
    sessions: Iterable[Session], by: str = "profile", profiles: dict[str, str] | None = None
) -> dict[str, Any]:
    """Totals and one summary per value of ``by``, most expensive (then busiest) first."""
    if by not in GROUPS:
        raise ValueError(f"group by one of: {', '.join(GROUPS)}")
    sessions = list(sessions)
    rows = [agent_row(s, a, profiles or {}) for s in sessions for a in s.agents if a.started]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[by])].append(row)
    summaries = [{"key": key, **_summarize(members)} for key, members in groups.items()]
    summaries.sort(key=lambda g: (-(g["cost"] or 0), -g["tool_calls"], g["key"]))
    starts = [r["started"] for r in rows if r["started"]]
    return {
        "by": by,
        "from": min(starts) if starts else None,
        "to": max((s.last_ts for s in sessions), default=None),
        "totals": _summarize(rows),
        "groups": summaries,
        "agents": rows,
    }


# ------------------------------------------------------------------ text output
def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _num(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}k"
    return f"{value:.0f}"


def _dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def table(report: dict[str, Any]) -> str:
    """The report as a plain-text table, for the terminal."""
    head = (
        report["by"],
        "agents",
        "cost",
        "$/agent",
        "tok out",
        "cache",
        "calls",
        "err",
        "retry",
        "avg time",
    )
    lines = [head]
    for g in [*report["groups"], {"key": "total", **report["totals"]}]:
        lines.append(
            (
                g["key"],
                str(g["agents"]),
                _usd(g["cost"]),
                _usd(g["cost_per_agent"]),
                _num(g["tokens_out"]),
                "-" if g["cache_hit"] is None else f"{g['cache_hit']:.0%}",
                str(g["tool_calls"]),
                f"{g['error_rate']:.0%}",
                str(g["retries"]),
                _dur(g["avg_duration"]),
            )
        )
    widths = [max(len(row[i]) for row in lines) for i in range(len(head))]
    widths[0] = min(widths[0], 40)
    out = []
    for i, row in enumerate(lines):
        cells = [row[0][: widths[0]].ljust(widths[0])]
        cells += [cell.rjust(w) for cell, w in zip(row[1:], widths[1:], strict=True)]
        out.append("  ".join(cells))
        if i == 0 or i == len(lines) - 2:
            out.append("  ".join("-" * w for w in widths))
    if report["totals"]["partial"]:
        out.append("(some long transcripts were only partly read: totals are lower bounds)")
    return "\n".join(out)
