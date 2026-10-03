"""Command line entry point: ``specops [tui|web] [options]``."""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .hive import Hive, default_root
from .metrics import GROUPS

DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$")
UNITS = {"": 3600, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """``90m``, ``6h``, ``2d``, ``1w``; a bare number means hours; ``0``/``all`` means no limit."""
    text = text.strip().lower()
    if text in ("0", "all", "any"):
        return 0
    match = DURATION.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (try 30m, 6h, 2d, all)")
    return float(match.group(1)) * UNITS[match.group(2)]


def port_number(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r} (must be 1-65535)")
    return port


def _add_common(parser: argparse.ArgumentParser, defaults: bool) -> None:
    """Options accepted both before and after the subcommand.

    The subcommand copies default to ``SUPPRESS`` so that ``specops --demo web`` keeps ``--demo``
    instead of having it reset by the subparser's own default.
    """

    def default(value: object) -> object:
        return value if defaults else argparse.SUPPRESS

    parser.add_argument(
        "--since",
        type=parse_duration,
        default=default(parse_duration("12h")),
        metavar="DURATION",
        help="only show sessions active within this window, e.g. 30m, 6h, 2d, all (default: 12h)",
    )
    parser.add_argument(
        "-p",
        "--project",
        default=default(None),
        metavar="TEXT",
        help="only show projects whose path contains TEXT",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default(None),
        metavar="DIR",
        help=f"transcripts directory (default: {default_root()})",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=default(False),
        help="watch a simulated squad instead of your real sessions",
    )
    parser.add_argument(
        "--no-antigravity",
        action="store_true",
        default=default(False),
        help="don't show Antigravity IDE / CLI conversations",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, defaults=False)

    parser = argparse.ArgumentParser(
        prog="specops",
        description="Watch your Claude Code (and Antigravity) agents and subagents work, live.",
    )
    _add_common(parser, defaults=True)
    parser.add_argument("-V", "--version", action="version", version=f"specops {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{tui,web,metrics}")
    sub.add_parser("tui", parents=[common], help="terminal dashboard (default)")
    web = sub.add_parser("web", parents=[common], help="browser dashboard on localhost")
    web.add_argument(
        "--port", type=port_number, default=7777, help="port to listen on (default: 7777)"
    )
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: 127.0.0.1). Transcripts are private: "
        "think twice before exposing them",
    )
    web.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    web.add_argument(
        "--read-only",
        action="store_true",
        help="show the skill library but never write skills (no unlock code is issued)",
    )
    metrics = sub.add_parser(
        "metrics",
        parents=[common],
        help="compare usage by profile, model, source or project (default window: 7d)",
    )
    metrics.add_argument(
        "--by", choices=GROUPS, default="profile", help="what to compare (default: profile)"
    )
    metrics.add_argument("--json", action="store_true", help="print JSON, for scripts and agents")
    metrics.add_argument(
        "--agents", action="store_true", help="with --json, include one row per agent"
    )
    metrics.add_argument(
        "--limit",
        type=int,
        default=300,
        metavar="N",
        help="read at most the N most recent sessions (default: 300)",
    )
    return parser


def make_hive(args: argparse.Namespace) -> tuple[Hive, object | None]:
    demo = None
    root = args.root
    since = args.since
    if args.demo:
        from .demo import Demo

        demo = Demo()
        root = demo.start()
        since = 0
    sources = []
    if not (args.demo or args.no_antigravity):
        from .antigravity import default_sources

        sources = default_sources(since, args.project)
    return Hive(root=root, since=since, project_filter=args.project, sources=sources), demo


def _print_code(code: str) -> None:
    print(
        f"  \033[38;5;215m🔑 unlock code: \033[1m{code}\033[0m"
        "  \033[2m(single use: type it in the browser to edit skills and profiles)\033[0m",
        flush=True,
    )


def run_web(args: argparse.Namespace) -> int:
    from .skills import Library
    from .web.auth import Auth
    from .web.server import Skills, serve

    hive, demo = make_hive(args)
    hive.poll()
    hive.start()
    library = Library(projects=hive.project_dirs)
    seeded: list[str] = []
    if not (args.read_only or args.demo):
        from .profiles import Studio
        from .starters import seed

        with contextlib.suppress(OSError):
            seeded = seed(Studio(library))
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if args.read_only or args.demo or not local:
        reason = (
            "started with --read-only"
            if args.read_only
            else "editing is off in demo mode"
            if args.demo
            else "editing is disabled on a non-local interface"
        )
        skills = Skills(library, None, reason)
    else:
        skills = Skills(library, None)  # Auth is created once the banner is printed
    server = serve(hive, host=args.host, port=args.port, skills=skills)
    host, port = server.server_address[:2]
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    url = f"http://{shown}:{port}/"
    print(f"\n  \033[38;5;215m◎ SpecOps Agents\033[0m is watching \033[2m{hive.root}\033[0m")
    print(f"     open \033[1m{url}\033[0m  ·  Ctrl+C to stop\n")
    if seeded:
        print(
            f"  \033[2madded {len(seeded)} starter rules, profiles and teams to your library "
            "(research, dev-team, usage-review): publish them from the Profiles tab\033[0m\n"
        )
    if not skills.reason:
        skills.auth = Auth(on_code=_print_code)
        print()
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  \033[33m! listening on a non-local interface: "
            "anyone who can reach it can read\033[0m"
        )
        print("  \033[33m  your transcripts (prompts, code, command output).\033[0m\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("  bye 👋")
    finally:
        server.server_close()
        hive.stop()
        if demo:
            demo.stop()
    return 0


def run_tui(args: argparse.Namespace) -> int:
    from .tui.app import SpecOpsApp

    hive, demo = make_hive(args)
    try:
        SpecOpsApp(hive).run()
    finally:
        hive.stop()
        if demo:
            demo.stop()
    return 0


def run_metrics(args: argparse.Namespace) -> int:
    from . import metrics
    from .profiles import Studio
    from .skills import Library

    hive, demo = make_hive(args)
    hive.max_sessions = max(1, args.limit)
    try:
        hive.poll()
        for _ in range(3):  # sources may need a couple of polls to settle
            if not hive.poll():
                break
        report = hive.metrics(args.by, Studio(Library()).agent_names())
    finally:
        hive.stop()
        if demo:
            demo.stop()
    if args.json:
        if not args.agents:
            report.pop("agents")
        report["window_seconds"] = args.since
        print(json.dumps(report, indent=2, default=str))
    else:
        print(metrics.table(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if args.command == "web":
        return run_web(args)
    if args.command == "metrics":
        if not any(a == "--since" or a.startswith("--since=") for a in argv):
            args.since = parse_duration("7d")
        return run_metrics(args)
    return run_tui(args)


if __name__ == "__main__":
    sys.exit(main())
