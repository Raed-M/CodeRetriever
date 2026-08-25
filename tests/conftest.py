"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codebot.app import create_app  # noqa: E402
from codebot.bot import Bot  # noqa: E402
from codebot.code_source import CodeResult  # noqa: E402
from codebot.config import Config  # noqa: E402
from codebot.state import BotState  # noqa: E402

ALLOWED_USER_ID = 4242
OTHER_USER_ID = 9999
CHAT_ID = 555
SECRET = "test-webhook-secret"
BOT_TOKEN = "111:test-bot-token"


class FakeTelegram:
    """Stands in for TelegramClient and records every outbound call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def _record(self, method: str, payload: dict[str, Any]) -> bool:
        with self.lock:
            self.calls.append((method, payload))
        return True

    def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None) -> bool:
        return self._record(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            },
        )

    def answer_callback_query(self, callback_query_id, text=None) -> bool:
        return self._record(
            "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}
        )

    # -- assertions helpers --

    @property
    def methods(self) -> list[str]:
        with self.lock:
            return [method for method, _ in self.calls]

    @property
    def sent_texts(self) -> list[str]:
        with self.lock:
            return [p["text"] for m, p in self.calls if m == "sendMessage"]

    @property
    def last_text(self) -> str:
        texts = self.sent_texts
        assert texts, "expected at least one sendMessage"
        return texts[-1]

    def reset(self) -> None:
        with self.lock:
            self.calls.clear()


class FakeClock:
    """Monotonic clock that only moves when something sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedSource:
    """Returns queued results, then repeats the final one forever."""

    def __init__(self, *results: CodeResult) -> None:
        self.results = list(results) or [CodeResult(status="not_found")]
        self.calls = 0

    def fetch(self) -> CodeResult:
        self.calls += 1
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


class BlockingSource:
    """Blocks inside fetch until released, so single-flight can be tested."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def fetch(self) -> CodeResult:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return CodeResult(status="ok", link="https://login.example.com/blocked", age_seconds=3)


def make_config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "bot_token": BOT_TOKEN,
        "webhook_secret": SECRET,
        "allowed_user_ids": frozenset({ALLOWED_USER_ID}),
        "code_source": "stub",
        "stub_mode": "ok",
        "stub_delay_seconds": 0.0,
    }
    values.update(overrides)
    return Config(**values)


class Harness:
    """An app wired to fakes, plus the pieces a test may want to inspect."""

    def __init__(self, app, telegram: FakeTelegram, bot: Bot, source, clock: FakeClock) -> None:
        self.app = app
        self.telegram = telegram
        self.bot = bot
        self.source = source
        self.clock = clock
        self.client = app.test_client()

    def post_update(self, update: dict[str, Any], *, secret: str | None = SECRET, client=None):
        headers = {}
        if secret is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        return (client or self.client).post("/webhook", json=update, headers=headers)

    def new_client(self):
        return self.app.test_client()


@pytest.fixture
def harness():
    """Factory fixture: harness(source=..., allowed=...) builds an app."""

    def build(
        source=None,
        *,
        allowed: set[int] | None = None,
        clock: FakeClock | None = None,
        real_clock: bool = False,
        deadline_seconds: float = 45.0,
        poll_interval: float = 3.0,
        cooldown_seconds: float = 30.0,
        config_overrides: dict[str, Any] | None = None,
    ) -> Harness:
        import time as _time

        clock = clock or FakeClock()
        source = source if source is not None else ScriptedSource(
            CodeResult(status="ok", link="https://login.example.com/verify?token=abc123", age_seconds=12)
        )
        telegram = FakeTelegram()
        monotonic = _time.monotonic if real_clock else clock.monotonic
        sleep = _time.sleep if real_clock else clock.sleep
        config = make_config(
            allowed_user_ids=frozenset(allowed or {ALLOWED_USER_ID}),
            **(config_overrides or {}),
        )
        bot = Bot(
            allowed_user_ids=config.allowed_user_ids,
            telegram=telegram,
            code_source=source,
            state=BotState(cooldown_seconds=cooldown_seconds, monotonic=monotonic),
            monotonic=monotonic,
            sleep=sleep,
            poll_interval=poll_interval,
            deadline_seconds=deadline_seconds,
        )
        app = create_app(config, bot=bot, configure_logs=False)
        app.config["TESTING"] = True
        return Harness(app, telegram, bot, source, clock)

    return build


_update_counter = iter(range(10_000, 99_999))


def next_update_id() -> int:
    return next(_update_counter)


def message_update(
    text: str = "/code",
    *,
    user_id: int = ALLOWED_USER_ID,
    update_id: int | None = None,
    chat_type: str = "private",
    chat_id: int = CHAT_ID,
) -> dict[str, Any]:
    return {
        "update_id": update_id if update_id is not None else next_update_id(),
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": False, "username": "tester"},
            "chat": {"id": chat_id, "type": chat_type},
            "date": 1700000000,
            "text": text,
        },
    }


def callback_update(
    data: str = "get_code",
    *,
    user_id: int = ALLOWED_USER_ID,
    update_id: int | None = None,
    callback_query_id: str = "cbq-1",
    chat_type: str = "private",
) -> dict[str, Any]:
    return {
        "update_id": update_id if update_id is not None else next_update_id(),
        "callback_query": {
            "id": callback_query_id,
            "from": {"id": user_id, "is_bot": False, "username": "tester"},
            "data": data,
            "message": {
                "message_id": 2,
                "chat": {"id": CHAT_ID, "type": chat_type},
                "date": 1700000000,
            },
        },
    }
