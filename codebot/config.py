"""Environment-driven configuration, validated once at startup.

Every secret comes from the environment. There are no defaults for secrets: if a
required variable is missing the process refuses to start (spec 5.5).

For convenience during local development, a .env file sitting next to main.py is
read first. The real environment always wins over it, so a file that somehow
reached a server could not override a secret injected there. The .env is
gitignored and excluded from both the image and the Cloud Build upload, so in
production there is simply no file to read.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"

# Next to main.py, not the working directory, so "python main.py" behaves the
# same whichever directory it is launched from.
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

VALID_CODE_SOURCES = ("appsscript", "stub")
VALID_STUB_MODES = ("ok", "not_found", "error", "delayed")


class ConfigError(RuntimeError):
    """Raised at startup when the environment is not usable."""


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines.

    Blank lines and lines starting with # are skipped, a leading "export " is
    tolerated, and one layer of matching surrounding quotes is removed.

    A # part-way through a line is NOT treated as a comment: secrets are allowed
    to contain one, and truncating a secret silently would be far worse than
    keeping a stray trailing comment.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            # Line number only. The content may be a secret.
            logger.warning(
                "ignoring unparseable line in the env file",
                extra={"event": "env_file_bad_line", "line_number": number},
            )
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", chr(34)):
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Read a .env file if there is one. A missing file is not an error.

    ENV_FILE overrides the location; setting it empty disables the lookup.
    """
    if path is None:
        override = os.environ.get("ENV_FILE")
        if override is not None:
            if not override.strip():
                return {}
            path = Path(override)
        else:
            path = DEFAULT_ENV_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(
            "could not read the env file",
            extra={"event": "env_file_unreadable", "error_type": type(exc).__name__},
        )
        return {}
    values = parse_env_file(text)
    if values:
        # Names only, never values.
        logger.info(
            "loaded local env file",
            extra={"event": "env_file_loaded", "keys": sorted(values)},
        )
    return values


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
        """Build from the environment, or from an explicit mapping.

        Passing a mapping skips the .env file entirely, which keeps tests
        hermetic: a developer machine with a populated .env must not change what
        the tests see.
        """
        if env is None:
            # The real environment wins over the file.
            env = {**load_env_file(), **os.environ}
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
