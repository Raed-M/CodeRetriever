"""Environment-driven configuration, validated once at startup.

Every secret comes from an environment variable. There are no defaults for
secrets and no dotenv loading: if a required variable is missing the process
refuses to start (spec 5.5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_PORT = 8080
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"

VALID_CODE_SOURCES = ("appsscript", "stub")
VALID_STUB_MODES = ("ok", "not_found", "error", "delayed")


class ConfigError(RuntimeError):
    """Raised at startup when the environment is not usable."""


@dataclass(frozen=True, repr=False)
class Config:
    """Immutable runtime configuration.

    ``repr`` is overridden so that a stray ``print(config)`` or a logging call
    that formats the object cannot leak the bot token or the shared secrets.
    """

    bot_token: str
    webhook_secret: str
    allowed_user_ids: frozenset[int]
    code_source: str = "appsscript"
    apps_script_url: str | None = None
    apps_script_secret: str | None = None
    port: int = DEFAULT_PORT
    telegram_api_base: str = DEFAULT_TELEGRAM_API_BASE
    # Stub-only knobs; ignored when code_source == "appsscript".
    stub_mode: str = "ok"
    stub_link: str = "https://example.invalid/login/confirm?token=stub"
    stub_delay_seconds: float = 1.0

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            "Config(code_source={0.code_source!r}, allowed_user_ids={1} ids, "
            "port={0.port}, stub_mode={0.stub_mode!r}, secrets=<redacted>)"
        ).format(self, len(self.allowed_user_ids))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        missing: list[str] = []
        problems: list[str] = []

        def required(name: str) -> str:
            value = (env.get(name) or "").strip()
            if not value:
                missing.append(name)
            return value

        bot_token = required("BOT_TOKEN")
        webhook_secret = required("WEBHOOK_SECRET")
        raw_ids = required("ALLOWED_USER_IDS")

        allowed_user_ids: frozenset[int] = frozenset()
        if raw_ids:
            ids: set[int] = set()
            for chunk in raw_ids.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    ids.add(int(chunk))
                except ValueError:
                    problems.append(
                        f"ALLOWED_USER_IDS contains a non-integer entry: {chunk!r}"
                    )
            if not ids and not problems:
                problems.append("ALLOWED_USER_IDS is empty after parsing")
            allowed_user_ids = frozenset(ids)

        code_source = (env.get("CODE_SOURCE") or "appsscript").strip().lower()
        if code_source not in VALID_CODE_SOURCES:
            problems.append(
                f"CODE_SOURCE must be one of {VALID_CODE_SOURCES}, got {code_source!r}"
            )

        apps_script_url = (env.get("APPS_SCRIPT_URL") or "").strip() or None
        apps_script_secret = (env.get("APPS_SCRIPT_SECRET") or "").strip() or None
        if code_source == "appsscript":
            if not apps_script_url:
                missing.append("APPS_SCRIPT_URL")
            elif not apps_script_url.startswith("https://"):
                problems.append("APPS_SCRIPT_URL must be an https:// URL")
            if not apps_script_secret:
                missing.append("APPS_SCRIPT_SECRET")

        port = DEFAULT_PORT
        raw_port = (env.get("PORT") or "").strip()
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                problems.append(f"PORT must be an integer, got {raw_port!r}")

        telegram_api_base = (
            env.get("TELEGRAM_API_BASE") or DEFAULT_TELEGRAM_API_BASE
        ).strip().rstrip("/")

        stub_mode = (env.get("STUB_MODE") or "ok").strip().lower()
        if stub_mode not in VALID_STUB_MODES:
            problems.append(
                f"STUB_MODE must be one of {VALID_STUB_MODES}, got {stub_mode!r}"
            )
        stub_link = (
            env.get("STUB_LINK") or "https://example.invalid/login/confirm?token=stub"
        ).strip()
        stub_delay_seconds = 1.0
        raw_delay = (env.get("STUB_DELAY_SECONDS") or "").strip()
        if raw_delay:
            try:
                stub_delay_seconds = float(raw_delay)
            except ValueError:
                problems.append(
                    f"STUB_DELAY_SECONDS must be a number, got {raw_delay!r}"
                )

        if missing or problems:
            details = []
            if missing:
                details.append(
                    "missing required environment variables: " + ", ".join(sorted(set(missing)))
                )
            details.extend(problems)
            raise ConfigError("; ".join(details))

        return cls(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            allowed_user_ids=allowed_user_ids,
            code_source=code_source,
            apps_script_url=apps_script_url,
            apps_script_secret=apps_script_secret,
            port=port,
            telegram_api_base=telegram_api_base,
            stub_mode=stub_mode,
            stub_link=stub_link,
            stub_delay_seconds=stub_delay_seconds,
        )
