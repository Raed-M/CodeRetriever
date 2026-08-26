"""Loading a local .env, and the precedence rule that keeps it safe."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebot.config import Config, load_env_file, parse_env_file

NL = chr(10)
DQ = chr(34)


def test_parses_plain_pairs():
    values = parse_env_file(NL.join(["BOT_TOKEN=123:abc", "PORT=9090"]))
    assert values == {"BOT_TOKEN": "123:abc", "PORT": "9090"}


def test_skips_blanks_and_comments():
    text = NL.join(["# a comment", "", "   ", "BOT_TOKEN=abc", "# trailing note"])
    assert parse_env_file(text) == {"BOT_TOKEN": "abc"}


def test_tolerates_export_prefix_and_spaces():
    text = NL.join(["export BOT_TOKEN=abc", "  WEBHOOK_SECRET = xyz  "])
    assert parse_env_file(text) == {"BOT_TOKEN": "abc", "WEBHOOK_SECRET": "xyz"}


def test_strips_one_layer_of_quotes():
    text = NL.join(["A=" + DQ + "quoted" + DQ, "B='single'", "C=" + DQ + "half"])
    values = parse_env_file(text)
    assert values["A"] == "quoted"
    assert values["B"] == "single"
    assert values["C"] == DQ + "half", "an unmatched quote is left alone"


def test_windows_line_endings():
    text = "BOT_TOKEN=abc" + chr(13) + NL + "PORT=8080" + chr(13) + NL
    assert parse_env_file(text) == {"BOT_TOKEN": "abc", "PORT": "8080"}


def test_a_hash_inside_a_value_is_kept():
    """Secrets may contain #; truncating one silently would be worse."""
    assert parse_env_file("WEBHOOK_SECRET=abc#def")["WEBHOOK_SECRET"] == "abc#def"


def test_url_with_query_string_survives():
    line = "APPS_SCRIPT_URL=https://script.google.com/macros/s/AKfy_x-y/exec?a=1&b=2"
    values = parse_env_file(line)
    assert values["APPS_SCRIPT_URL"].endswith("exec?a=1&b=2")


def test_value_containing_equals_is_kept_whole():
    assert parse_env_file("SECRET=aa=bb=cc")["SECRET"] == "aa=bb=cc"


def test_unparseable_line_is_skipped_without_logging_its_content(caplog):
    with caplog.at_level("WARNING"):
        values = parse_env_file(NL.join(["JUST_A_WORD_NO_EQUALS", "GOOD=1"]))
    assert values == {"GOOD": "1"}
    assert "JUST_A_WORD_NO_EQUALS" not in caplog.text
    assert any(getattr(r, "event", "") == "env_file_bad_line" for r in caplog.records)


def test_missing_file_is_not_an_error(tmp_path: Path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_loads_from_a_real_file(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("BOT_TOKEN=from-file" + NL, encoding="utf-8")
    assert load_env_file(path) == {"BOT_TOKEN": "from-file"}


def test_load_logs_key_names_but_never_values(caplog, tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("BOT_TOKEN=super-secret-value" + NL, encoding="utf-8")
    with caplog.at_level("INFO"):
        load_env_file(path)

    record = next(r for r in caplog.records if getattr(r, "event", "") == "env_file_loaded")
    assert record.keys == ["BOT_TOKEN"], "key names are logged"
    everything_logged = caplog.text + repr([r.__dict__ for r in caplog.records])
    assert "super-secret-value" not in everything_logged, "values are not"


def test_real_environment_wins_over_the_file(monkeypatch, tmp_path: Path):
    """A file must never override a secret injected by the platform."""
    path = tmp_path / ".env"
    path.write_text(
        NL.join(
            [
                "BOT_TOKEN=from-file",
                "WEBHOOK_SECRET=from-file",
                "ALLOWED_USER_IDS=1",
                "CODE_SOURCE=stub",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(path))
    monkeypatch.setenv("BOT_TOKEN", "from-environment")

    config = Config.from_env()
    assert config.bot_token == "from-environment"
    assert config.webhook_secret == "from-file", "the file still fills the gaps"


def test_env_file_can_be_disabled(monkeypatch, tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("BOT_TOKEN=from-file" + NL, encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", "")
    assert load_env_file() == {}


def test_an_explicit_mapping_ignores_the_file(monkeypatch, tmp_path: Path):
    """Tests must not be steered by whatever .env a developer has locally."""
    path = tmp_path / ".env"
    path.write_text("BOT_TOKEN=from-file" + NL, encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(path))

    with pytest.raises(Exception) as excinfo:
        Config.from_env({"CODE_SOURCE": "stub"})
    assert "BOT_TOKEN" in str(excinfo.value)
