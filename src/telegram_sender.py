"""
src/telegram_sender.py — Telegram notification sender.
"""

from __future__ import annotations

import html
import time as _time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

from src.logger import get_logger
from src.models import Notification

if TYPE_CHECKING:
    from src.config import Settings

log = get_logger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_PARSE_MODE = "HTML"
_MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    """Base class for all Telegram sender errors."""


class TelegramNotConfiguredError(TelegramError):
    """Raised when BOT_TOKEN or CHAT_ID are missing from settings."""


class TelegramAuthError(TelegramError):
    """Raised on 401/400/403 — bad token or invalid chat_id. Not retried."""

    def __init__(self, message: str, status_code: int, telegram_description: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.telegram_description = telegram_description


class TelegramNetworkError(TelegramError):
    """Raised on connection failures, timeouts, or server errors. Retried."""


class TelegramHTMLFormatter:
    """Formats a Notification as a Telegram HTML message."""

    EMOJI_UNIVERSITY   = "\U0001f393"
    EMOJI_ALERT        = "\U0001f6a8"
    EMOJI_TITLE        = "\U0001f4c4"
    EMOJI_DATE         = "\U0001f4c5"
    EMOJI_KEYWORDS     = "\U0001f50e"
    EMOJI_PDF          = "\U0001f4ce"
    EMOJI_WEBSITE      = "\U0001f310"
    EMOJI_TIME         = "\U0001f552"
    EMOJI_SUCCESS      = "\U0001f389"
    EMOJI_TICK         = "✅"

    @classmethod
    def format_notification(cls, notification: Notification) -> str:
        title    = cls._esc(notification.title)
        pub_date = cls._esc(notification.display_date)
        checked  = cls._esc(notification.display_checked_time)
        keywords = cls._esc(", ".join(notification.matched_keywords) or "—")
        summary  = cls._esc(notification.summary) if notification.summary else ""

        pdf_line = (
            f"{cls.EMOJI_PDF} <b>PDF:</b> "
            f'<a href="{cls._esc(notification.pdf_url)}">View PDF</a>'
        ) if notification.pdf_url else f"{cls.EMOJI_PDF} <b>PDF:</b> —"

        website_line = (
            f"{cls.EMOJI_WEBSITE} <b>Website:</b> "
            f'<a href="{cls._esc(notification.website_url)}">Open page</a>'
        )

        parts = [
            f"{cls.EMOJI_UNIVERSITY} <b>University of Calicut</b>",
            f"{cls.EMOJI_ALERT} <b>New Special Examination Notification</b>",
            "",
            f"{cls.EMOJI_TITLE} <b>Title:</b> {title}",
            f"{cls.EMOJI_DATE} <b>Published:</b> {pub_date}",
            f"{cls.EMOJI_KEYWORDS} <b>Matching Keywords:</b> {keywords}",
        ]

        if summary:
            parts.append(f"\n\U0001f4dd <b>Summary:</b>\n<i>{summary}</i>")

        parts += ["", pdf_line, website_line, "", f"{cls.EMOJI_TIME} <b>Checked:</b> {checked}"]

        message = "\n".join(parts)
        if len(message) > _MAX_MESSAGE_LENGTH:
            cutoff = _MAX_MESSAGE_LENGTH - 40
            message = message[:cutoff] + "\n\n<i>[message truncated]</i>"
        return message

    @classmethod
    def format_test_message(cls) -> str:
        return (
            f"{cls.EMOJI_SUCCESS} <b>Pareeksha Bhavan Monitor</b>\n\n"
            "GitHub Actions and Telegram integration is working successfully.\n\n"
            f"<b>Repository:</b> PareekshBahavan\n"
            f"<b>Status:</b> {cls.EMOJI_TICK} Connected"
        )

    @staticmethod
    def _esc(text: str) -> str:
        return html.escape(str(text), quote=False)


class TelegramSender:
    """Sends Telegram messages via the Bot API."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: int = 30,
        max_retries: int = 3,
        wait_seconds: float = 1.0,
        formatter: type[TelegramHTMLFormatter] | None = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._max_retries = max_retries
        self._wait_seconds = wait_seconds
        self._formatter = formatter or TelegramHTMLFormatter
        self._url = _API_BASE.format(token=self._token)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TelegramSender":
        if not settings.telegram_enabled:
            raise TelegramNotConfiguredError(
                "Telegram is not configured: BOT_TOKEN and/or CHAT_ID are missing."
            )
        return cls(
            bot_token=settings.bot_token.get_secret_value(),  # type: ignore[union-attr]
            chat_id=settings.chat_id,                          # type: ignore[arg-type]
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )

    def send_notification(self, notification: Notification) -> bool:
        message = self._formatter.format_notification(notification)
        log.info(
            "telegram_send_notification",
            notification_id=notification.notification_id,
            title=notification.title[:80],
            keywords=notification.matched_keywords,
        )
        return self._send(message)

    def send_test_message(self) -> bool:
        message = self._formatter.format_test_message()
        log.info("telegram_send_test_message", chat_id=self._chat_id)
        return self._send(message)

    def _send(self, message: str) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": _PARSE_MODE,
            "disable_web_page_preview": False,
        }

        last_exc: TelegramNetworkError | None = None
        wait = self._wait_seconds

        for attempt in range(1, self._max_retries + 1):
            try:
                return self._attempt_send(payload)
            except TelegramAuthError:
                raise
            except TelegramNetworkError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    log.warning(
                        "telegram_retry",
                        attempt=attempt,
                        max_retries=self._max_retries,
                        wait_seconds=wait,
                        error=str(exc),
                    )
                    _time.sleep(wait)
                    wait = min(wait * 2, 30.0)

        raise TelegramNetworkError(
            f"All {self._max_retries} Telegram send attempts failed. Last error: {last_exc}"
        ) from last_exc

    def _attempt_send(self, payload: dict[str, Any]) -> bool:
        start = _time.monotonic()
        try:
            response = requests.post(self._url, json=payload, timeout=self._timeout)
        except requests.exceptions.Timeout as exc:
            log.warning("telegram_timeout", timeout_seconds=self._timeout, error=str(exc))
            raise TelegramNetworkError(f"Telegram request timed out after {self._timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            log.warning("telegram_connection_error", error=str(exc))
            raise TelegramNetworkError(f"Connection error while reaching Telegram API: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            log.warning("telegram_request_error", error=str(exc))
            raise TelegramNetworkError(f"Unexpected requests error: {exc}") from exc

        elapsed_ms = int((_time.monotonic() - start) * 1000)
        log.debug("telegram_response", status_code=response.status_code, elapsed_ms=elapsed_ms)

        if response.status_code in (400, 401, 403):
            description = ""
            try:
                description = response.json().get("description", "")
            except Exception:
                pass
            log.error("telegram_auth_error", status_code=response.status_code, description=description)
            raise TelegramAuthError(
                f"Telegram API returned {response.status_code}: {description}",
                status_code=response.status_code,
                telegram_description=description,
            )

        if response.status_code >= 500:
            log.warning("telegram_server_error", status_code=response.status_code)
            raise TelegramNetworkError(f"Telegram server error: HTTP {response.status_code}")

        if response.status_code != 200:
            raise TelegramNetworkError(f"Unexpected Telegram status code: {response.status_code}")

        try:
            data = response.json()
            ok = data.get("ok", False)
        except Exception:
            ok = False

        if ok:
            log.info(
                "telegram_sent_ok",
                elapsed_ms=elapsed_ms,
                message_id=response.json().get("result", {}).get("message_id"),
            )
            return True

        description = response.json().get("description", "unknown error")
        log.error("telegram_ok_false", description=description)
        raise TelegramAuthError(
            f"Telegram returned ok=false: {description}",
            status_code=200,
            telegram_description=description,
        )
