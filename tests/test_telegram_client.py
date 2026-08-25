"""The Bot API client: correct payloads, quiet failures, no token in logs."""

from __future__ import annotations

import json
import logging

import pytest
import requests

from codebot.logging_setup import JsonFormatter, configure_logging
from codebot.telegram_api import GET_CODE_KEYBOARD, TelegramClient

TOKEN = "1234567:AAfake-token-for-tests"


class FakeSession:
    def __init__(self, status_code=200, body="{}", exception=None):
        self.status_code = status_code
        self.body = body
        self.exception = exception
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exception:
            raise self.exception

        class Response:
            status_code = self.status_code
            text = self.body

        return Response()


def client(session):
    return TelegramClient(TOKEN, session=session)


def test_send_message_payload_and_url():
    session = FakeSession()
    assert client(session).send_message(555, "hello", parse_mode="HTML") is True

    call = session.calls[0]
    assert call["url"] == "https://api.telegram.org/bot{0}/sendMessage".format(TOKEN)
    assert call["json"]["chat_id"] == 555
    assert call["json"]["text"] == "hello"
    assert call["json"]["parse_mode"] == "HTML"
    assert call["timeout"] == 10.0


def test_send_message_with_keyboard():
    session = FakeSession()
    client(session).send_message(1, "hi", reply_markup=GET_CODE_KEYBOARD)
    assert session.calls[0]["json"]["reply_markup"] == GET_CODE_KEYBOARD


def test_answer_callback_query():
    session = FakeSession()
    client(session).answer_callback_query("cbq-9")
    assert session.calls[0]["url"].endswith("/answerCallbackQuery")
    assert session.calls[0]["json"] == {"callback_query_id": "cbq-9"}


def test_api_error_is_reported_as_false_not_raised():
    session = FakeSession(status_code=400, body='{"ok":false,"description":"chat not found"}')
    assert client(session).send_message(1, "hi") is False


def test_network_error_is_reported_as_false_not_raised():
    session = FakeSession(exception=requests.Timeout("timed out"))
    assert client(session).send_message(1, "hi") is False


def test_the_bot_token_never_reaches_the_logs(caplog):
    """requests puts the full URL, token and all, into its exception text."""
    session = FakeSession(
        exception=requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded with url: /bot{0}/sendMessage".format(TOKEN)
        )
    )
    with caplog.at_level(logging.DEBUG):
        client(session).send_message(1, "hi")

    rendered = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert TOKEN not in rendered
    assert "<BOT_TOKEN>" in rendered


def test_token_is_stripped_from_error_bodies(caplog):
    session = FakeSession(status_code=401, body="Unauthorized: " + TOKEN)
    with caplog.at_level(logging.DEBUG):
        client(session).send_message(1, "hi")
    assert TOKEN not in caplog.text


def test_json_formatter_emits_one_parseable_line():
    record = logging.LogRecord("codebot", logging.INFO, __file__, 10, "delivered", (), None)
    record.event = "code_delivered"
    record.user_id = 4242

    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["severity"] == "INFO"
    assert payload["message"] == "delivered"
    assert payload["event"] == "code_delivered"
    assert payload["user_id"] == 4242


def test_json_formatter_survives_an_unserialisable_extra():
    record = logging.LogRecord("codebot", logging.WARNING, __file__, 10, "odd", (), None)
    record.thing = object()
    json.loads(JsonFormatter().format(record))


def test_configure_logging_writes_json_to_stdout(capsys):
    configure_logging()
    try:
        logging.getLogger("codebot.test").info("ready", extra={"event": "startup"})
        line = capsys.readouterr().out.strip()
        assert json.loads(line)["event"] == "startup"
    finally:
        logging.getLogger().handlers.clear()
