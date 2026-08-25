"""Endpoint contract: the secret header, the always-200 rule, the whitelist."""

from __future__ import annotations

from conftest import ALLOWED_USER_ID, OTHER_USER_ID, SECRET, callback_update, message_update


def test_healthz(harness):
    h = harness()
    response = h.client.get("/healthz")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"


def test_missing_secret_header_is_403(harness):
    h = harness()
    response = h.post_update(message_update("/code"), secret=None)
    assert response.status_code == 403
    assert response.get_data() == b""
    assert h.telegram.calls == []


def test_wrong_secret_header_is_403(harness):
    h = harness()
    response = h.post_update(message_update("/code"), secret="not-the-secret")
    assert response.status_code == 403
    assert h.telegram.calls == []


def test_secret_header_that_is_a_prefix_is_rejected(harness):
    h = harness()
    response = h.post_update(message_update("/code"), secret=SECRET[:-1])
    assert response.status_code == 403


def test_non_ascii_secret_header_does_not_crash(harness):
    h = harness()
    response = h.post_update(message_update("/code"), secret="sécret")
    assert response.status_code == 403


def test_unauthorised_user_gets_silence(harness):
    h = harness()
    response = h.post_update(message_update("/code", user_id=OTHER_USER_ID))
    assert response.status_code == 200
    assert response.get_data() == b""
    assert h.telegram.calls == [], "an unauthorised user must get no reply at all"


def test_unauthorised_callback_is_not_even_answered(harness):
    h = harness()
    response = h.post_update(callback_update(user_id=OTHER_USER_ID))
    assert response.status_code == 200
    assert h.telegram.calls == []


def test_malformed_payload_is_200_and_silent(harness):
    h = harness()
    for payload in ([], "text", 12, {"update_id": "not-an-int"}, {"update_id": 1}):
        h.telegram.reset()
        response = h.client.post(
            "/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
        )
        assert response.status_code == 200
        assert h.telegram.calls == []


def test_non_json_body_is_200(harness):
    h = harness()
    response = h.client.post(
        "/webhook",
        data=b"<html>not json</html>",
        content_type="text/html",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert response.status_code == 200


def test_handler_crash_still_returns_200(harness):
    class Exploding:
        def fetch(self):
            raise RuntimeError("boom")

    h = harness(Exploding())
    response = h.post_update(message_update("/code"))
    assert response.status_code == 200
    assert "lookup failed" in h.telegram.last_text.lower()


def test_group_chat_is_ignored(harness):
    h = harness()
    response = h.post_update(message_update("/code", chat_type="group"))
    assert response.status_code == 200
    assert h.telegram.calls == [], "a login code must never be relayed into a group"


def test_allowed_user_from_a_second_id_works(harness):
    h = harness(allowed={ALLOWED_USER_ID, OTHER_USER_ID})
    h.post_update(message_update("/start", user_id=OTHER_USER_ID))
    assert h.telegram.methods == ["sendMessage"]
