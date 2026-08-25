"""Update handling: parse, authorise, route, fetch, reply.

Everything here is synchronous and finishes before the webhook returns 200.
Cloud Run throttles CPU once a response is sent, so there is no background work
and no fire-and-forget thread anywhere in this service (spec 11).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import messages
from .code_source import CodeResult, CodeSource
from .state import BotState
from .telegram_api import GET_CODE_CALLBACK_DATA, GET_CODE_KEYBOARD, TelegramClient

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3.0
LOOKUP_DEADLINE_SECONDS = 45.0
# Do not start another poll unless a full interval is left; a fetch that starts
# at the very edge of the deadline would run past it.
MIN_FETCH_BUDGET_SECONDS = POLL_INTERVAL_SECONDS

MESSAGE_KINDS = ("message", "callback")


@dataclass(frozen=True)
class Interaction:
    """The handful of fields this bot needs out of a Telegram Update."""

    kind: str
    user_id: int
    chat_id: int | None
    chat_type: str
    username: str | None
    text: str = ""
    callback_query_id: str | None = None
    callback_data: str | None = None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_update(update: dict[str, Any]) -> Interaction | None:
    """Pull an Interaction out of an Update, or None if there is nothing to do.

    Only message and callback_query updates are requested in setWebhook, but a
    stray update type (or a malformed payload) must not raise.
    """
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from")
        user_id = _as_int(sender.get("id")) if isinstance(sender, dict) else None
        if user_id is None:
            return None
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = _as_int(chat.get("id")) if isinstance(chat, dict) else None
        chat_type = str(chat.get("type", "private")) if isinstance(chat, dict) else "private"
        callback_id = callback.get("id")
        return Interaction(
            kind="callback",
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
            username=sender.get("username") if isinstance(sender, dict) else None,
            callback_query_id=str(callback_id) if callback_id is not None else None,
            callback_data=str(callback.get("data") or ""),
        )

    message = update.get("message")
    if isinstance(message, dict):
        sender = message.get("from")
        user_id = _as_int(sender.get("id")) if isinstance(sender, dict) else None
        chat = message.get("chat")
        chat_id = _as_int(chat.get("id")) if isinstance(chat, dict) else None
        if user_id is None or chat_id is None:
            return None
        return Interaction(
            kind="message",
            user_id=user_id,
            chat_id=chat_id,
            chat_type=str(chat.get("type", "private")),
            username=sender.get("username") if isinstance(sender, dict) else None,
            text=str(message.get("text") or ""),
        )

    return None


def parse_command(text: str) -> str | None:
    """Return the bare command name, or None when the text is not a command.

    Telegram appends the bot username in groups (/code@my_bot), so strip that.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    word = stripped.split(maxsplit=1)[0]
    return word.split("@", 1)[0][1:].lower()


class Bot:
    """Routes one update to one reply. Never raises out of handle_update."""

    def __init__(
        self,
        *,
        allowed_user_ids: frozenset[int],
        telegram: TelegramClient,
        code_source: CodeSource,
        state: BotState | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        deadline_seconds: float = LOOKUP_DEADLINE_SECONDS,
    ) -> None:
        self._allowed = allowed_user_ids
        self._telegram = telegram
        self._source = code_source
        self._monotonic = monotonic
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._deadline_seconds = deadline_seconds
        self._state = state if state is not None else BotState(monotonic=monotonic)

    # -- entry point ----------------------------------------------------

    def monotonic(self) -> float:
        """The clock this bot measures elapsed time on.

        Callers that want to time a request from before handle_update (the
        webhook does) must read it from here, so that every deadline in the
        request is measured against one clock.
        """
        return self._monotonic()

    def handle_update(self, update: dict[str, Any], started_at: float | None = None) -> None:
        """Handle one Telegram Update. Elapsed time is measured from started_at,
        which the webhook captures the moment the request arrives."""
        started_at = self._monotonic() if started_at is None else started_at

        update_id = _as_int(update.get("update_id"))
        if update_id is None:
            logger.warning(
                "update without a usable update_id",
                extra={"event": "malformed_update", "keys": sorted(update.keys())[:10]},
            )
            return

        interaction = parse_update(update)
        if interaction is None:
            logger.info(
                "ignoring update with nothing actionable in it",
                extra={"event": "update_ignored", "update_id": update_id},
            )
            return

        # Whitelist first, and before dedup, so every attempt gets logged.
        # Unauthorised users get no reply of any kind: a silent bot tells a
        # prober nothing (spec 5.3).
        if interaction.user_id not in self._allowed:
            logger.warning(
                "unauthorised user attempted to use the bot",
                extra={
                    "event": "unauthorised",
                    "update_id": update_id,
                    "user_id": interaction.user_id,
                    "username": interaction.username,
                    "kind": interaction.kind,
                },
            )
            return

        if self._state.dedup.check_and_add(update_id):
            logger.info(
                "duplicate update dropped",
                extra={
                    "event": "duplicate_update",
                    "update_id": update_id,
                    "user_id": interaction.user_id,
                },
            )
            return

        # Group chats are out of scope (spec 14), and relaying a login code into
        # a room full of people would be an actual leak, so non-private chats
        # are ignored outright.
        if interaction.chat_type and interaction.chat_type != "private":
            logger.warning(
                "ignoring update from a non-private chat",
                extra={
                    "event": "non_private_chat",
                    "update_id": update_id,
                    "user_id": interaction.user_id,
                    "chat_type": interaction.chat_type,
                },
            )
            return

        self._route(interaction, started_at, update_id)

    # -- routing --------------------------------------------------------

    def _route(self, interaction: Interaction, started_at: float, update_id: int) -> None:
        if interaction.kind == "callback":
            if interaction.callback_data == GET_CODE_CALLBACK_DATA:
                self._handle_code(interaction, started_at, update_id)
                return
            # Unknown button: stop the spinner and do nothing else.
            if interaction.callback_query_id:
                self._telegram.answer_callback_query(interaction.callback_query_id)
            logger.info(
                "unknown callback_data ignored",
                extra={
                    "event": "unknown_callback",
                    "update_id": update_id,
                    "user_id": interaction.user_id,
                },
            )
            return

        command = parse_command(interaction.text)
        if command in ("start", "help"):
            self._reply(interaction, messages.START, keyboard=True)
            return
        if command == "code":
            self._handle_code(interaction, started_at, update_id)
            return

        self._reply(interaction, messages.HINT)

    def _reply(
        self,
        interaction: Interaction,
        text: str,
        *,
        keyboard: bool = False,
    ) -> None:
        if interaction.chat_id is None:
            logger.warning(
                "no chat to reply in",
                extra={"event": "reply_without_chat", "user_id": interaction.user_id},
            )
            return
        self._telegram.send_message(
            interaction.chat_id,
            text,
            reply_markup=GET_CODE_KEYBOARD if keyboard else None,
            parse_mode="HTML",
        )

    # -- the code flow --------------------------------------------------

    def _handle_code(self, interaction: Interaction, started_at: float, update_id: int) -> None:
        # Answer the callback before anything slow, or the button spins on the
        # user screen for the whole lookup (spec 6.1).
        if interaction.kind == "callback" and interaction.callback_query_id:
            self._telegram.answer_callback_query(interaction.callback_query_id)

        # No chat to answer in means the code would be fetched and then thrown
        # away, and a fetched code is a consumed code.
        if interaction.chat_id is None:
            logger.warning(
                "code request with no chat to reply in, dropped",
                extra={
                    "event": "code_request_without_chat",
                    "update_id": update_id,
                    "user_id": interaction.user_id,
                },
            )
            return

        user_id = interaction.user_id

        waiting = self._state.cooldown.remaining(user_id)
        if waiting > 0:
            logger.info(
                "code request rejected by cooldown",
                extra={
                    "event": "cooldown_block",
                    "update_id": update_id,
                    "user_id": user_id,
                    "seconds_remaining": round(waiting, 1),
                },
            )
            self._reply(interaction, messages.cooldown(math.ceil(waiting)), keyboard=True)
            return

        with self._state.single_flight.acquire() as acquired:
            if not acquired:
                logger.info(
                    "code request rejected, another lookup is in flight",
                    extra={
                        "event": "single_flight_busy",
                        "update_id": update_id,
                        "user_id": user_id,
                    },
                )
                self._reply(interaction, messages.BUSY, keyboard=True)
                return

            # The cooldown starts once a lookup really begins, so a user who was
            # only told to wait is not penalised for it.
            self._state.cooldown.mark(user_id)
            self._run_lookup(interaction, started_at, update_id)

    def _run_lookup(self, interaction: Interaction, started_at: float, update_id: int) -> None:
        """Poll the code source and reply. Runs holding the single-flight lock."""
        # 45 seconds of silence reads as a broken bot, so say something first.
        self._reply(interaction, messages.INTERIM)

        deadline = started_at + self._deadline_seconds
        result, attempts = self._poll(deadline)
        elapsed = round(self._monotonic() - started_at, 1)
        base_extra = {
            "update_id": update_id,
            "user_id": interaction.user_id,
            "attempts": attempts,
            "elapsed_seconds": elapsed,
        }

        if result.status == "ok" and result.link:
            # The link is never logged, only the fact of delivery.
            logger.info(
                "login link delivered",
                extra=dict(base_extra, event="link_delivered", age_seconds=result.age_seconds),
            )
            self._reply(
                interaction, messages.login_link_delivered(result.link, result.age_seconds)
            )
            return

        if result.status == "error":
            logger.error(
                "code lookup failed",
                extra=dict(base_extra, event="code_lookup_error", detail=result.detail),
            )
            self._reply(interaction, messages.LOOKUP_FAILED, keyboard=True)
            return

        logger.info(
            "no code found before the deadline",
            extra=dict(base_extra, event="code_not_found"),
        )
        self._reply(interaction, messages.timeout(int(self._deadline_seconds)), keyboard=True)

    def _poll(self, deadline: float) -> tuple[CodeResult, int]:
        """Ask the source every poll_interval until it answers or time runs out.

        An error is terminal: a misconfigured mail-side script would otherwise
        keep the user waiting the full deadline for a failure that is already
        known (spec 6.10).
        """
        attempts = 0
        last: CodeResult = CodeResult(status="not_found")

        while True:
            attempts += 1
            try:
                last = self._source.fetch()
            except Exception as exc:  # a source must not take the webhook down
                logger.exception(
                    "code source raised",
                    extra={"event": "code_source_exception", "error_type": type(exc).__name__},
                )
                return CodeResult(status="error", detail=type(exc).__name__), attempts

            if last.status in ("ok", "error"):
                return last, attempts

            remaining = deadline - self._monotonic()
            if remaining < MIN_FETCH_BUDGET_SECONDS:
                return CodeResult(status="not_found"), attempts
            self._sleep(min(self._poll_interval, remaining))
