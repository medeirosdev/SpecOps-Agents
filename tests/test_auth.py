"""Pairing codes, lockout and session expiry."""

from __future__ import annotations

import pytest

from specops.web.auth import Auth, AuthError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup() -> tuple[Auth, list[str], Clock]:
    codes: list[str] = []
    clock = Clock()
    auth = Auth(on_code=codes.append, idle=60, absolute=300, clock=clock)
    return auth, codes, clock


def test_code_is_single_use(setup) -> None:
    auth, codes, _ = setup
    first = codes[-1]
    token, expires = auth.pair(first.lower().replace("-", " "))  # forgiving about format
    assert auth.check(token) == expires == 60
    assert codes[-1] != first
    with pytest.raises(AuthError) as err:
        auth.pair(first)
    assert err.value.status == 401


def test_lockout_replaces_the_code(setup) -> None:
    auth, codes, clock = setup
    code = codes[-1]
    for left in (4, 3, 2, 1):
        with pytest.raises(AuthError, match=f"{left} tries left"):
            auth.pair("WRONG-CODE0")
    with pytest.raises(AuthError) as err:
        auth.pair("WRONG-CODE0")
    assert err.value.status == 429 and err.value.retry_after == 300
    assert codes[-1] != code
    with pytest.raises(AuthError) as err:
        auth.pair(codes[-1])  # even the right code waits out the lockout
    assert err.value.status == 429
    clock.now += 301
    assert auth.pair(codes[-1])


def test_idle_and_absolute_expiry(setup) -> None:
    auth, codes, clock = setup
    token, _ = auth.pair(codes[-1])
    for _ in range(4):
        clock.now += 50  # used every 50s: stays alive...
        auth.check(token)
    clock.now += 61
    with pytest.raises(AuthError):
        auth.check(token)  # ...until idle for longer than 60s

    token, _ = auth.pair(codes[-1])
    for _ in range(5):
        clock.now += 50
        auth.check(token)  # 250s old, used every 50s
    clock.now += 50
    with pytest.raises(AuthError):
        auth.check(token)  # 300s: the absolute limit, however active


def test_revoke_and_unknown_tokens(setup) -> None:
    auth, codes, _ = setup
    token, _ = auth.pair(codes[-1])
    for bad in (None, "", "x" * 43, token + "x"):
        with pytest.raises(AuthError):
            auth.check(bad)
    auth.revoke(token)
    with pytest.raises(AuthError):
        auth.check(token)
    assert auth.status(token)["unlocked"] is False


def test_keeps_a_bounded_number_of_sessions(setup) -> None:
    auth, codes, _ = setup
    tokens = [auth.pair(codes[-1])[0] for _ in range(6)]
    with pytest.raises(AuthError):
        auth.check(tokens[0])
    assert auth.check(tokens[-1])
