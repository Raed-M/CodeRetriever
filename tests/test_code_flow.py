"""The /code path: polling, timeout, dedup, cooldown, single-flight."""

from __future__ import annotations

import threading

from codebot.code_source import CodeResult, StubCodeSource
from conftest import (
    ALLOWED_USER_ID,
    OTHER_USER_ID,
    BlockingSource,
    FakeClock,
    ScriptedSource,
    callback_update,
    message_update,
)


LINK = "https://login.example.com/verify?token=abc123"


def test_code_success_sends_interim_then_the_link(harness):
    h = harness(ScriptedSource(CodeResult(status="ok", link=LINK, age_seconds=12)))
    h.post_update(message_update("/code"))

    texts = h.telegram.sent_texts
    assert len(texts) == 2
    assert "Looking for your login link" in texts[0]
    # On its own line and unformatted, so Telegram makes it tappable.
    assert chr(10) + LINK + chr(10) in texts[1]
    assert "<code>" not in texts[1], "a code block would not be clickable"
    assert "12 seconds ago" in texts[1]
    assert "read the code off the page" in texts[1]
    assert h.telegram.calls[-1][1]["parse_mode"] == "HTML"


def test_link_is_html_escaped(harness):
    """Query strings are full of & and go straight into an HTML message."""
    link = "https://login.example.com/verify?a=1&b=2&next=<x>"
    h = harness(ScriptedSource(CodeResult(status="ok", link=link, age_seconds=1)))
    h.post_update(message_update("/code"))

    text = h.telegram.last_text
    assert "a=1&amp;b=2" in text
    assert "&lt;x&gt;" in text
    assert "<x>" not in text


def test_callback_is_answered_before_the_lookup_starts(harness):
    h = harness(ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=2)))
    h.post_update(callback_update("get_code"))

    # The spinner has to stop first, or the button spins for the whole lookup.
    assert h.telegram.methods == ["answerCallbackQuery", "sendMessage", "sendMessage"]


def test_polling_retries_until_the_link_appears(harness):
    clock = FakeClock()
    source = ScriptedSource(
        CodeResult(status="not_found"),
        CodeResult(status="not_found"),
        CodeResult(status="ok", link="https://login.example.com/second?token=xyz", age_seconds=4),
    )
    h = harness(source, clock=clock)
    h.post_update(message_update("/code"))

    assert source.calls == 3
    assert clock.slept == [3.0, 3.0], "poll interval is 3 seconds"
    assert "https://login.example.com/second?token=xyz" in h.telegram.last_text


def test_not_found_until_the_deadline_gives_the_timeout_message(harness):
    clock = FakeClock()
    h = harness(ScriptedSource(CodeResult(status="not_found")), clock=clock)
    h.post_update(message_update("/code"))

    text = h.telegram.last_text
    assert "No new login email" in text
    assert "45 seconds" in text
    assert "website" in text
    # Gave up inside the deadline, and offered a one-tap retry.
    assert clock.now - 1000.0 <= 45.0
    assert h.telegram.calls[-1][1]["reply_markup"] is not None


def test_stub_not_found_mode_reaches_the_timeout_message(harness):
    """Same path, driven through the real StubCodeSource (spec 13)."""
    clock = FakeClock()
    source = StubCodeSource(
        mode="not_found", delay_seconds=0.0, sleep=clock.sleep, monotonic=clock.monotonic
    )
    h = harness(source, clock=clock)
    h.post_update(message_update("/code"))
    assert "No new login email" in h.telegram.last_text


def test_stub_delayed_mode_returns_a_link_after_polling(harness):
    clock = FakeClock()
    source = StubCodeSource(
        mode="delayed",
        link="https://login.example.com/delayed",
        delay_seconds=7.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    h = harness(source, clock=clock)
    h.post_update(message_update("/code"))
    assert "https://login.example.com/delayed" in h.telegram.last_text


def test_source_error_reports_failure_without_waiting_out_the_deadline(harness):
    clock = FakeClock()
    h = harness(ScriptedSource(CodeResult(status="error", detail="html error page")), clock=clock)
    h.post_update(message_update("/code"))

    assert "lookup failed" in h.telegram.last_text.lower()
    assert clock.slept == [], "an error is terminal, there is nothing to wait for"


def test_duplicate_update_id_is_processed_once(harness):
    source = ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=1))
    h = harness(source)
    update = message_update("/code")

    h.post_update(update)
    first_call_count = len(h.telegram.calls)
    response = h.post_update(update)

    assert response.status_code == 200
    assert source.calls == 1
    assert len(h.telegram.calls) == first_call_count, "the redelivery must be silent"


def test_dedup_history_is_bounded(harness):
    h = harness()
    for update_id in range(1, 260):
        h.bot._state.dedup.check_and_add(update_id)
    assert len(h.bot._state.dedup) == 200
    assert h.bot._state.dedup.check_and_add(259) is True
    assert h.bot._state.dedup.check_and_add(1) is False, "oldest ids fall out of the window"


def test_cooldown_blocks_a_second_request(harness):
    clock = FakeClock()
    source = ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=1))
    h = harness(source, clock=clock)

    h.post_update(message_update("/code"))
    h.telegram.reset()
    clock.advance(5)
    h.post_update(message_update("/code"))

    assert source.calls == 1, "the second request must not consume a link"
    assert "Wait" in h.telegram.last_text
    assert h.telegram.sent_texts[0].startswith("You just asked")


def test_cooldown_expires_after_30_seconds(harness):
    clock = FakeClock()
    source = ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=1))
    h = harness(source, clock=clock)

    h.post_update(message_update("/code"))
    clock.advance(31)
    h.post_update(message_update("/code"))

    assert source.calls == 2


def test_cooldown_is_per_user(harness):
    clock = FakeClock()
    source = ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=1))
    h = harness(source, allowed={ALLOWED_USER_ID, OTHER_USER_ID}, clock=clock)

    h.post_update(message_update("/code", user_id=ALLOWED_USER_ID))
    h.post_update(message_update("/code", user_id=OTHER_USER_ID))

    assert source.calls == 2


def test_cooldown_is_not_started_by_a_rejected_request(harness):
    """Being told to wait must not itself start a 30 second penalty."""
    clock = FakeClock()
    h = harness(ScriptedSource(CodeResult(status="ok", link="https://login.example.com/one", age_seconds=1)), clock=clock)

    h.post_update(message_update("/code"))
    clock.advance(10)
    h.post_update(message_update("/code"))
    clock.advance(21)
    h.telegram.reset()
    h.post_update(message_update("/code"))

    assert "https://login.example.com/one" in h.telegram.last_text


def test_callback_without_a_chat_does_not_consume_a_code(harness):
    """A code fetched with nowhere to send it is a code burned for nothing."""
    source = ScriptedSource(CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=1))
    h = harness(source)

    update = callback_update("get_code")
    del update["callback_query"]["message"]
    h.post_update(update)

    assert source.calls == 0
    assert h.telegram.methods == ["answerCallbackQuery"], "the spinner still stops"


def test_cooldown_message_rounds_up_not_past_the_window(harness):
    clock = FakeClock()
    h = harness(ScriptedSource(CodeResult(status="ok", link="https://login.example.com/one", age_seconds=1)), clock=clock)
    h.post_update(message_update("/code"))
    h.telegram.reset()
    h.post_update(message_update("/code"))
    assert "Wait 30 more seconds" in h.telegram.last_text
