"""Single-line JSON logging to stdout, shaped for Cloud Logging.

Cloud Run reads stdout line by line; a JSON object with a "severity" field is
parsed into a structured entry instead of a blob of text (spec 11).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

# Attributes present on every LogRecord; anything else was passed via extra=.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Formats records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        # default=str keeps a stray non-serialisable extra from killing the log
        # line; ensure_ascii=False keeps unicode readable in Cloud Logging.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # The Werkzeug dev-server request log is noise next to the Cloud Run
    # request log; keep warnings and above only.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
