# Build Spec: Telegram Login-Code Relay Bot

## 1. Purpose

A small group shares one account on a third-party website. Logging in requires a
one-time code emailed to a single Gmail account. Today, nobody can log in unless
the Gmail owner is awake and available.

This bot removes that dependency. An authorised user asks the bot for the code
over Telegram; the bot retrieves it and replies. That is the entire scope.

## 2. Architecture

```
Telegram user
    |  (taps /code)
    v
Telegram servers
    |  HTTPS POST (webhook)
    v
Cloud Run service  <-- THIS IS WHAT YOU ARE BUILDING
    |  HTTPS POST (shared secret)
    v
Google Apps Script Web App   (runs in the Gmail owner's account)
    |  reads labelled email, follows link, extracts code
    v
returns { "status": "ok", "code": "123456" }
```

**Why the Apps Script hop exists:** so that no Gmail credential (app password,
OAuth token) is ever stored on the server. The server holds only a URL and a
shared secret. If the container is compromised, the attacker can request login
codes but cannot read the mailbox or send mail.

The Apps Script side is **out of scope for this build** — see §9 for the contract
it must satisfy and the stub you should build against.

## 3. Runtime and stack

- Python 3.12
- Flask + gunicorn (simplest reliable Cloud Run combination; do not use an ASGI
  server unless you have a reason)
- `requests` for outbound HTTP
- No database, no Redis, no persistent storage. All state is in-process and
  intentionally ephemeral.
- No `python-telegram-bot` library. It is built around a polling/application
  lifecycle that fights the request-response model here. Call the Bot API
  directly over HTTPS — there are only three methods needed.

Deliverables: `main.py` (or a small package), `requirements.txt`, `Dockerfile`,
`.gitignore`, `.dockerignore`, `README.md` with local-run instructions.

## 4. HTTP endpoints

### `POST /webhook`

The only functional endpoint. Receives Telegram `Update` objects.

**Critical rule: always return HTTP 200, with an empty body, for every request
that passes the secret check — including unauthorised users, malformed payloads,
and internal errors.** A non-2xx response makes Telegram redeliver the same
update, which causes duplicate work and can consume a second login code. Handle
errors by replying to the user with a message, not by returning an error status.

The single exception: if the `X-Telegram-Bot-Api-Secret-Token` header is missing
or does not match `WEBHOOK_SECRET`, return **403** with no body and log the
attempt. That request did not come from Telegram.

### `GET /healthz`

Returns 200 and the string `ok`. For manual checks only.

## 5. Security requirements

These are not optional. This bot relays authentication codes.

1. **Verify the secret header on every request.** Constant-time comparison
   (`hmac.compare_digest`).
2. **Whitelist by Telegram user ID.** Read `ALLOWED_USER_IDS` (comma-separated
   integers) at startup into a `frozenset[int]`. Check
   `update.message.from.id` or `update.callback_query.from.id`.
3. **Unauthorised users get no reply at all.** Do not send "you are not
   authorised". Log the attempt (ID, username, timestamp) and return 200
   silently. A silent bot gives a prober no confirmation it exists.
4. **Never log the retrieved code, the bot token, or the Apps Script secret.**
   Log that a code was delivered, to whom, and when — not its value.
5. Read all secrets from environment variables. No secrets in source, no
   defaults, no `.env` file committed. Fail fast at startup with a clear error if
   a required variable is missing.

## 6. Behaviour

### `/start` and `/help`

Reply with a short usage message: what the bot does, that access is restricted,
and that `/code` fetches the latest login code. Mention that the login request
must be initiated on the website *before* asking for the code — otherwise there
is no email to find.

### `/code` (command) and `callback_data == "get_code"` (inline button)

Both paths run the same handler. Sequence:

1. If the update is a `callback_query`, call `answerCallbackQuery` with its `id`
   **immediately, before any slow work**. Otherwise the button spins on the
   user's screen for the whole lookup.
2. Check the whitelist. Not on it → log, return 200, send nothing.
3. Check the dedup set (§7). Already seen `update_id` → return 200, do nothing.
4. Check the per-user cooldown (§7). Too soon → reply asking them to wait.
5. Try to acquire the single-flight lock (§7). Busy → reply that another request
   is in progress and to try again shortly.
6. Send an interim message: something like "Looking for your code…". This
   matters — the lookup can take 45 seconds and silence reads as breakage.
7. Poll the code source (§9) every 3 seconds, up to a 45-second deadline.
8. On success: reply with the code. Format it so it is easy to copy on mobile —
   put it in a code block on its own line. Include how many seconds old the
   email was.
9. On timeout: reply that no recent code was found, and suggest they confirm the
   login request was submitted on the website and try again.
10. On error from the code source: reply that the lookup failed and to try again;
    log the full error server-side.
11. Release the lock in a `finally` block. It must never leak.

### Any other message

Reply with a one-line hint pointing at `/code`. Whitelist check still applies
first — non-whitelisted users get silence.

### Inline button

Attach an inline keyboard with a single button (text: "Get login code",
`callback_data`: `get_code`) to the `/start` reply and to any failure reply, so
retrying is one tap.

## 7. Concurrency and correctness

The service runs with `--max-instances=1`, so in-process state is reliable.
**Do not design around multiple instances** — but do make the code correct under
concurrent requests within one instance, since gunicorn will use multiple
threads.

- **Dedup:** keep the last 200 `update_id` values in a `collections.deque` plus a
  `set` for lookup. Check-and-insert must be under a lock. Telegram guarantees
  `update_id` is unique per update.
- **Single-flight lock:** a module-level `threading.Lock`. Only one code lookup
  may run at a time. Two friends requesting simultaneously could otherwise
  consume each other's codes with no way to tell which is which. Acquire
  non-blocking; if unavailable, tell the second user to wait.
- **Per-user cooldown:** 30 seconds between `/code` requests from the same user
  ID. A dict of `user_id -> last_request_monotonic`.
- Use `time.monotonic()` for all elapsed-time logic, never `time.time()`.

## 8. Timing

- Poll interval: 3 seconds.
- Total deadline: **45 seconds**, measured from the start of the handler.
- Cloud Run request timeout will be set to 120s, so 45s leaves ample margin.
- The deadline exists because Telegram redelivers updates that do not get a
  timely 2xx. Returning well inside that window, combined with dedup, prevents
  duplicate code consumption.

Make the interval and deadline module-level constants, not magic numbers.

## 9. Code source interface

Define an abstract interface so the Telegram half can be built and tested before
the Apps Script side exists.

```python
class CodeSource(Protocol):
    def fetch(self) -> CodeResult: ...

@dataclass
class CodeResult:
    status: Literal["ok", "not_found", "error"]
    code: str | None = None
    age_seconds: int | None = None
    detail: str | None = None
```

### `AppsScriptCodeSource` (production)

- `POST` to `APPS_SCRIPT_URL`
- JSON body: `{"secret": "<APPS_SCRIPT_SECRET>"}`
- Timeout: 20 seconds per call
- Follow redirects — Apps Script web apps redirect to `script.googleusercontent.com`
- Expected 200 response:
  - `{"status": "ok", "code": "123456", "age_seconds": 12}`
  - `{"status": "not_found"}`
  - `{"status": "error", "detail": "..."}`
- Treat a non-JSON response as an error and log the first 200 characters of the
  body. Apps Script returns an HTML error page when the deployment is
  misconfigured, and this is the single most likely failure during setup.

### `StubCodeSource` (development)

Returns a fixed code after a short simulated delay. Selected when
`CODE_SOURCE=stub`. Include a mode that returns `not_found` so the timeout path
can be tested. **Build and test the entire Telegram flow against this stub
first** — it is the current priority.

## 10. Configuration

All via environment variables. Validate at startup.

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | From BotFather |
| `WEBHOOK_SECRET` | yes | Random string, matches `setWebhook` |
| `ALLOWED_USER_IDS` | yes | Comma-separated integers |
| `CODE_SOURCE` | no | `appsscript` (default) or `stub` |
| `APPS_SCRIPT_URL` | if not stub | The `/exec` URL |
| `APPS_SCRIPT_SECRET` | if not stub | Shared secret |
| `PORT` | no | Injected by Cloud Run; default 8080 |

## 11. Cloud Run specifics

- **Bind to `0.0.0.0`, port from `PORT`.** Binding to `127.0.0.1` produces a
  container that works locally and silently fails to serve on Cloud Run.
- Do not use background threads for work that must complete after the response
  is returned. Cloud Run throttles CPU once the response is sent. Everything is
  synchronous within the request.
- Log to stdout as single-line JSON where practical; Cloud Logging parses it.
- Dockerfile: `python:3.12-slim` base, non-root user, `gunicorn` with
  `--threads 4 --timeout 120 --workers 1`. One worker matters — in-process state
  is not shared between worker processes.

Reference deploy command (region `me-central2`, Dammam):

```bash
gcloud run deploy telegram-code-bot \
  --source . \
  --region me-central2 \
  --allow-unauthenticated \
  --max-instances=1 \
  --min-instances=0 \
  --memory=512Mi \
  --timeout=120
```

`--allow-unauthenticated` is required: Telegram cannot present a Google IAM
token. The secret-token header is the actual access control.

## 12. Telegram API methods used

Only three. Call them directly.

- `sendMessage` — `chat_id`, `text`, optional `reply_markup`, `parse_mode`
- `answerCallbackQuery` — `callback_query_id`
- (setup only, run manually) `setWebhook`, `getWebhookInfo`

Base URL: `https://api.telegram.org/bot{BOT_TOKEN}/{method}`

Give outbound calls a 10-second timeout. If `sendMessage` fails, log it and
still return 200 — retrying the whole update would be worse.

Include in the README the exact `setWebhook` command, with
`allowed_updates=["message","callback_query"]`. **Omitting `callback_query`
means button presses never arrive.**

## 13. Testing

Include `pytest` tests covering, at minimum:

- Missing/wrong secret header → 403
- Non-whitelisted user → 200, no outbound `sendMessage`
- Duplicate `update_id` → processed once
- Second concurrent `/code` → gets the busy message
- Stub source returning `not_found` → user gets the timeout message
- Cooldown enforced

Mock all outbound HTTP. No test should touch the network.

## 14. Explicit non-goals

Do not build: a database, user self-registration, admin commands, multi-account
support, group chat support, message history, an inline query handler, i18n, or
a web dashboard. If a requirement seems to call for one of these, it is out of
scope — leave a comment and move on.

## 15. Priority order

1. `/webhook` skeleton with secret verification and whitelist
2. `/start`, `/help`, unknown-message handling
3. `StubCodeSource` and the full `/code` flow against it
4. Dedup, lock, cooldown
5. Dockerfile, README, deploy verification on Cloud Run
6. `AppsScriptCodeSource` (last — the Apps Script side does not exist yet)

Steps 1–5 should be fully working and deployed before step 6 is attempted.
