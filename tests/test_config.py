"""Startup configuration must fail fast and never leak secrets."""

from __future__ import annotations

import pytest

from codebot.config import Config, ConfigError

BASE_ENV = {
    "BOT_TOKEN": "123:abc",
    "WEBHOOK_SECRET": "s3cret",
    "ALLOWED_USER_IDS": "1,2,3",
    "CODE_SOURCE": "stub",
}


def test_stub_config_parses():
    config = Config.from_env(BASE_ENV)
    assert config.allowed_user_ids == frozenset({1, 2, 3})
    assert config.code_source == "stub"
    assert config.port == 8080


def test_whitespace_and_trailing_commas_in_user_ids():
    config = Config.from_env(dict(BASE_ENV, ALLOWED_USER_IDS=" 10 , 20 ,"))
    assert config.allowed_user_ids == frozenset({10, 20})


@pytest.mark.parametrize("missing", ["BOT_TOKEN", "WEBHOOK_SECRET", "ALLOWED_USER_IDS"])
def test_missing_required_variable_is_fatal(missing):
    env = dict(BASE_ENV)
    del env[missing]
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env)
    assert missing in str(excinfo.value)


def test_empty_variable_counts_as_missing():
    with pytest.raises(ConfigError):
        Config.from_env(dict(BASE_ENV, BOT_TOKEN="   "))


def test_non_integer_user_id_is_fatal():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(dict(BASE_ENV, ALLOWED_USER_IDS="1,bob"))
    assert "ALLOWED_USER_IDS" in str(excinfo.value)


def test_appsscript_requires_url_and_secret():
    env = dict(BASE_ENV, CODE_SOURCE="appsscript")
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env)
    message = str(excinfo.value)
    assert "APPS_SCRIPT_URL" in message and "APPS_SCRIPT_SECRET" in message


def test_appsscript_config_parses():
    config = Config.from_env(
        dict(
            BASE_ENV,
            CODE_SOURCE="appsscript",
            APPS_SCRIPT_URL="https://script.google.com/macros/s/xyz/exec",
            APPS_SCRIPT_SECRET="shared",
        )
    )
    assert config.code_source == "appsscript"
    assert config.apps_script_url.endswith("/exec")


def test_appsscript_url_must_be_https():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(
            dict(
                BASE_ENV,
                CODE_SOURCE="appsscript",
                APPS_SCRIPT_URL="http://insecure.example/exec",
                APPS_SCRIPT_SECRET="shared",
            )
        )
    assert "https" in str(excinfo.value)


def test_defaults_to_appsscript_when_unset():
    env = {k: v for k, v in BASE_ENV.items() if k != "CODE_SOURCE"}
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env)
    assert "APPS_SCRIPT_URL" in str(excinfo.value)


def test_unknown_code_source_is_fatal():
    with pytest.raises(ConfigError):
        Config.from_env(dict(BASE_ENV, CODE_SOURCE="imap"))


def test_port_comes_from_the_environment():
    assert Config.from_env(dict(BASE_ENV, PORT="9090")).port == 9090


def test_bad_port_is_fatal():
    with pytest.raises(ConfigError):
        Config.from_env(dict(BASE_ENV, PORT="eighty"))


def test_all_problems_are_reported_at_once():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env({"CODE_SOURCE": "stub"})
    message = str(excinfo.value)
    for name in ("BOT_TOKEN", "WEBHOOK_SECRET", "ALLOWED_USER_IDS"):
        assert name in message


def test_repr_hides_every_secret():
    config = Config.from_env(
        dict(
            BASE_ENV,
            CODE_SOURCE="appsscript",
            APPS_SCRIPT_URL="https://script.google.com/macros/s/xyz/exec",
            APPS_SCRIPT_SECRET="shared-secret-value",
        )
    )
    rendered = "{0!r} {0!s}".format(config)
    assert "123:abc" not in rendered
    assert "s3cret" not in rendered
    assert "shared-secret-value" not in rendered
    assert "redacted" in rendered
