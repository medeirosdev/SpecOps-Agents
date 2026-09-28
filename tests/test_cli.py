from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from specops.cli import build_parser, parse_duration, port_number


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("90m", 5400),
        ("6h", 21600),
        ("2d", 172800),
        ("1w", 604800),
        ("3", 10800),
        ("1.5h", 5400),
        (" 2D ", 172800),
        ("all", 0),
        ("0", 0),
        ("any", 0),
    ],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "h", "-1h", "5y", "soon"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(text)


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_defaults() -> None:
    args = parse()
    assert (args.command, args.since, args.project, args.root, args.demo) == (
        None,
        12 * 3600,
        None,
        None,
        False,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--demo", "--since", "2d", "-p", "shop", "--root", "/tmp/x", "web"],
        ["web", "--demo", "--since", "2d", "-p", "shop", "--root", "/tmp/x"],
        ["--demo", "--since", "2d", "tui", "-p", "shop", "--root", "/tmp/x"],
    ],
)
def test_common_options_work_before_or_after_the_subcommand(argv: list[str]) -> None:
    args = parse(*argv)
    expected = (True, 172800, "shop", Path("/tmp/x"))
    assert (args.demo, args.since, args.project, args.root) == expected


def test_option_after_subcommand_wins() -> None:
    assert parse("--since", "1h", "web", "--since", "3h").since == 3 * 3600


def test_web_options() -> None:
    args = parse("web", "--port", "8000", "--host", "0.0.0.0", "--no-browser")
    assert (args.command, args.port, args.host, args.no_browser) == ("web", 8000, "0.0.0.0", True)
    args = parse("web")
    assert (args.port, args.host, args.no_browser) == (7777, "127.0.0.1", False)


@pytest.mark.parametrize("text", ["0", "-1", "65536", "99999", "http"])
def test_port_number_rejects(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        port_number(text)


def test_web_rejects_out_of_range_port(capsys) -> None:
    with pytest.raises(SystemExit):
        parse("web", "--port", "99999")
    assert "must be 1-65535" in capsys.readouterr().err
