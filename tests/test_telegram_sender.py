"""
tests/test_telegram_sender.py — Unit tests for src/telegram_sender.py

All HTTP calls are mocked with the ``responses`` library — no real network
traffic occurs.  Tests are fully isolated and deterministic.

Coverage
--------
- Successful send (notification + test message)
- Invalid token (401 Unauthorized)
- Invalid chat_id (400 Bad Request)
- Forbidden (403)
- Timeout error
- Connection error
- Server error (500) with retry exhaustion
- Retry behaviour (fails n-1 times, succeeds on nth)
- TelegramNotConfiguredError when credentials missing
- HTML message formatting (escaping, structure, fields)
- TelegramHTMLFormatter independently
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_lib
from responses import RequestsMock

from src.models import Notification
from src.telegram_sender import (
    TelegramAuthError,
    TelegramHTMLFormatter,
    TelegramNetworkError,
    TelegramNotConfiguredError,
    TelegramSender,
    _API_BASE,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

BOT_TOKEN = "123456:ABC-test-token"
CHAT_ID = "987654321"
API_URL = _API_BASE.format(token=BOT_TOKEN)


def _make_sender(
    bot_token: str = BOT_TOKEN,
    chat_id: str = CHAT_ID,
    max_retries: int = 3,
    timeout: int = 5,
) -> TelegramSender:
    return TelegramSender(
        bot_token=bot_token,
        chat_id=chat_id,
        timeout=timeout,
        max_retries=max_retries,
        wait_seconds=0,  # no actual sleeping in tests
    )


def _make_notification(
    title: str = "Special Examination Notice 2026",
    keywords: list[str] | None = None,
    pdf_url: str | None = "https://example.com/notice.pdf",
    summary: str = "B.Sc Computer Science third semester exam details.",
) -> Notification:
    return Notification(
        title=title,
        website_url="https://pareekshabhavan.uoc.ac.in/notices/1",
        publication_date=datetime(2026, 6, 25, tzinfo=timezone.utc),
        pdf_url=pdf_url,
        summary=summary,
        matched_keywords=keywords or ["Special Examination", "CBCSS", "B.Sc"],
        notification_id="abc123456789abcd",
        checked_time=datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc),
    )


def _ok_response(message_id: int = 42) -> dict:
    return {"ok": True, "result": {"message_id": message_id}}


def _error_response(description: str) -> dict:
    return {"ok": False, "description": description}


# ---------------------------------------------------------------------------
# TelegramHTMLFormatter — formatting tests
# ---------------------------------------------------------------------------

class TestTelegramHTMLFormatter:

    def test_format_notification_contains_university(self):
        n = _make_notification()
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "University of Calicut" in msg

    def test_format_notification_contains_title(self):
        n = _make_notification(title="Special Exam 2026")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "Special Exam 2026" in msg

    def test_format_notification_contains_keywords(self):
        n = _make_notification(keywords=["CBCSS", "B.Sc"])
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "CBCSS" in msg
        assert "B.Sc" in msg

    def test_format_notification_contains_date(self):
        n = _make_notification()
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "25 Jun 2026" in msg

    def test_format_notification_contains_pdf_link(self):
        n = _make_notification(pdf_url="https://example.com/exam.pdf")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "https://example.com/exam.pdf" in msg

    def test_format_notification_no_pdf_shows_dash(self):
        n = _make_notification(pdf_url=None)
        msg = TelegramHTMLFormatter.format_notification(n)
        # PDF line should exist but show a dash
        assert "PDF:" in msg
        assert "—" in msg

    def test_format_notification_contains_website_link(self):
        n = _make_notification()
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "pareekshabhavan.uoc.ac.in" in msg

    def test_format_notification_contains_checked_time(self):
        n = _make_notification()
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "25 Jun 2026" in msg
        assert "12:00 UTC" in msg

    def test_format_notification_escapes_html_in_title(self):
        n = _make_notification(title="<script>alert('xss')</script>")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg

    def test_format_notification_escapes_ampersand(self):
        n = _make_notification(title="Arts & Science College")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "Arts & Science" not in msg
        assert "Arts &amp; Science" in msg

    def test_format_notification_summary_included(self):
        n = _make_notification(summary="Exam scheduled for July 2026.")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "Exam scheduled for July 2026." in msg

    def test_format_notification_empty_summary_omitted(self):
        n = _make_notification(summary="")
        msg = TelegramHTMLFormatter.format_notification(n)
        assert "Summary:" not in msg

    def test_format_notification_length_within_telegram_limit(self):
        # Build a notification with a very long title to exercise truncation
        n = _make_notification(title="X" * 5000)
        msg = TelegramHTMLFormatter.format_notification(n)
        assert len(msg) <= 4096

    def test_format_test_message_contains_repo_name(self):
        msg = TelegramHTMLFormatter.format_test_message()
        assert "PareekshBahavan" in msg

    def test_format_test_message_contains_connected(self):
        msg = TelegramHTMLFormatter.format_test_message()
        assert "Connected" in msg

    def test_format_test_message_contains_success_emoji(self):
        msg = TelegramHTMLFormatter.format_test_message()
        assert TelegramHTMLFormatter.EMOJI_SUCCESS in msg

    def test_esc_escapes_lt_gt_amp(self):
        assert TelegramHTMLFormatter._esc("<b>&foo</b>") == "&lt;b&gt;&amp;foo&lt;/b&gt;"


# ---------------------------------------------------------------------------
# TelegramSender — send_notification
# ---------------------------------------------------------------------------

class TestTelegramSenderSendNotification:

    @resp_lib.activate
    def test_send_notification_success(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        result = sender.send_notification(_make_notification())
        assert result is True

    @resp_lib.activate
    def test_send_notification_posts_to_correct_url(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        sender.send_notification(_make_notification())
        assert len(resp_lib.calls) == 1
        assert BOT_TOKEN in resp_lib.calls[0].request.url

    @resp_lib.activate
    def test_send_notification_uses_html_parse_mode(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        sender.send_notification(_make_notification())
        import json
        body = json.loads(resp_lib.calls[0].request.body)
        assert body["parse_mode"] == "HTML"

    @resp_lib.activate
    def test_send_notification_includes_chat_id(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        sender.send_notification(_make_notification())
        import json
        body = json.loads(resp_lib.calls[0].request.body)
        assert body["chat_id"] == CHAT_ID


# ---------------------------------------------------------------------------
# TelegramSender — send_test_message
# ---------------------------------------------------------------------------

class TestTelegramSenderTestMessage:

    @resp_lib.activate
    def test_send_test_message_success(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        result = sender.send_test_message()
        assert result is True

    @resp_lib.activate
    def test_send_test_message_body_contains_repo(self):
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender()
        sender.send_test_message()
        import json
        body = json.loads(resp_lib.calls[0].request.body)
        assert "PareekshBahavan" in body["text"]


# ---------------------------------------------------------------------------
# TelegramSender — auth errors (no retry)
# ---------------------------------------------------------------------------

class TestTelegramSenderAuthErrors:

    @resp_lib.activate
    def test_raises_auth_error_on_401(self):
        resp_lib.add(
            resp_lib.POST, API_URL,
            json=_error_response("Unauthorized"),
            status=401,
        )
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramAuthError) as exc_info:
            sender.send_notification(_make_notification())
        assert exc_info.value.status_code == 401
        # Should NOT retry a 401 — only one HTTP call made
        assert len(resp_lib.calls) == 1

    @resp_lib.activate
    def test_raises_auth_error_on_400_bad_chat_id(self):
        resp_lib.add(
            resp_lib.POST, API_URL,
            json=_error_response("Bad Request: chat not found"),
            status=400,
        )
        sender = _make_sender(max_retries=3)
        with pytest.raises(TelegramAuthError) as exc_info:
            sender.send_notification(_make_notification())
        assert exc_info.value.status_code == 400
        assert "chat not found" in exc_info.value.telegram_description
        # Should NOT retry a 400 — only one HTTP call made
        assert len(resp_lib.calls) == 1

    @resp_lib.activate
    def test_raises_auth_error_on_403(self):
        resp_lib.add(
            resp_lib.POST, API_URL,
            json=_error_response("Forbidden: bot was blocked"),
            status=403,
        )
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramAuthError):
            sender.send_notification(_make_notification())
        assert len(resp_lib.calls) == 1

    @resp_lib.activate
    def test_raises_auth_error_when_ok_false(self):
        """200 OK but ok=false in JSON body is treated as auth/config error."""
        resp_lib.add(
            resp_lib.POST, API_URL,
            json={"ok": False, "description": "message text is empty"},
            status=200,
        )
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramAuthError) as exc_info:
            sender.send_notification(_make_notification())
        assert "message text is empty" in exc_info.value.telegram_description


# ---------------------------------------------------------------------------
# TelegramSender — network errors (retried)
# ---------------------------------------------------------------------------

class TestTelegramSenderNetworkErrors:

    @resp_lib.activate
    def test_raises_network_error_on_timeout(self):
        resp_lib.add(
            resp_lib.POST, API_URL,
            body=__import__("requests").exceptions.Timeout("timed out"),
        )
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramNetworkError, match="timed out"):
            sender.send_notification(_make_notification())

    @resp_lib.activate
    def test_raises_network_error_on_connection_error(self):
        resp_lib.add(
            resp_lib.POST, API_URL,
            body=__import__("requests").exceptions.ConnectionError("refused"),
        )
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramNetworkError, match="Connection error"):
            sender.send_notification(_make_notification())

    @resp_lib.activate
    def test_raises_network_error_on_500(self):
        resp_lib.add(resp_lib.POST, API_URL, json={}, status=500)
        sender = _make_sender(max_retries=1)
        with pytest.raises(TelegramNetworkError):
            sender.send_notification(_make_notification())

    @resp_lib.activate
    def test_retries_on_500_up_to_max(self):
        """Server error is retried max_retries times."""
        for _ in range(3):
            resp_lib.add(resp_lib.POST, API_URL, json={}, status=500)
        sender = _make_sender(max_retries=3)
        with pytest.raises(TelegramNetworkError):
            sender.send_notification(_make_notification())
        assert len(resp_lib.calls) == 3

    @resp_lib.activate
    def test_succeeds_on_retry_after_transient_failure(self):
        """Fails twice with 500, succeeds on third attempt."""
        resp_lib.add(resp_lib.POST, API_URL, json={}, status=500)
        resp_lib.add(resp_lib.POST, API_URL, json={}, status=500)
        resp_lib.add(resp_lib.POST, API_URL, json=_ok_response(), status=200)
        sender = _make_sender(max_retries=3)
        result = sender.send_notification(_make_notification())
        assert result is True
        assert len(resp_lib.calls) == 3


# ---------------------------------------------------------------------------
# TelegramSender — not configured
# ---------------------------------------------------------------------------

class TestTelegramSenderNotConfigured:

    def test_from_settings_raises_when_not_configured(self):
        mock_settings = MagicMock()
        mock_settings.telegram_enabled = False
        with pytest.raises(TelegramNotConfiguredError):
            TelegramSender.from_settings(mock_settings)

    def test_from_settings_succeeds_when_configured(self):
        mock_settings = MagicMock()
        mock_settings.telegram_enabled = True
        mock_settings = MagicMock()
        mock_settings.telegram_enabled = True
        mock_settings.bot_token.get_secret_value.return_value = BOT_TOKEN
        mock_settings.chat_id = CHAT_ID
        mock_settings.request_timeout = 30
        mock_settings.max_retries = 3
        sender = TelegramSender.from_settings(mock_settings)
        assert isinstance(sender, TelegramSender)
