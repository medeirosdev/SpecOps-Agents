"""Terminal dashboard built with Textual."""

from __future__ import annotations

import contextlib
import time
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from .. import __version__
from ..hive import Hive
from . import render as r

SPECOPS_THEME = Theme(
    name="specops",
    primary="#f2a65a",
    secondary="#b69cff",
    accent="#4fd3e0",
    warning="#f2a65a",
    error="#ff6b6b",
    success="#5ad19a",
    foreground="#efe8dc",
    background="#12100d",
    surface="#1a1712",
    panel="#24201a",
    dark=True,
    variables={
        "footer-key-foreground": "#f2a65a",
        "block-cursor-background": "#3a3025",
        "block-cursor-foreground": "#efe8dc",
        "block-cursor-text-style": "none",
    },
)


class AgentCard(Static, can_focus=True):
    """One agent. Border title/subtitle carry its name and live status."""

    def __init__(self, agent_id: str, wide: bool = False) -> None:
        super().__init__(id=f"card-{agent_id}", classes="card")
        self.agent_id = agent_id
        self.wide = wide

    def show(self, a: dict[str, Any]) -> None:
        self.agent_id = a["id"]
        self.border_title = r.card_title(a)
        self.border_subtitle = r.card_subtitle(a)
        for status in r.STATUS_COLOR:
            self.set_class(a["status"] == status, f"-{status}")
        self.update(r.card_body(a, self.wide))

    def on_click(self, event: events.Click) -> None:
        self.app.open_agent(self.agent_id)  # type: ignore[attr-defined]

    def key_enter(self) -> None:
        self.app.open_agent(self.agent_id)  # type: ignore[attr-defined]


class AgentScreen(ModalScreen[None]):
    """Full timeline of a single agent, updated live."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "dismiss", "Back"),
        Binding("1", "tab('timeline')", "Timeline"),
        Binding("2", "tab('thoughts')", "Thoughts"),
        Binding("3", "tab('tools')", "Tools"),
        Binding("4", "tab('files')", "Files"),
        Binding("end,G", "bottom", "Latest", show=False),
    ]

    def __init__(self, agent_id: str) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.tab = "timeline"
        self._stick = True

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-panel"):
            yield Static(id="agent-head")
            yield Static(id="agent-tabs")
            with VerticalScroll(id="agent-scroll"):
                yield Static(id="agent-timeline")
            yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.app.refresh_view, force=True)  # type: ignore[attr-defined]

    def action_tab(self, tab: str) -> None:
        self.tab = tab
        self._stick = True
        self.app.refresh_view(force=True)  # type: ignore[attr-defined]

    def action_bottom(self) -> None:
        self._stick = True
        self.query_one("#agent-scroll").scroll_end(animate=False)

    def show(self, a: dict[str, Any], session: dict[str, Any]) -> None:
        if not self.is_mounted:  # a refresh can land between push_screen and compose
            return
        head = Text()
        head.append(a["name"], style=f"bold {r.STATUS_COLOR.get(a['status'], r.DIM)}")
        if a.get("model"):
            head.append(f"  {r.model(a['model'])}", style=r.MUTED)
        head.append("   ")
        head.append_text(r.status_badge(a["status"]))
        desc = session["title"] if a["kind"] == "main" else a.get("description", "")
        if desc:
            head.append("\n" + desc, style=r.TEXT_2)
        head.append("\n▸ ", style=r.ACCENT)
        head.append(r.tilde(a.get("cwd", "")), style=r.TEXT_2)
        if a.get("branch"):
            head.append(f"   ⎇ {a['branch']}", style=r.MUTED)
        tok = a.get("tokens") or {}
        head.append(
            f"   {a.get('tool_count', 0)} tools · {r.tokens(tok.get('out'))} out · "
            f"{r.tokens((tok.get('in') or 0) + (tok.get('cache') or 0))} in",
            style=r.MUTED,
        )
        if a["kind"] == "sub" and a.get("task"):
            head.append("\n\nTask  ", style=f"bold {r.ACCENT}")
            head.append(r.one_line(a["task"], 400), style=r.TEXT_2)
        self.query_one("#agent-head", Static).update(head)

        tabs = Text()
        for i, name in enumerate(("timeline", "thoughts", "tools", "files"), 1):
            label = (
                f" {i} {name.title()}"
                + (f" ({a.get('file_count', 0)})" if name == "files" else "")
                + " "
            )
            tabs.append(label, style="bold reverse" if name == self.tab else r.MUTED)
            tabs.append(" ")
        self.query_one("#agent-tabs", Static).update(tabs)

        scroll = self.query_one("#agent-scroll", VerticalScroll)
        at_bottom = scroll.scroll_y >= scroll.max_scroll_y - 2
        self.query_one("#agent-timeline", Static).update(r.timeline(a, self.tab))
        if self._stick or at_bottom:
            self._stick = False
            self.call_after_refresh(scroll.scroll_end, animate=False)


class SpecOpsApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "SpecOps Claude"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("tab", "focus_next", "Next", show=False),
        Binding("shift+tab", "focus_previous", "Prev", show=False),
        Binding("s", "focus_sessions", "Sessions"),
        Binding("a", "focus_agents", "Agents"),
        Binding("enter", "open_focused", "Timeline", show=True, priority=False),
    ]

    def __init__(self, hive: Hive) -> None:
        super().__init__()
        self.hive = hive
        self.selected: str | None = None
        self.follow = True
        self.snap: dict[str, Any] | None = None
        self._session_ids: list[str] = []
        self._finished_ids: list[str] = []
        self._seen_version = -1
        self._last_paint = 0.0

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static(" SESSIONS", id="sessions-label", classes="label")
                yield OptionList(id="sessions")
            with VerticalScroll(id="colony"):
                yield Static(id="shead")
                yield AgentCard("queen", wide=True)
                yield Static(id="wtitle", classes="label")
                yield Grid(id="workers")
                yield Static(id="ftitle", classes="label")
                yield OptionList(id="finished")
                yield Static(id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(SPECOPS_THEME)
        self.theme = "specops"
        self.hive.start()
        self.set_interval(0.25, self.refresh_view)
        self.query_one("#sessions").focus()

    # ------------------------------------------------------------------ actions
    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.notify(
            f"Follow {'on: jumping to active sessions' if self.follow else 'off'}", timeout=2
        )
        self.refresh_view(force=True)

    def action_focus_sessions(self) -> None:
        self.query_one("#sessions").focus()

    def action_focus_agents(self) -> None:
        self.query_one("#colony AgentCard").focus()

    def action_open_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, AgentCard):
            self.open_agent(focused.agent_id)

    def open_agent(self, agent_id: str) -> None:
        if isinstance(self.screen, AgentScreen):
            return
        self.push_screen(AgentScreen(agent_id))  # the screen paints itself once mounted

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if (
            event.option_list.id == "sessions"
            and event.option.id
            and event.option.id != self.selected
            and not self._syncing
        ):
            self.selected = event.option.id
            self.follow = False
            self.refresh_view(force=True)
            self.query_one("#colony").scroll_home(animate=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "finished" and event.option.id:
            self.open_agent(event.option.id)
        elif event.option_list.id == "sessions":
            self.action_focus_agents()

    _syncing = False

    # ------------------------------------------------------------------ refresh
    def refresh_view(self, force: bool = False) -> None:
        # The interval can fire while the app tears its widgets down on exit; a skipped
        # repaint is harmless since the next tick paints again.
        with contextlib.suppress(NoMatches):
            self._refresh_view(force)

    def _refresh_view(self, force: bool) -> None:
        version = self.hive.version
        now = time.monotonic()
        # Repaint on new data, and ~4x/s anyway so spinners and timers move.
        if not force and version == self._seen_version and now - self._last_paint < 0.24:
            return
        self._seen_version = version
        self._last_paint = now

        snap = self.hive.snapshot(self.selected)
        sessions = snap["sessions"]
        current = next((s for s in sessions if s["id"] == self.selected), None)
        if self.follow and not isinstance(self.screen, AgentScreen):
            busy = [s for s in sessions if s["status"] == "working"]
            if busy and (not current or current["status"] != "working"):
                self.selected = busy[0]["id"]
        if not any(s["id"] == self.selected for s in sessions):
            self.selected = sessions[0]["id"] if sessions else None
        detail = next((s for s in sessions if s["id"] == self.selected and s.get("detail")), None)
        if self.selected and detail is None:
            snap = self.hive.snapshot(self.selected)
            sessions = snap["sessions"]
            detail = next((s for s in sessions if s["id"] == self.selected), None)
        self.snap = snap

        self._paint_topbar(sessions)
        self._paint_sessions(sessions)
        self._paint_colony(detail)
        screen = self.screen
        if isinstance(screen, AgentScreen):
            agent = next(
                (a for a in (detail or {}).get("agents", []) if a["id"] == screen.agent_id), None
            )
            if agent and detail:
                screen.show(agent, detail)

    def _paint_topbar(self, sessions: list[dict[str, Any]]) -> None:
        working = sum(s.get("working", 0) for s in sessions)
        active = sum(1 for s in sessions if s["status"] == "working")
        t = Text()
        t.append(" ▲▼ ", style=f"bold {r.ACCENT}")
        t.append("SpecOps Claude", style="bold")
        t.append(f" {__version__}", style=r.DIM)
        t.append("   ")
        t.append(f"{working}", style=f"bold {r.STATUS_COLOR['tool'] if working else r.TEXT_2}")
        t.append(f" agent{'s' if working != 1 else ''} working  ", style=r.TEXT_2)
        t.append(f"{active}", style="bold")
        t.append(f" active  {len(sessions)} sessions", style=r.TEXT_2)
        t.append(
            "   follow " + ("on" if self.follow else "off"),
            style=r.STATUS_COLOR["waiting"] if self.follow else r.DIM,
        )
        self.query_one("#topbar", Static).update(t)

    def _paint_sessions(self, sessions: list[dict[str, Any]]) -> None:
        lst = self.query_one("#sessions", OptionList)
        width = max(20, lst.size.width - 4) if lst.size.width else 30
        ids = [s["id"] for s in sessions]
        prompts = [r.session_prompt(s, width) for s in sessions]
        self._syncing = True
        try:
            if ids != self._session_ids:
                self._session_ids = ids
                lst.set_options([Option(p, id=i) for p, i in zip(prompts, ids, strict=True)])
            else:
                for p, i in zip(prompts, ids, strict=True):
                    lst.replace_option_prompt(i, p)
            if self.selected in ids:
                idx = ids.index(self.selected)
                if lst.highlighted != idx:
                    lst.highlighted = idx
        finally:
            self.call_after_refresh(self._end_sync)

    def _end_sync(self) -> None:
        self._syncing = False

    def _paint_colony(self, s: dict[str, Any] | None) -> None:
        empty = self.query_one("#empty", Static)
        queen_card = self.query_one("#card-queen", AgentCard)
        if not s:
            for w in ("#shead", "#wtitle", "#ftitle"):
                self.query_one(w, Static).update("")
            queen_card.display = False
            self.query_one("#finished").display = False
            empty.display = True
            empty.update(
                Text.assemble(
                    ("All quiet on the field.\n\n", "bold"),
                    ("No Claude Code or Antigravity sessions found under\n", r.TEXT_2),
                    (f"{self.hive.root}\n\n", r.ACCENT),
                    ("Start `claude` in any project and it appears here live,\nor try ", r.TEXT_2),
                    ("specops --demo", "bold"),
                )
            )
            return
        empty.display = False
        queen_card.display = True

        head = Text()
        head.append(s["project"], style="bold")
        head.append("  " + s["title"], style=r.TEXT_2)
        head.append("\n▸ ", style=r.ACCENT)
        head.append(r.tilde(s.get("cwd", "")), style=r.TEXT_2)
        if s.get("branch"):
            head.append(f"   ⎇ {s['branch']}", style=r.MUTED)
        if s.get("source") in r.SOURCE_NAME:
            head.append(f"   {r.SOURCE_NAME[s['source']]}", style=f"bold {r.CAT_COLOR['web']}")
        agents = s["agents"]
        tools_used = sum(a.get("tool_count", 0) for a in agents)
        out = sum((a.get("tokens") or {}).get("out", 0) for a in agents)
        totals = f"   {len(agents)} agents · {tools_used} tool calls"
        if out or s.get("source", "claude") == "claude":
            totals += f" · {r.tokens(out)} tokens out"
        head.append(totals, style=r.MUTED)
        self.query_one("#shead", Static).update(head)

        queen, *subs = agents
        queen_card.show(queen)

        ended = {"done", "idle", "error", "interrupted"}
        working = [a for a in subs if a["status"] not in ended]
        finished = sorted(
            (a for a in subs if a["status"] in ended),
            key=lambda a: a.get("last_ts") or 0,
            reverse=True,
        )

        self.query_one("#wtitle", Static).update(
            Text.assemble((" WORKING NOW ", "bold"), (str(len(working)), r.ACCENT))
            if working
            else ""
        )
        grid = self.query_one("#workers", Grid)
        cards = {c.agent_id: c for c in grid.query(AgentCard)}
        wanted = [a["id"] for a in working]
        for aid, card in cards.items():
            if aid not in wanted:
                card.remove()
        for a in working:
            card = cards.get(a["id"])
            if card is None:
                card = AgentCard(a["id"])
                grid.mount(card)
            card.show(a)
        grid.display = bool(working)

        self.query_one("#ftitle", Static).update(
            Text.assemble((" FINISHED ", "bold"), (str(len(finished)), r.MUTED)) if finished else ""
        )
        fl = self.query_one("#finished", OptionList)
        fl.display = bool(finished)
        ids = [a["id"] for a in finished]
        prompts = [r.finished_prompt(a) for a in finished]
        if ids != self._finished_ids:
            self._finished_ids = ids
            fl.set_options([Option(p, id=i) for p, i in zip(prompts, ids, strict=True)])
            if ids and fl.highlighted is None:
                fl.highlighted = 0  # so Enter works as soon as the list gets focus
        else:
            for p, i in zip(prompts, ids, strict=True):
                fl.replace_option_prompt(i, p)
