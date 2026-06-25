"""
tests/test_config.py — Unit tests for src/config.py
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
    """Build a Settings instance with sane test defaults."""
    defaults = dict(
        bot_token="test-bot-token",
        chat_id="12345",
        email_host="smtp.example.com",
        email_username="user@example.com",
        email_password="secret",
        email_to="recipient@example.com",
    )
    defaults.update(overrides)
    return Settings.model_validate(defaults)


# ---------------------------------------------------------------------------
# Tests — defaults
# ---------------------------------------------------------------------------

class TestSettingsDefaults:
    def test_default_base_url(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.model_validate({})
        assert s.base_url == "https://pareekshabhavan.uoc.ac.in/"

    def test_default_log_level(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.model_validate({})
        assert s.log_level == "INFO"

    def test_default_email_port(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.model_validate({})
        assert s.email_port == 587

    def test_default_request_timeout(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.model_validate({})
        assert s.request_timeout == 30

    def test_default_max_retries(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = Settings.model_validate({})
        assert s.max_retries == 3


# ---------------------------------------------------------------------------
# Tests — keyword parsing
# ---------------------------------------------------------------------------

class TestKeywordParsing:
    def test_keywords_parsed_from_raw(self):
        s = _make_settings(KEYWORDS="Alpha,Beta,Gamma")
        assert s.keywords == ["Alpha", "Beta", "Gamma"]

    def test_keywords_strip_whitespace(self):
        s = _make_settings(KEYWORDS="  Alpha , Beta ,  Gamma  ")
        assert s.keywords == ["Alpha", "Beta", "Gamma"]

    def test_keywords_empty_segments_skipped(self):
        s = _make_settings(KEYWORDS="Alpha,,Beta,")
        assert s.keywords == ["Alpha", "Beta"]

    def test_default_keywords_non_empty(self):
        s = _make_settings()
        assert len(s.keywords) > 0
        assert "Special Examination" in s.keywords


# ---------------------------------------------------------------------------
# Tests — properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_telegram_enabled_when_both_present(self):
        s = _make_settings(bot_token="tok", chat_id="123")
        assert s.telegram_enabled is True

    def test_telegram_disabled_when_token_missing(self):
        s = _make_settings(bot_token=None, chat_id="123")
        assert s.telegram_enabled is False

    def test_telegram_disabled_when_chat_id_missing(self):
        s = _make_settings(bot_token="tok", chat_id=None)
        assert s.telegram_enabled is False

    def test_email_enabled_when_all_present(self):
        s = _make_settings()
        assert s.email_enabled is True

    def test_email_disabled_when_host_missing(self):
        s = _make_settings(email_host=None)
        assert s.email_enabled is False

    def test_effective_log_level_debug_mode(self):
        s = _make_settings(debug_mode=True, log_level="INFO")
        assert s.effective_log_level == "DEBUG"

    def test_effective_log_level_normal(self):
        s = _make_settings(debug_mode=False, log_level="WARNING")
        assert s.effective_log_level == "WARNING"


# ---------------------------------------------------------------------------
# Tests — validators
# ---------------------------------------------------------------------------

class TestValidators:
    def test_invalid_log_level_raises(self):
        with pytest.raises(Exception):
            _make_settings(log_level="VERBOSE")

    def test_log_level_case_insensitive(self):
        s = _make_settings(log_level="debug")
        assert s.log_level == "DEBUG"

    def test_last_seen_path_coerced_from_string(self):
        s = _make_settings(last_seen_path="custom/path.json")
        assert isinstance(s.last_seen_path, Path)
        assert s.last_seen_path == Path("custom/path.json")

    def test_log_file_empty_string_becomes_none(self):
        s = _make_settings(log_file="")
        assert s.log_file is None


# ---------------------------------------------------------------------------
# Tests — warning when no notifiers configured
# ---------------------------------------------------------------------------

class TestNoNotifiersWarning:
    def test_warns_when_neither_telegram_nor_email(self):
        with pytest.warns(UserWarning, match="Neither Telegram nor Email"):
            Settings.model_validate({
                "bot_token": None,
                "chat_id": None,
                "email_host": None,
                "email_username": None,
                "email_password": None,
                "email_to": None,
            })


# ---------------------------------------------------------------------------
# Tests — get_settings cache
# ---------------------------------------------------------------------------

class TestGetSettings:
    def setup_method(self):
        get_settings.cache_clear()

    def teardown_method(self):
        get_settings.cache_clear()

    def test_get_settings_returns_settings_instance(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = get_settings()
        assert isinstance(s, Settings)

    def test_get_settings_is_cached(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s1 = get_settings()
            s2 = get_settings()
        assert s1 is s2
