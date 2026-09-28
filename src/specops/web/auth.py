"""Who may change skills from the browser.

Reading the dashboard needs nothing, but writing skills means writing instructions that agents
will follow, so it takes proof of access to the terminal running ``specops web``:

1. On start, a pairing code is printed to that terminal only (never sent over HTTP).
2. Typing it in the browser trades it for a random session token (256 bits). The code is single
   use: a new one is printed as soon as it's used.
3. After ``max_failures`` wrong codes, pairing is locked for ``lockout`` seconds and the code is
   replaced, so guesses never accumulate against the same code.
4. Tokens expire after ``idle`` seconds without use and ``absolute`` seconds in any case. Only
   their SHA-256 is kept, compared in constant time; locking from the browser revokes the token.

The HTTP layer adds the checks that make the token usable only from this page (see server.py).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LENGTH = 10  # ~49 bits


class AuthError(Exception):
    def __init__(self, message: str, status: int, retry_after: float = 0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return f"{raw[:5]}-{raw[5:]}"


def normalize(code: str) -> str:
    return "".join(c for c in str(code).upper() if c in ALPHABET)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass
class _Session:
    digest: bytes
    created: float
    last_seen: float
    client: str


class Auth:
    def __init__(
        self,
        on_code: Callable[[str], None] = lambda code: None,
        idle: float = 15 * 60,
        absolute: float = 8 * 3600,
        max_failures: int = 5,
        lockout: float = 5 * 60,
        max_sessions: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle = idle
        self.absolute = absolute
        self.max_failures = max_failures
        self.lockout = lockout
        self.max_sessions = max_sessions
        self._on_code = on_code
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: list[_Session] = []
        self._failures = 0
        self._locked_until = 0.0
        self._code = ""
        self._rotate()

    def _rotate(self) -> None:
        self._code = new_code()
        self._on_code(self._code)

    def pair(self, code: str, client: str = "") -> tuple[str, float]:
        """Trade the pairing code for a session token. Returns ``(token, expires_in)``."""
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                raise AuthError("too many wrong codes, try later", 429, self._locked_until - now)
            given = normalize(code)
            if not given or not hmac.compare_digest(given.encode(), normalize(self._code).encode()):
                self._failures += 1
                if self._failures >= self.max_failures:
                    self._failures = 0
                    self._locked_until = now + self.lockout
                    self._rotate()
                    raise AuthError("too many wrong codes, try later", 429, self.lockout)
                left = self.max_failures - self._failures
                raise AuthError(f"wrong code ({left} tries left)", 401)
            self._failures = 0
            token = secrets.token_urlsafe(32)
            self._sessions.append(_Session(_digest(token), now, now, client))
            del self._sessions[: -self.max_sessions]
            self._rotate()
            return token, self.idle

    def _find(self, token: str | None, now: float) -> _Session | None:
        self._sessions = [
            s
            for s in self._sessions
            if now - s.last_seen < self.idle and now - s.created < self.absolute
        ]
        if not token:
            return None
        digest = _digest(token)
        for s in self._sessions:
            if hmac.compare_digest(s.digest, digest):
                return s
        return None

    def check(self, token: str | None) -> float:
        """Validate (and refresh) a token; returns seconds until it expires, or raises."""
        with self._lock:
            now = self._clock()
            session = self._find(token, now)
            if session is None:
                raise AuthError("locked: unlock with the code shown in the terminal", 401)
            session.last_seen = now
            return self._expires_in(session, now)

    def status(self, token: str | None) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            session = self._find(token, now)
            return {
                "unlocked": session is not None,
                "expires_in": self._expires_in(session, now) if session else 0,
                "retry_after": max(0.0, self._locked_until - now),
            }

    def _expires_in(self, s: _Session, now: float) -> float:
        return max(0.0, min(s.last_seen + self.idle, s.created + self.absolute) - now)

    def revoke(self, token: str | None) -> None:
        with self._lock:
            session = self._find(token, self._clock())
            if session is not None:
                self._sessions.remove(session)
