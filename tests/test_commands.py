"""/start, /help, unknown messages, and the inline button."""

from __future__ import annotations

from codebot.bot import parse_command
from conftest import callback_update, message_update


def _keyboard(call):
    return call[1]["reply_markup"]


def test_start_explains_and_offers_the_button(harness):
    h = harness()
    h.post_update(message_update("/start"))

    method, payload = h.telegram.calls[0]
    assert method == "sendMessage"
    text = payload["text"]
    assert "/code" in text
    assert "restricted" in text.lower()
    # The user must know the login has to be started on the website first.
    assert "website" in text.lower()
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "get_code"


def test_help_matches_start(harness):
    h = harness()
    h.post_update(message_update("/help"))
    h.post_update(message_update("/start"))
    assert h.telegram.sent_texts[0] == h.telegram.sent_texts[1]


def test_command_with_bot_suffix_is_understood(harness):
    h = harness()
    h.post_update(message_update("/code@my_login_bot"))
    assert "Looking for your login link" in h.telegram.sent_texts[0]


def test_unknown_message_gets_a_one_line_hint(harness):
    h = harness()
    h.post_update(message_update("what is the code"))
    assert h.telegram.sent_texts == ["Send /code to fetch the latest login link."]


def test_unknown_command_gets_the_hint(harness):
    h = harness()
    h.post_update(message_update("/status"))
    assert "/code" in h.telegram.last_text


def test_non_text_message_gets_the_hint(harness):
    h = harness()
    update = message_update("/code")
    del update["message"]["text"]
    update["message"]["sticker"] = {"file_id": "abc"}
    h.post_update(update)
    assert "/code" in h.telegram.last_text


def test_unknown_callback_data_is_answered_and_dropped(harness):
    h = harness()
    h.post_update(callback_update("something_else"))
    assert h.telegram.methods == ["answerCallbackQuery"]


def test_parse_command():
    assert parse_command("/code") == "code"
    assert parse_command("  /Code  ") == "code"
    assert parse_command("/code@some_bot extra") == "code"
    assert parse_command("code") is None
    assert parse_command("") is None
