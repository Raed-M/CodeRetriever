"""A three-method Telegram Bot API client.

Only sendMessage and answerCallbackQuery are used at runtime (setWebhook is a
manual, one-off setup step). A full bot framework would fight the
request-response model this service is built on, so this calls the HTTP API
directly (spec 3, 12).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SECONDS = 10.0
ERROR_BODY_LOG_CHARS = 200

# The callback_data stays "get_code" even though the button copy changed:
# buttons already sitting in people chat history still send the old value.
GET_CODE_CALLBACK_DATA = "get_code"
GET_CODE_KEYBOARD = {
    "inline_keyboard": [[{"text": "Get login link", "callback_data": GET_CODE_CALLBACK_DATA}]]
}


class TelegramClient:
    """Thin wrapper over the Bot API.

    No method raises: an outbound failure is logged and reported as False. The
    webhook must still answer 200, because making Telegram redeliver the update
    is worse than a missing reply (spec 12).
    """

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: float = TELEGRAM_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self._url_prefix = "{0}/bot{1}/".format(api_base.rstrip("/"), token)
        self._timeout = timeout
        self._session = session or requests.Session()

    def _redact(self, text: str) -> str:
        """Strip the bot token out of anything on its way to the logs.

        requests puts the full request URL into its exception messages, and that
        URL contains the token (spec 5.4).
        """
        if self._token and self._token in text:
            text = text.replace(self._token, "<BOT_TOKEN>")
        return text

    def _call(self, method: str, payload: dict[str, Any]) -> bool:
        try:
            response = self._session.post(
                self._url_prefix + method, json=payload, timeout=self._timeout
            )
        except requests.RequestException as exc:
            logger.error(
                "telegram api call failed",
                extra={
                    "event": "telegram_request_error",
                    "method": method,
                    "error_type": type(exc).__name__,
                    "error": self._redact(str(exc))[:ERROR_BODY_LOG_CHARS],
                },
            )
            return False

        if response.status_code != 200:
            logger.error(
                "telegram api returned an error",
                extra={
                    "event": "telegram_api_error",
                    "method": method,
                    "status_code": response.status_code,
                    "body_prefix": self._redact(response.text or "")[:ERROR_BODY_LOG_CHARS],
                },
            )
            return False
        return True

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)
