"""Where login links come from.

The mail-side script returns the login LINK out of the email, not a 6-digit
code: extracting the code from the linked page turned out to be unreliable, so
the user opens the link and reads the code themselves. Everything downstream
relays an opaque URL.

The Telegram half of the bot only ever sees this interface, so the whole flow
can be built and tested against StubCodeSource before the Apps Script web app
exists (spec 9).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

import requests

logger = logging.getLogger(__name__)

APPS_SCRIPT_TIMEOUT_SECONDS = 20.0
# Apps Script serves an HTML error page when the deployment is misconfigured;
# log a bounded slice of it so setup problems are diagnosable (spec 9).
BODY_LOG_CHARS = 200
# Keys the mail-side script may use for the link, in order of preference.
LINK_KEYS = ("link", "url", "code")


@dataclass
class CodeResult:
    """The outcome of one lookup. On success, link is the URL from the email."""

    status: Literal["ok", "not_found", "error"]
    link: str | None = None
    age_seconds: int | None = None
    detail: str | None = None


class CodeSource(Protocol):
    def fetch(self) -> CodeResult: ...


def _error(detail: str) -> CodeResult:
    return CodeResult(status="error", detail=detail)


def looks_like_url(value: str) -> bool:
    """Cheap shape check on what the mail-side script sent back.

    No length limit: a login URL can carry an arbitrarily long token, and the
    only real ceiling is what Telegram will accept in one message.
    """
    return value.startswith("https://") or value.startswith("http://")


def _coerce_age(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AppsScriptCodeSource:
    """Production source: an Apps Script web app in the mailbox owner account.

    The server holds only a URL and a shared secret, never a Gmail credential.
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        timeout: float = APPS_SCRIPT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self._url = url
        self._secret = secret
        self._timeout = timeout
        self._session = session or requests.Session()

    def fetch(self) -> CodeResult:
        try:
            # allow_redirects: /exec bounces to script.googleusercontent.com.
            response = self._session.post(
                self._url,
                json={"secret": self._secret},
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            # The exception text can embed the request URL, so log only the type.
            logger.error(
                "apps script request failed",
                extra={
                    "event": "code_source_request_error",
                    "error_type": type(exc).__name__,
                },
            )
            return _error("request failed: " + type(exc).__name__)

        body = response.text or ""
        if response.status_code != 200:
            logger.error(
                "apps script returned non-200",
                extra={
                    "event": "code_source_http_error",
                    "status_code": response.status_code,
                    "body_prefix": body[:BODY_LOG_CHARS],
                },
            )
            return _error("HTTP {0} from code source".format(response.status_code))

        try:
            payload = response.json()
        except ValueError:
            logger.error(
                "apps script returned a non-JSON body",
                extra={
                    "event": "code_source_non_json",
                    "content_type": response.headers.get("Content-Type", ""),
                    "body_prefix": body[:BODY_LOG_CHARS],
                },
            )
            return _error("code source returned a non-JSON response")

        if not isinstance(payload, dict):
            logger.error(
                "apps script returned an unexpected JSON shape",
                extra={
                    "event": "code_source_bad_shape",
                    "json_type": type(payload).__name__,
                },
            )
            return _error("code source returned an unexpected JSON shape")

        status = payload.get("status")
        if status == "ok":
            link = ""
            for key in LINK_KEYS:
                link = str(payload.get(key) or "").strip()
                if link:
                    break
            if not link:
                logger.error(
                    "apps script reported ok without a link",
                    extra={"event": "code_source_missing_link", "keys": sorted(payload)[:10]},
                )
                return _error("code source reported ok but sent no link")
            if not looks_like_url(link):
                # A bare 6-digit code landing here means the mail-side script is
                # still on the old contract, so say so rather than relaying it.
                logger.error(
                    "apps script returned something that is not a URL",
                    extra={"event": "code_source_not_a_url", "value_length": len(link)},
                )
                return _error("code source returned something that is not a link")
            # NB: the link itself is never logged; it is the credential here.
            return CodeResult(
                status="ok",
                link=link,
                age_seconds=_coerce_age(payload.get("age_seconds")),
            )

        if status == "not_found":
            return CodeResult(status="not_found")

        if status == "error":
            detail = str(payload.get("detail") or "unspecified error")[:BODY_LOG_CHARS]
            logger.error(
                "apps script reported an error",
                extra={"event": "code_source_reported_error", "detail": detail},
            )
            return _error(detail)

        logger.error(
            "apps script returned an unknown status",
            extra={"event": "code_source_unknown_status", "status": str(status)[:64]},
        )
        return _error("unknown status from code source")


class StubCodeSource:
    """Development source, selected with CODE_SOURCE=stub.

    Modes:
      ok         - sleep briefly, then return a fixed link
      not_found  - always empty-handed, so the timeout path can be exercised
      error      - always fails, for the error path
      delayed    - not_found until delay_seconds have passed since the first
                   fetch, then ok; exercises the polling loop
    """

    def __init__(
        self,
        *,
        mode: str = "ok",
        link: str = "https://example.invalid/login/confirm?token=stub",
        delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mode = mode
        self._link = link
        self._delay = delay_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._first_fetch_at: float | None = None

    def fetch(self) -> CodeResult:
        now = self._monotonic()
        if self._first_fetch_at is None:
            self._first_fetch_at = now

        if self._mode == "delayed":
            elapsed = now - self._first_fetch_at
            if elapsed < self._delay:
                return CodeResult(status="not_found")
            return CodeResult(status="ok", link=self._link, age_seconds=int(elapsed))

        if self._delay > 0:
            self._sleep(self._delay)

        if self._mode == "not_found":
            return CodeResult(status="not_found")
        if self._mode == "error":
            return _error("stub source configured to fail")
        return CodeResult(status="ok", link=self._link, age_seconds=12)


def build_code_source(config, *, session: requests.Session | None = None) -> CodeSource:
    """Pick the source named by CODE_SOURCE."""
    if config.code_source == "stub":
        logger.warning(
            "using the stub code source; no real login codes will be fetched",
            extra={"event": "stub_source_selected", "stub_mode": config.stub_mode},
        )
        return StubCodeSource(
            mode=config.stub_mode,
            link=config.stub_link,
            delay_seconds=config.stub_delay_seconds,
        )
    return AppsScriptCodeSource(
        url=config.apps_script_url or "",
        secret=config.apps_script_secret or "",
        session=session,
    )
