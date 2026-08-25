"""Flask application factory and the two HTTP endpoints.

Endpoint contract (spec 4):
  POST /webhook  - always 200 with an empty body once the secret header checks
                   out, whatever happens afterwards. A non-2xx makes Telegram
                   redeliver the update, which can burn a second login code.
                   The one exception is a bad secret header: that request did
                   not come from Telegram, so it gets 403.
  GET  /healthz  - 200 "ok", for manual checks.
"""

from __future__ import annotations

import hmac
import logging

from typing import Any

from flask import Flask, Response, request

from .bot import Bot
from .code_source import CodeSource, build_code_source
from .config import Config
from .logging_setup import configure_logging
from .state import BotState
from .telegram_api import TelegramClient

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

EMPTY_200 = ("", 200)


def _secret_ok(provided: str | None, expected: str) -> bool:
    """Constant-time compare, on bytes so a non-ASCII header cannot raise."""
    if not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8", "replace"), expected.encode("utf-8", "replace")
    )


def create_app(
    config: Config | None = None,
    *,
    telegram: TelegramClient | None = None,
    code_source: CodeSource | None = None,
    bot: Bot | None = None,
    configure_logs: bool = True,
) -> Flask:
    """Build the app. The keyword arguments exist so tests can inject fakes and
    never touch the network."""
    if configure_logs:
        configure_logging()
    config = config or Config.from_env()

    if bot is None:
        telegram = telegram or TelegramClient(
            config.bot_token, api_base=config.telegram_api_base
        )
        code_source = code_source or build_code_source(config)
        bot = Bot(
            allowed_user_ids=config.allowed_user_ids,
            telegram=telegram,
            code_source=code_source,
            state=BotState(),
        )

    app = Flask(__name__)
    app.config["BOT"] = bot
    app.config["CONFIG"] = config

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", status=200, mimetype="text/plain")

    @app.post("/webhook")
    def webhook():
        # Read the clock off the bot: the lookup deadline is measured against
        # it, and tests run the whole flow on a fake one.
        started_at = bot.monotonic()

        if not _secret_ok(request.headers.get(SECRET_HEADER), config.webhook_secret):
            logger.warning(
                "webhook request rejected: bad or missing secret token",
                extra={
                    "event": "bad_secret_token",
                    "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
                    "header_present": SECRET_HEADER in request.headers,
                },
            )
            return Response(status=403)

        try:
            update: Any = request.get_json(force=True, silent=True)
            if not isinstance(update, dict):
                logger.warning(
                    "webhook payload was not a JSON object",
                    extra={"event": "malformed_payload", "payload_type": type(update).__name__},
                )
                return EMPTY_200
            bot.handle_update(update, started_at)
        except Exception:
            # Reporting the failure to Telegram would only buy a redelivery of
            # an update that already failed once.
            logger.exception("unhandled error while processing an update", extra={"event": "handler_crash"})

        return EMPTY_200

    logger.info(
        "service ready",
        extra={
            "event": "startup",
            "code_source": config.code_source,
            "allowed_user_count": len(config.allowed_user_ids),
            "port": config.port,
        },
    )
    return app
