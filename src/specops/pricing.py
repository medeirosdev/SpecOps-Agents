"""Context windows and API list prices, for estimating what a session costs.

Prices are USD per million tokens at Anthropic's first-party API rates (checked 2026-09-25).
Claude Code on a Pro/Max plan isn't billed per token, so the UIs show the result as an
API-equivalent estimate. Models missing here get no cost rather than a guessed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000

# Cache writes cost 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL.
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
# Fast mode (Opus only) is billed at twice the standard rates.
FAST_MULTIPLIER = 2.0


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    window: int = LARGE_WINDOW


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, 0.25),
    "claude-mythos-5-1": Price(10.0, 50.0, 0.25),
    "claude-fable-5": Price(10.0, 50.0, 1.0),
    "claude-mythos-5": Price(10.0, 50.0, 1.0),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20),
    "claude-opus-5": Price(5.0, 25.0, 0.50),
    "claude-opus-4-8": Price(5.0, 25.0, 0.50),
    "claude-opus-4-7": Price(5.0, 25.0, 0.50),
    "claude-opus-4-6": Price(5.0, 25.0, 0.50),
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.20),
    "claude-sonnet-5": Price(2.0, 10.0, 0.20),
    "claude-sonnet-4-6": Price(3.0, 15.0, 0.30),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.10, DEFAULT_WINDOW),
}

_SUFFIX = re.compile(r"(-\d{8})?(\[1m\])?$")


def price_for(model: str) -> Price | None:
    """The price of ``model``, tolerating a date suffix (``-20251001``) or ``[1m]``."""
    return PRICES.get(_SUFFIX.sub("", model or "", count=1))


def window_for(model: str, used: int = 0) -> int:
    """Context window of ``model``; if more than that is in use, it must be the 1M window."""
    model = model or ""
    price = price_for(model)
    window = price.window if price else DEFAULT_WINDOW
    if model.endswith("[1m]") or used > window:
        window = max(window, LARGE_WINDOW)
    return window


def cost(
    model: str,
    *,
    input: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    fast: bool = False,
) -> float | None:
    """USD for one request's usage, or None when the model's price is unknown."""
    price = price_for(model)
    if price is None:
        return None
    usd = (
        input * price.input
        + output * price.output
        + cache_read * price.cache_read
        + cache_write_5m * price.input * CACHE_WRITE_5M
        + cache_write_1h * price.input * CACHE_WRITE_1H
    ) / 1_000_000
    return usd * FAST_MULTIPLIER if fast else usd
