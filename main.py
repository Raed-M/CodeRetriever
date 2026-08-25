"""Cloud Run entrypoint.

Production:  gunicorn --workers 1 --threads 4 --timeout 120 main:app
Local dev:   python main.py
"""

from __future__ import annotations

import logging
import sys

from codebot.app import create_app
from codebot.config import Config, ConfigError
from codebot.logging_setup import configure_logging

try:
    app = create_app()
except ConfigError as exc:
    # Fail fast and loudly: a container that starts without its configuration
    # would just answer every request with an error (spec 5.5).
    configure_logging()
    logging.getLogger(__name__).critical(
        "startup aborted: invalid configuration",
        extra={"event": "config_error", "detail": str(exc)},
    )
    raise SystemExit("Configuration error: {0}".format(exc)) from exc


if __name__ == "__main__":
    # Development server only. Cloud Run runs gunicorn (see Dockerfile).
    # threaded=True mirrors the multi-threaded worker used in production.
    port = app.config["CONFIG"].port
    print("dev server on http://0.0.0.0:{0}".format(port), file=sys.stderr)
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
