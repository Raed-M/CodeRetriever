"""Behaviour under gunicorn threads: single-flight, dedup, cooldown races."""

from __future__ import annotations

import threading

from codebot.state import Cooldown, SingleFlight, UpdateDeduplicator
from conftest import ALLOWED_USER_ID, OTHER_USER_ID, BlockingSource, message_update


def test_second_concurrent_code_request_gets_the_busy_message(harness):
    """Two friends tapping at once must not consume a link each."""
    source = BlockingSource()
    h = harness(source, allowed={ALLOWED_USER_ID, OTHER_USER_ID}, real_clock=True)

    first_client = h.new_client()
    second_client = h.new_client()
    results: dict[str, int] = {}

    def first():
        response = h.post_update(
            message_update("/code", user_id=ALLOWED_USER_ID), client=first_client
        )
        results["first"] = response.status_code

    thread = threading.Thread(target=first)
    thread.start()
    assert source.entered.wait(timeout=5), "the first lookup never started"

    # While the first lookup is parked inside fetch, a second one arrives.
    response = h.post_update(
        message_update("/code", user_id=OTHER_USER_ID), client=second_client
    )
    assert response.status_code == 200
    assert "Someone else is fetching a link" in h.telegram.last_text

    source.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert results["first"] == 200
    assert source.calls == 1, "only one lookup may reach the code source"
    assert any("https://login.example.com/blocked" in text for text in h.telegram.sent_texts)


def test_lock_is_released_even_when_the_lookup_explodes(harness):
    class Exploding:
        calls = 0

        def fetch(self):
            Exploding.calls += 1
            raise RuntimeError("boom")

    h = harness(Exploding(), real_clock=True, cooldown_seconds=0.0)
    h.post_update(message_update("/code"))
    assert not h.bot._state.single_flight.busy

    h.post_update(message_update("/code"))
    assert Exploding.calls == 2, "a leaked lock would block every later request"


def test_dedup_is_atomic_under_threads():
    dedup = UpdateDeduplicator(200)
    duplicates: list[bool] = []
    lock = threading.Lock()
    start = threading.Event()

    def worker():
        start.wait()
        seen = dedup.check_and_add(77)
        with lock:
            duplicates.append(seen)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=5)

    assert duplicates.count(False) == 1, "exactly one thread may claim the update"
    assert duplicates.count(True) == 23


def test_single_flight_allows_one_holder_then_frees_the_lock():
    single_flight = SingleFlight()
    with single_flight.acquire() as first:
        assert first is True
        with single_flight.acquire() as second:
            assert second is False
    assert not single_flight.busy


def test_single_flight_releases_on_exception():
    single_flight = SingleFlight()
    try:
        with single_flight.acquire() as acquired:
            assert acquired
            raise ValueError("boom")
    except ValueError:
        pass
    assert not single_flight.busy


def test_cooldown_arithmetic():
    now = [100.0]
    cooldown = Cooldown(30.0, monotonic=lambda: now[0])

    assert cooldown.remaining(1) == 0.0
    cooldown.mark(1)
    assert cooldown.remaining(1) == 30.0
    now[0] += 29.0
    assert cooldown.remaining(1) == 1.0
    assert cooldown.remaining(2) == 0.0, "cooldowns are per user"
    now[0] += 1.5
    assert cooldown.remaining(1) == 0.0
