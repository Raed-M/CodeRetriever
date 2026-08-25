# Telegram login-link relay bot

A small Cloud Run service that hands a shared account's one-time login link to a
short list of Telegram users, so nobody has to wait for the mailbox owner to be
awake. The recipient opens the link and reads the code off the page.

```
Telegram user  --/code-->  Telegram servers  --webhook-->  Cloud Run (this repo)
                                                                |
                                                 POST + shared secret
                                                                v
                                            Google Apps Script web app
                                            (reads the labelled email,
                                             extracts the code)
```

The Apps Script hop keeps every Gmail credential out of this service. It holds a
URL and a shared secret, nothing else: someone who compromises the container can
ask for login codes, but cannot read or send mail.

## What it does

| Input | Behaviour |
|---|---|
| `/start`, `/help` | usage message plus a **Get login code** button |
| `/code` or the button | polls the code source every 3s for up to 45s, then replies with the login link as a tappable URL |
| anything else | one-line hint pointing at `/code` |
| a user not on the whitelist | nothing at all, silently logged |

Guardrails: one lookup at a time process-wide, a 30s per-user cooldown, and the
last 200 update_id values remembered so a Telegram redelivery cannot burn a
second link.

## Layout

```
main.py                  gunicorn entrypoint (main:app), fails fast on bad config
codebot/config.py        environment parsing and validation
codebot/app.py           Flask factory, POST /webhook and GET /healthz
codebot/bot.py           parse, authorise, route, poll, reply
codebot/code_source.py   CodeSource protocol, AppsScriptCodeSource, StubCodeSource
codebot/state.py         dedup, single-flight lock, per-user cooldown
codebot/telegram_api.py  the three Bot API calls this needs
codebot/messages.py      user-facing copy
tests/                   86 tests, all outbound HTTP mocked
```

## Configuration

Everything comes from environment variables and is validated at startup; a
missing or malformed value stops the process with a message naming it.

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | from BotFather |
| `WEBHOOK_SECRET` | yes | random string, must match the secret_token given to setWebhook |
| `ALLOWED_USER_IDS` | yes | comma-separated Telegram user IDs |
| `CODE_SOURCE` | no | `appsscript` (default) or `stub` |
| `APPS_SCRIPT_URL` | if not stub | the /exec URL |
| `APPS_SCRIPT_SECRET` | if not stub | shared secret |
| `PORT` | no | injected by Cloud Run, defaults to 8080 |
| `STUB_MODE` | no | stub only: ok, not_found, error, delayed |
| `STUB_LINK`, `STUB_DELAY_SECONDS` | no | stub only |
| `TELEGRAM_API_BASE` | no | test hook, points the Bot API elsewhere; leave unset in production |

Find a Telegram user ID by having that person message @userinfobot.

Generate a webhook secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Running it locally

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt    # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt # macOS, Linux

cp .env.example .env      # then fill it in; .env is gitignored
```

Load the variables and start the dev server:

```bash
# bash
set -a; . ./.env; set +a
python main.py
```

```powershell
# PowerShell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim())
  }
}
python main.py
```

`python main.py` runs the Flask development server: threaded, bound to 0.0.0.0.
Cloud Run runs gunicorn instead (see the Dockerfile). gunicorn does not run on
Windows, so local runs use the dev server.

### Poking it without Telegram

With `CODE_SOURCE=stub` the whole flow works offline. Health check:

```bash
curl -s http://localhost:8080/healthz     # -> ok
```

A synthetic /code update (replace YOUR_ID with a whitelisted user ID):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://localhost:8080/webhook \
  -H 'Content-Type: application/json' \
  -H "X-Telegram-Bot-Api-Secret-Token: $WEBHOOK_SECRET" \
  -d '{"update_id":1,"message":{"message_id":1,"date":1700000000,
       "from":{"id":YOUR_ID,"is_bot":false,"username":"you"},
       "chat":{"id":YOUR_ID,"type":"private"},"text":"/code"}}'
```

That returns 200 and the bot messages you through the real Bot API. Drop the
secret header and the same request returns 403. Point `TELEGRAM_API_BASE` at a
local server to capture the outgoing calls instead of sending them to Telegram.

`STUB_MODE=not_found` exercises the 45 second timeout path; `STUB_MODE=delayed`
with `STUB_DELAY_SECONDS=10` exercises the polling loop.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

86 tests, no network: the Bot API, the Apps Script call and the clock are all
substituted. They cover the 403 path, the silent-whitelist path, dedup, the
cooldown, single-flight under real threads, the timeout path, HTML escaping, and
the guarantee that no secret reaches the logs.

## Deploying to Cloud Run

Store the secrets first:

```bash
printf '%s' 'YOUR_BOT_TOKEN'   | gcloud secrets create bot-token        --data-file=-
printf '%s' 'YOUR_SECRET'      | gcloud secrets create webhook-secret   --data-file=-
printf '%s' '111111,222222'    | gcloud secrets create allowed-user-ids --data-file=-
```

Then deploy:

```bash
gcloud run deploy telegram-code-bot \
  --source . \
  --region me-central2 \
  --allow-unauthenticated \
  --max-instances=1 \
  --min-instances=0 \
  --memory=512Mi \
  --timeout=120 \
  --set-env-vars "CODE_SOURCE=stub,STUB_MODE=ok" \
  --set-secrets "BOT_TOKEN=bot-token:latest,WEBHOOK_SECRET=webhook-secret:latest,ALLOWED_USER_IDS=allowed-user-ids:latest"
```

`--allow-unauthenticated` is required: Telegram cannot present a Google IAM
token. The secret-token header is the real access control, with the user
whitelist behind it.

`--max-instances=1` is not a cost setting. The dedup set, the single-flight lock
and the cooldown map live in process memory; a second instance would have its
own copy of all three and could hand out two codes at once. The Dockerfile runs
one gunicorn worker with four threads for the same reason.

`--set-env-vars` on a later deploy replaces the whole set, so keep every plain
variable in one flag, or use `--update-env-vars`.

`.gcloudignore` keeps `.env` and `.venv` out of the source upload; without it
`--source .` would push the local bot token into the Cloud Build staging bucket.
`.dockerignore` then keeps the tests and the spec out of the image itself.

## Pointing Telegram at it

Register the webhook once the service is live. **allowed_updates must include
callback_query, or button presses never arrive.**

```bash
SERVICE_URL="$(gcloud run services describe telegram-code-bot \
  --region me-central2 --format='value(status.url)')"

curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\": \"${SERVICE_URL}/webhook\",
       \"secret_token\": \"${WEBHOOK_SECRET}\",
       \"allowed_updates\": [\"message\", \"callback_query\"],
       \"drop_pending_updates\": true}"
```

Check it:

```bash
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

A climbing pending_update_count or a last_error_message means Telegram cannot
reach the service, or the secret does not match. To take the bot offline:

```bash
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook"
```

## Switching to the real mail source

Deploy with the stub first and confirm the Telegram half works end to end. Then:

```bash
gcloud run services update telegram-code-bot --region me-central2 \
  --update-env-vars CODE_SOURCE=appsscript \
  --update-secrets "APPS_SCRIPT_URL=apps-script-url:latest,APPS_SCRIPT_SECRET=apps-script-secret:latest"
```

The Apps Script web app must accept `POST {"secret": "..."}` and answer with one
of:

```json
{"status": "ok", "link": "https://third-party.example/login/confirm?t=...", "age_seconds": 12}
{"status": "not_found"}
{"status": "error", "detail": "what went wrong"}
```

The success payload carries the **login link from the email**, not a 6-digit
code: pulling the code off the linked page proved unreliable, so the recipient
opens the link themselves. The link field may equally be named `url` or `code`
(all three are read, in that order), and there is no length limit on it. A value
that is not an `http://` or `https://` URL is rejected as an error rather than
relayed, which is what a script still on the old code-returning contract would
produce.

Deploy it as **Execute as: me**, **Who has access: anyone**. Anything else
serves an HTML sign-in page instead of JSON; the service treats that as an error
and logs the first 200 characters of the page, which is usually enough to spot
the misconfiguration. Check a deployment before wiring it up:

```bash
curl -s -L -X POST -H 'Content-Type: application/json'   -d '{"secret":"YOUR_SECRET"}' "$APPS_SCRIPT_URL" | head -c 300
```

JSON back means it is ready. HTML back, or HTTP 401, means the access setting is
still wrong.

## Operating it

Logs are single-line JSON, so Cloud Logging can be queried by field:

```bash
gcloud run services logs read telegram-code-bot --region me-central2 --limit 50
```

Useful event values: link_delivered, code_not_found, code_lookup_error,
unauthorised, bad_secret_token, duplicate_update, single_flight_busy,
cooldown_block.

The login link itself, the bot token and the Apps Script secret are never
logged: the link is the credential here. The
token is scrubbed out of exception text too, since requests puts the full
request URL into its error messages.

## Deliberately not built

No database, no self-registration, no admin commands, no group support, no
multi-account support, no dashboard. State is in memory and is meant to be lost
on restart: that costs at most one forgotten cooldown window.
