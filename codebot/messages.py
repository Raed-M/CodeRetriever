"""User-facing copy, kept in one place.

All text is sent with parse_mode=HTML, so anything interpolated must go through
html.escape first. The copy is deliberately plain ASCII: it travels through
Telegram, Cloud Logging and a Windows shell during development, and there is
nothing to gain from characters that any of those might mangle.
"""

from __future__ import annotations

import html

START = (
    "<b>Login link relay</b>\n\n"
    "I pull the one-time login link out of the shared mailbox and send it to you, "
    "so nobody has to wait for the mailbox owner to be awake. You open the link "
    "and read the code off the page yourself.\n\n"
    "<b>How to use it</b>\n"
    "1. Start the login on the website first, so the email actually gets sent.\n"
    "2. Then send /code here, or tap the button below.\n"
    "3. Open the link I send back, and take the code from that page.\n\n"
    "Access is restricted to a fixed list of users. /help shows this message again."
)

HINT = "Send /code to fetch the latest login link."

INTERIM = "Looking for your login link..."

BUSY = (
    "Someone else is fetching a link right now. "
    "Wait a few seconds and try again, so the two requests do not cross."
)

TIMEOUT = (
    "No new login email turned up in the last {deadline} seconds.\n\n"
    "Check that the login request was actually submitted on the website, then try again."
)

LOOKUP_FAILED = (
    "The lookup failed, so I could not read the mailbox. "
    "Try again in a moment; if it keeps failing the mail-side script needs a look."
)


def cooldown(seconds_remaining: int) -> str:
    unit = "second" if seconds_remaining == 1 else "seconds"
    return (
        "You just asked for a link. Wait {0} more {1} before asking again."
    ).format(seconds_remaining, unit)


def timeout(deadline_seconds: int) -> str:
    return TIMEOUT.format(deadline=deadline_seconds)


def login_link_delivered(link: str, age_seconds: int | None) -> str:
    """The success message.

    The URL sits alone on its own line and unformatted, so Telegram turns it
    into something tappable. A code block would be copyable but not clickable,
    which is the wrong trade now that the payload is a link.
    """
    lines = ["Your login link:", "", html.escape(link), ""]
    if age_seconds is None:
        lines.append("Open it and read the code off the page.")
    elif age_seconds <= 1:
        lines.append("The email arrived just now. Open it and read the code off the page.")
    else:
        lines.append(
            "The email arrived {0} seconds ago. Open it and read the code off the page.".format(
                int(age_seconds)
            )
        )
    return "\n".join(lines)
