"""Development-only bridge: real Telegram, locally running service.

Cloud Run gets its updates pushed to it over HTTPS. A laptop has no public URL,
so Telegram can never reach it and running main.py on its own will sit there
receiving nothing. This stands in for that hop: it long-polls getUpdates and
POSTs each update to the local /webhook with the same secret header Telegram
would send. The service replies through the real Bot API, so everything except
Telegram inbound HTTP delivery is exercised for real.

    python tools/local_relay.py

It reads .env the same way the service does, starts main.py itself, and honours
whatever CODE_SOURCE is set to. Add --stub to force the stub source instead, or
--no-start-app to bridge to a service you started yourself.

Some networks answer NXDOMAIN for api.telegram.org while the route is open; the
address is pinned automatically when that happens, for this process and the
service subprocess only, leaving SNI, the Host header and certificate checks on
the real hostname.

Not part of the service. Excluded from the image and the Cloud Build upload.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_IPS = ("149.154.167.220", "149.154.166.110", "149.154.175.50")


def log(event: str, **fields) -> None:
    print(json.dumps({"relay": event, **fields}, default=str), flush=True)


ACTIVE_DNS_OVERRIDE: dict[str, str] = {}


def install_dns_override(ip: str) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def patched(host, port, *args, **kwargs):
        return real_getaddrinfo(ip if host == "api.telegram.org" else host, port, *args, **kwargs)

    socket.getaddrinfo = patched
    ACTIVE_DNS_OVERRIDE["ip"] = ip
    log("dns_override", host="api.telegram.org", ip=ip)


def dns_override_pythonpath(ip: str) -> str:
    """Carry the override into the service subprocess.

    Python imports sitecustomize at interpreter startup, so a throwaway one on
    PYTHONPATH patches the child before main.py runs. The service source stays
    unaware of any of this.
    """
    directory = Path(tempfile.mkdtemp(prefix="relay-dns-"))
    body = [
        "import socket",
        "_real = socket.getaddrinfo",
        "def _patched(host, port, *a, **k):",
        "    host = {0!r} if host == 'api.telegram.org' else host".format(ip),
        "    return _real(host, port, *a, **k)",
        "socket.getaddrinfo = _patched",
    ]
    (directory / "sitecustomize.py").write_text(chr(10).join(body), encoding="utf-8")
    return str(directory)


def pick_reachable_ip() -> str:
    for ip in TELEGRAM_IPS:
        sock = socket.socket()
        sock.settimeout(4)
        try:
            sock.connect((ip, 443))
            return ip
        except OSError:
            continue
        finally:
            sock.close()
    raise SystemExit("no Telegram front-end is reachable from this network")


class Relay:
    def __init__(self, token: str, secret: str, app_url: str) -> None:
        self._api = "https://api.telegram.org/bot{0}/".format(token)
        self._secret = secret
        self._app_url = app_url.rstrip("/")
        self._session = requests.Session()
        self._offset: int | None = None
        self._threads: list[threading.Thread] = []

    def api(self, method: str, payload: dict | None = None, timeout: float = 40.0):
        response = self._session.post(self._api + method, json=payload or {}, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def poll_once(self) -> list[dict]:
        payload = {
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            payload["offset"] = self._offset
        result = self.api("getUpdates", payload).get("result", [])
        for update in result:
            self._offset = update["update_id"] + 1
        return result

    def deliver(self, update: dict) -> None:
        """POST the update to the local service, the way Telegram would.

        In a thread: a /code lookup holds the request for up to 45 seconds and
        the relay has to keep polling meanwhile, or a button press during a
        lookup would not arrive until it finished.
        """

        def send() -> None:
            started = time.monotonic()
            try:
                response = self._session.post(
                    self._app_url + "/webhook",
                    json=update,
                    headers={"X-Telegram-Bot-Api-Secret-Token": self._secret},
                    timeout=120,
                )
                log(
                    "delivered",
                    update_id=update.get("update_id"),
                    status=response.status_code,
                    seconds=round(time.monotonic() - started, 1),
                )
            except requests.RequestException as exc:
                log("delivery_failed", update_id=update.get("update_id"), error=type(exc).__name__)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        self._threads.append(thread)


def sender_of(update: dict) -> tuple[int | None, str | None]:
    source = update.get("message") or update.get("callback_query") or {}
    sender = source.get("from") or {}
    return sender.get("id"), sender.get("username")


def start_app(env_extra: dict[str, str], port: int) -> subprocess.Popen:
    env = dict(os.environ, **env_extra)
    env["PORT"] = str(port)
    if ACTIVE_DNS_OVERRIDE:
        path = dns_override_pythonpath(ACTIVE_DNS_OVERRIDE["ip"])
        env["PYTHONPATH"] = path + os.pathsep + env.get("PYTHONPATH", "")
        log("app_dns_override", pythonpath=path)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def pump() -> None:
        for line in process.stdout:
            print("app | " + line.rstrip(), flush=True)

    threading.Thread(target=pump, daemon=True).start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if requests.get("http://127.0.0.1:{0}/healthz".format(port), timeout=2).text == "ok":
                log("app_ready", port=port)
                return process
        except requests.RequestException:
            time.sleep(0.4)
        if process.poll() is not None:
            raise SystemExit("the service exited during startup")
    raise SystemExit("the service did not become healthy in time")


def telegram_resolves() -> bool:
    try:
        socket.getaddrinfo("api.telegram.org", 443)
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument(
        "--no-start-app",
        action="store_true",
        help="relay only; assume main.py is already running on --port",
    )
    parser.add_argument(
        "--dns-override",
        metavar="IP",
        nargs="?",
        const="auto",
        help="force a Telegram address; by default this is applied only when DNS fails",
    )
    parser.add_argument("--no-dns-override", action="store_true")
    parser.add_argument("--allow", type=int, action="append", help="whitelist this user ID")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="run the service against the stub instead of whatever CODE_SOURCE says",
    )
    parser.add_argument("--stub-mode", default="delayed")
    parser.add_argument("--stub-link", default="https://example.invalid/login/confirm?token=stub")
    parser.add_argument("--stub-delay", default="8")
    parser.add_argument(
        "--seconds",
        type=int,
        default=0,
        help="stop after this many seconds; 0 (the default) runs until Ctrl+C",
    )
    args = parser.parse_args()
    args.start_app = not args.no_start_app

    # Same precedence the service uses: real environment first, .env behind it.
    sys.path.insert(0, str(ROOT))
    from codebot.config import load_env_file

    file_values = load_env_file()
    settings = {**file_values, **os.environ}

    token = (settings.get("BOT_TOKEN") or "").strip()
    secret = (settings.get("WEBHOOK_SECRET") or "").strip()
    if not token or not secret:
        raise SystemExit("BOT_TOKEN and WEBHOOK_SECRET must be set, in .env or the environment")
    # The service subprocess inherits these, so it never has to re-read the file.
    os.environ.setdefault("BOT_TOKEN", token)
    os.environ.setdefault("WEBHOOK_SECRET", secret)

    if not args.allow:
        raw = (settings.get("ALLOWED_USER_IDS") or "").strip()
        args.allow = [int(x) for x in raw.split(",") if x.strip()] or None

    if args.no_dns_override:
        pass
    elif args.dns_override:
        install_dns_override(
            pick_reachable_ip() if args.dns_override == "auto" else args.dns_override
        )
    elif not telegram_resolves():
        # This network answers NXDOMAIN for api.telegram.org even though the
        # route is open, so fall back to a known address rather than failing.
        log("dns_lookup_failed", host="api.telegram.org")
        install_dns_override(pick_reachable_ip())

    relay = Relay(token, secret, "http://127.0.0.1:{0}".format(args.port))
    me = relay.api("getMe").get("result", {})
    log("bot", username=me.get("username"), id=me.get("id"))
    relay.api("deleteWebhook", {"drop_pending_updates": False})
    return run(relay, args)


def app_env(args, allowed: list[int]) -> dict[str, str]:
    """Environment for the service subprocess.

    Only the whitelist is forced, so the service reads CODE_SOURCE and
    everything else from .env exactly as it would on its own. --stub overrides
    that for a run that must not touch the mailbox.
    """
    env = {"ALLOWED_USER_IDS": ",".join(str(i) for i in allowed)}
    if args.stub:
        env.update(
            {
                "CODE_SOURCE": "stub",
                "STUB_MODE": args.stub_mode,
                "STUB_LINK": args.stub_link,
                "STUB_DELAY_SECONDS": args.stub_delay,
            }
        )
    return env


def run(relay: Relay, args) -> int:
    allowed = list(args.allow or [])
    app_process = None
    pending: list[dict] = []

    if allowed and args.start_app:
        app_process = start_app(app_env(args, allowed), args.port)

    log(
        "waiting_for_messages",
        allowed=allowed or "first sender will be whitelisted",
        stop_with="Ctrl+C" if not args.seconds else "{0}s limit".format(args.seconds),
    )
    deadline = time.monotonic() + args.seconds if args.seconds else float("inf")
    try:
        while time.monotonic() < deadline:
            for update in relay.poll_once():
                user_id, username = sender_of(update)
                log(
                    "received",
                    update_id=update.get("update_id"),
                    user_id=user_id,
                    username=username,
                    kind="callback_query" if "callback_query" in update else "message",
                    text=(update.get("message") or {}).get("text"),
                    callback_data=(update.get("callback_query") or {}).get("data"),
                )

                # First sender seen becomes the whitelist, so a live test needs
                # no user ID looked up in advance.
                if app_process is None and args.start_app and user_id is not None:
                    allowed = [user_id]
                    app_process = start_app(app_env(args, allowed), args.port)
                    for buffered in pending:
                        relay.deliver(buffered)
                    pending.clear()

                if app_process is None and args.start_app:
                    pending.append(update)
                    continue
                relay.deliver(update)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        for thread in relay._threads:
            thread.join(timeout=60)
        if app_process is not None:
            app_process.terminate()
            log("app_stopped")
    log("finished", whitelisted=allowed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
