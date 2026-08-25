"""In-process state: dedup, single-flight, per-user cooldown.

The service runs with --max-instances=1, so in-process state is reliable, but
gunicorn serves requests on several threads, so every mutation here is guarded
by a lock (spec 7).

The spec describes these as module-level objects. They are instead held on one
BotState instance created once per application, which is the same thing in the
one-process deployment and keeps tests from leaking state into each other.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator

DEDUP_HISTORY_SIZE = 200
COOLDOWN_SECONDS = 30.0


class UpdateDeduplicator:
    """Remembers the last N update_ids Telegram delivered.

    Telegram redelivers an update if the webhook does not answer in time, and a
    redelivered /code would consume a second login code. update_id is unique per
    update, so check-and-insert under one lock is enough.
    """

    def __init__(self, max_entries: int = DEDUP_HISTORY_SIZE) -> None:
        self._max_entries = max_entries
        self._order: deque[int] = deque()
        self._seen: set[int] = set()
        self._lock = threading.Lock()

    def check_and_add(self, update_id: int) -> bool:
        """Return True if this update_id was already seen (so: skip it)."""
        with self._lock:
            if update_id in self._seen:
                return True
            self._seen.add(update_id)
            self._order.append(update_id)
            while len(self._order) > self._max_entries:
                self._seen.discard(self._order.popleft())
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._order)


class Cooldown:
    """Per-user rate limit on code lookups."""

    def __init__(
        self,
        seconds: float = COOLDOWN_SECONDS,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._monotonic = monotonic
        self._last: dict[int, float] = {}
        self._lock = threading.Lock()

    def remaining(self, user_id: int) -> float:
        """Seconds the user must still wait; 0.0 when they may go ahead."""
        with self._lock:
            last = self._last.get(user_id)
            if last is None:
                return 0.0
            elapsed = self._monotonic() - last
            return max(0.0, self._seconds - elapsed)

    def mark(self, user_id: int) -> None:
        """Record that a lookup ran for this user, starting the cooldown."""
        with self._lock:
            self._last[user_id] = self._monotonic()


class SingleFlight:
    """Only one code lookup may be in progress at a time.

    Two people asking at once could otherwise consume one code each with no way
    to tell which code belongs to whom.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        """Non-blocking acquire. Yields True when the caller owns the lock.

        The release always happens in a finally block, so the lock cannot leak
        even if the body raises.
        """
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    @property
    def busy(self) -> bool:
        return self._lock.locked()


class BotState:
    """Everything the bot remembers between requests. Nothing is persisted."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        dedup_size: int = DEDUP_HISTORY_SIZE,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dedup = UpdateDeduplicator(dedup_size)
        self.cooldown = Cooldown(cooldown_seconds, monotonic=monotonic)
        self.single_flight = SingleFlight()
