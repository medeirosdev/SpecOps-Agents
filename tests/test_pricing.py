from __future__ import annotations

from specops import pricing


def test_known_models_and_suffixes() -> None:
    assert pricing.price_for("claude-opus-5-5") == pricing.PRICES["claude-opus-5-5"]
    assert pricing.price_for("claude-haiku-4-5-20251001") == pricing.PRICES["claude-haiku-4-5"]
    assert pricing.price_for("claude-opus-5-5[1m]") == pricing.PRICES["claude-opus-5-5"]
    # a newer point release must not be priced as the older one it starts with
    assert pricing.price_for("claude-opus-5-7") is None
    assert pricing.price_for("") is None


def test_context_windows() -> None:
    assert pricing.window_for("claude-opus-5-5") == 1_000_000
    assert pricing.window_for("claude-haiku-4-5") == 200_000
    assert pricing.window_for("something-else") == 200_000
    assert pricing.window_for("something-else", used=250_000) == 1_000_000
    assert pricing.window_for("") == 200_000


def test_cost() -> None:
    assert pricing.cost("claude-sonnet-5-5", input=1_000_000, output=1_000_000) == 12.0
    fast = pricing.cost("claude-opus-5-5", output=1_000_000, fast=True)
    assert fast == 40.0
    assert pricing.cost("claude-opus-4-5", input=10) is None
