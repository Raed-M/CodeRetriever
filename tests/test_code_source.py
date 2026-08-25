"""AppsScriptCodeSource against a mocked requests session. No network."""

from __future__ import annotations

import json

import pytest
import requests

from codebot.code_source import AppsScriptCodeSource, StubCodeSource, build_code_source

URL = "https://script.google.com/macros/s/xyz/exec"
LINK = "https://login.example.com/verify?token=abc123"
SECRET = "shared-secret"


class FakeResponse:
    def __init__(self, status_code=200, body="", headers=None, json_body=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        if json_body is not None:
            body = json.dumps(json_body)
        self.text = body

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.requests: list[dict] = []

    def post(self, url, json=None, timeout=None, allow_redirects=None):
        self.requests.append(
            {"url": url, "json": json, "timeout": timeout, "allow_redirects": allow_redirects}
        )
        if self.exception:
            raise self.exception
        return self.response


def source(session):
    return AppsScriptCodeSource(URL, SECRET, session=session)


def test_ok_response():
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": LINK, "age_seconds": 12}))
    result = source(session).fetch()

    assert (result.status, result.link, result.age_seconds) == ("ok", LINK, 12)
    sent = session.requests[0]
    assert sent["json"] == {"secret": SECRET}
    assert sent["timeout"] == 20.0
    assert sent["allow_redirects"] is True, "the /exec URL redirects to googleusercontent"


def test_not_found_response():
    session = FakeSession(FakeResponse(json_body={"status": "not_found"}))
    assert source(session).fetch().status == "not_found"


def test_error_response_carries_the_detail():
    session = FakeSession(FakeResponse(json_body={"status": "error", "detail": "label missing"}))
    result = source(session).fetch()
    assert result.status == "error"
    assert result.detail == "label missing"


def test_html_error_page_is_an_error_not_a_crash(caplog):
    """The most likely failure while setting the Apps Script deployment up."""
    html = "<!DOCTYPE html><html><head><title>Error</title></head><body>" + "x" * 500
    session = FakeSession(FakeResponse(body=html, headers={"Content-Type": "text/html"}))

    with caplog.at_level("ERROR"):
        result = source(session).fetch()

    assert result.status == "error"
    assert "non-JSON" in (result.detail or "")
    logged = [r for r in caplog.records if getattr(r, "event", "") == "code_source_non_json"]
    assert len(logged) == 1
    assert len(logged[0].body_prefix) == 200, "log a bounded slice of the page"


def test_non_200_is_an_error():
    session = FakeSession(FakeResponse(status_code=500, body="server error"))
    result = source(session).fetch()
    assert result.status == "error"
    assert "500" in (result.detail or "")


def test_network_failure_is_an_error_without_leaking_the_url(caplog):
    session = FakeSession(exception=requests.ConnectionError("failed to reach " + URL))
    with caplog.at_level("ERROR"):
        result = source(session).fetch()

    assert result.status == "error"
    assert URL not in json.dumps([r.getMessage() for r in caplog.records])


def test_ok_without_a_link_is_an_error():
    session = FakeSession(FakeResponse(json_body={"status": "ok"}))
    assert source(session).fetch().status == "error"


def test_a_bare_six_digit_code_is_rejected_now():
    """The old contract returned a code; relaying one as a link would confuse."""
    session = FakeSession(FakeResponse(json_body={"status": "ok", "code": "123456"}))
    result = source(session).fetch()
    assert result.status == "error"
    assert "not a link" in (result.detail or "")


def test_a_very_long_link_is_accepted():
    """Login URLs carry long opaque tokens; there is no length ceiling."""
    long_link = "https://login.example.com/verify?token=" + ("a" * 900)
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": long_link}))
    result = source(session).fetch()
    assert result.status == "ok"
    assert result.link == long_link


def test_link_key_aliases_are_accepted():
    """The mail-side script may name the field link, url or code."""
    for key in ("link", "url", "code"):
        session = FakeSession(FakeResponse(json_body={"status": "ok", key: LINK}))
        assert source(session).fetch().link == LINK


def test_http_link_is_accepted_but_junk_is_not():
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": "http://x.example/go"}))
    assert source(session).fetch().status == "ok"
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": "javascript:alert(1)"}))
    assert source(session).fetch().status == "error"


def test_unknown_status_is_an_error():
    session = FakeSession(FakeResponse(json_body={"status": "maybe"}))
    assert source(session).fetch().status == "error"


def test_json_array_is_an_error():
    session = FakeSession(FakeResponse(body="[1, 2, 3]"))
    assert source(session).fetch().status == "error"


def test_missing_age_is_tolerated():
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": LINK}))
    result = source(session).fetch()
    assert result.status == "ok"
    assert result.age_seconds is None


def test_the_link_is_never_logged(caplog):
    secret_link = "https://login.example.com/verify?token=super-secret-token"
    session = FakeSession(FakeResponse(json_body={"status": "ok", "link": secret_link, "age_seconds": 3}))
    with caplog.at_level("DEBUG"):
        source(session).fetch()
    assert "super-secret-token" not in caplog.text


@pytest.mark.parametrize(
    "mode,expected", [("ok", "ok"), ("not_found", "not_found"), ("error", "error")]
)
def test_stub_modes(mode, expected):
    stub = StubCodeSource(mode=mode, delay_seconds=0.0)
    assert stub.fetch().status == expected


def test_stub_returns_the_configured_link():
    stub = StubCodeSource(mode="ok", link="https://stub.example/go", delay_seconds=0.0)
    assert stub.fetch().link == "https://stub.example/go"


def test_build_code_source_picks_the_stub():
    from conftest import make_config

    assert isinstance(build_code_source(make_config(code_source="stub")), StubCodeSource)


def test_build_code_source_picks_apps_script():
    from conftest import make_config

    config = make_config(
        code_source="appsscript", apps_script_url=URL, apps_script_secret=SECRET
    )
    assert isinstance(build_code_source(config), AppsScriptCodeSource)
