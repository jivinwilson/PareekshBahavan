"""
src/telegram_sender.py — Telegram notification sender.

Responsibility
--------------
Send formatted Telegram messages via the Bot API.  This module knows nothing
about scraping, PDF extraction, or email — it has one job: take a
``Notification`` (or a test message), format it, and deliver it to a Telegram
chat.

Design
------
TelegramSender
    The main class.  Instantiate with a ``Settings`` object (or let the
    module-level factory ``from_settings()`` do it).  Call:

    - ``send_notification(notification)``  — send a full alert
    - ``send_test_message()``              — verify connectivity

TelegramHTMLFormatter
    Pure formatting logic, fully separated from HTTP transport.
    Tested independently.

TelegramError hierarchy
    ``TelegramError`` → base
    ``TelegramAuthError``   — bad token / chat_id (4xx from Telegram)
    ``TelegramNetworkError`` — connection/timeout failures (retried)
    ``TelegramNotConfiguredError`` — credentials missing in settings

Retry strategy
    Uses ``tenacity`` with exponential back-off.  Network/timeout errors are
    retried up to ``settings.max_retries`` times.  Auth errors are *not*
    retried (retrying a 401 wastes quota and never recovers).

Security
    ``BOT_TOKEN`` is read from ``SecretStr``; ``.get_secret_value()`` is
    called only at send time so the token never ends up in repr() or logs.

Usage
-----
    from src.telegram_sender import TelegramSender
    from src.config import get_settings

    sender = TelegramSender.from_settings(get_settings())
    ok = sender.send_notification(notification)
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import time as _time

import requests

from src.logger import get_logger
from src.models import Notification

if TYPE_CHECKING:
    from src.config import Settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Telegram API constants
# ---------------------------------------------------------------------------

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_PARSE_MODE = "HTML"
_MAX_MESSAGE_LENGTH = 4096   # Telegram hard limit


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class TelegramError(Exception):
    """Base class for all Telegram sender errors."""


class TelegramNotConfiguredError(TelegramError):
    """
    Raised when ``BOT_TOKEN`` or ``CHAT_ID`` are missing from settings.

    This is a configuration error, not a network error — no retry.
    """


class TelegramAuthError(TelegramError):
    """
    Raised when Telegram returns 401 Unauthorized or 400 Bad Request.

    Indicates a bad token or an invalid chat_id.  No retry — the error
    is deterministic.

    Attributes
    ----------
    status_code:
        HTTP status code returned by Telegram.
    telegram_description:
        Error description from Telegram's JSON response.
    """

    def __init__(
        self,
        message: str,
        status_code: int,
        telegram_description: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.telegram_description = telegram_description


class TelegramNetworkError(TelegramError):
    """
    Raised on connection failures, timeouts, or unexpected HTTP errors.

    These errors are retried with exponential back-off.
    """


# ---------------------------------------------------------------------------
# HTML Formatter
# ---------------------------------------------------------------------------

class TelegramHTMLFormatter:
    """
    Formats a ``Notification`` as a Telegram HTML message.

    All user-supplied text is passed through ``html.escape()`` before
    embedding in the message template so that special characters
    (``<``, ``>``, ``&``) cannot break the HTML layout or inject tags.

    This class is stateless — all methods are static or class methods.
    """

    # Emojis kept as constants so tests can reference them
    EMOJI_UNIVERSITY   = "🎓"
    EMOJI_ALERT        = "🚨"
    EMOJI_TITLE        = "📄"
    EMOJI_DATE         = "📅"
    EMOJI_KEYWORDS     = "🔎"
    EMOJI_PDF          = "📎"
    EMOJI_WEBSITE      = "🌐"
    EMOJI_TIME         = "🕒"
    EMOJI_SUCCESS      = "🎉"
    EMOJI_TICK         = "✅"

    @classmethod
    def format_notification(cls, notification: Notification) -> str:
        """
        Build the full HTML message for a new notification alert.

        Parameters
        ----------
        notification:
            The ``Notification`` to format.  ``matched_keywords`` must be
            non-empty (it indicates a confirmed match).

        Returns
        -------
        str
            HTML string, safe to pass as ``text`` to the Telegram API with
            ``parse_mode=HTML``.
        """
        title       = cls._esc(notification.title)
        pub_date    = cls._esc(notification.display_date)
        checked     = cls._esc(notification.display_checked_time)
        keywords    = cls._esc(", ".join(notification.matched_keywords) or "—")
        summary     = cls._esc(notification.summary) if notification.summary else ""

        # Build PDF line only when a URL is present
        pdf_line = (
            f"{cls.EMOJI_PDF} <b>PDF:</b> "
            f'<a href="{cls._esc(notification.pdf_url)}">'
            f"View PDF</a>"
        ) if notification.pdf_url else f"{cls.EMOJI_PDF} <b>PDF:</b> —"

        website_line = (
            f"{cls.EMOJI_WEBSITE} <b>Website:</b> "
            f'<a href="{cls._esc(notification.website_url)}">'
            f"Open page</a>"
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
            parts.append(f"\n📝 <b>Summary:</b>\n<i>{summary}</i>")

        parts += [
            "",
            pdf_line,
            website_line,
            "",
            f"{cls.EMOJI_TIME} <b>Checked:</b> {checked}",
        ]

        message = "\n".join(parts)

        # Truncate to Telegram's hard limit; append a note if truncated
        if len(message) > _MAX_MESSAGE_LENGTH:
            cutoff = _MAX_MESSAGE_LENGTH - 40
            message = message[:cutoff] + "\n\n<i>[message truncated]</i>"

        return message

    @classmethod
    def format_test_message(cls) -> str:
        """
        Build the connectivity test message.

        Returns
        -------
        str
            HTML string confirming Telegram integration is working.
        """
        return (
            f"{cls.EMOJI_SUCCESS} <b>Pareeksha Bhavan Monitor</b>\n\n"
            "GitHub Actions and Telegram integration is working successfully.\n\n"
            f"<b>Repository:</b> PareekshBahavan\n"
            f"<b>Status:</b> {cls.EMOJI_TICK} Connected"
        )

    @staticmethod
    def _esc(text: str) -> str:
        """
        Escape a string for safe embedding in a Telegram HTML message.

        Escapes ``&``, ``<``, and ``>`` using ``html.escape()``.
        """
        return html.escape(str(text), quote=False)


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

class TelegramSender:
    """
    Sends Telegram messages via the Bot API.

    Parameters
    ----------
    bot_token:
        Telegram Bot HTTP API token (plain string — obtained from SecretStr
        at call site, not stored here as plain text in practice).
    chat_id:
        Telegram chat or user ID.
    timeout:
        HTTP request timeout in seconds.
    max_retries:
        Number of retry attempts for network errors.
    formatter:
        Optional custom ``TelegramHTMLFormatter`` subclass.
        Defaults to ``TelegramHTMLFormatter``.
    """

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
        self._wait_seconds = wait_seconds   # base back-off; doubles each retry
        self._formatter = formatter or TelegramHTMLFormatter
        self._url = _API_BASE.format(token=self._token)

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TelegramSender":
        """
        Construct a ``TelegramSender`` from application settings.

        Raises
        ------
        TelegramNotConfiguredError
            If ``BOT_TOKEN`` or ``CHAT_ID`` are absent from settings.
        """
        if not settings.telegram_enabled:
            raise TelegramNotConfiguredError(
                "Telegram is not configured: BOT_TOKEN and/or CHAT_ID are missing. "
                "Set them as environment variables or in your .env file."
            )
        return cls(
            bot_token=settings.bot_token.get_secret_value(),  # type: ignore[union-attr]
            chat_id=settings.chat_id,                          # type: ignore[arg-type]
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def send_notification(self, notification: Notification) -> bool:
        """
        Send a formatted notification alert to the configured Telegram chat.

        Parameters
        ----------
        notification:
            The matched notification to send.

        Returns
        -------
        bool
            ``True`` on success, ``False`` if all retries were exhausted
            without success (only possible when ``reraise=False``).

        Raises
        ------
        TelegramAuthError
            On 401/400 responses from Telegram (bad token or chat_id).
        TelegramNetworkError
            After all retries are exhausted on connection/timeout errors.
        """
        message = self._formatter.format_notification(notification)
        log.info(
            "telegram_send_notification",
            notification_id=notification.notification_id,
            title=notification.title[:80],
            keywords=notification.matched_keywords,
        )
        return self._send(message)

    def send_test_message(self) -> bool:
        """
        Send a connectivity test message to the configured Telegram chat.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        TelegramAuthError
            On 401/400 responses.
        TelegramNetworkError
            After all retries are exhausted.
        """
        message = self._formatter.format_test_message()
        log.info("telegram_send_test_message", chat_id=self._chat_id)
        return self._send(message)

    # ── Internal send logic ───────────────────────────────────────────────────

    def _send(self, message: str) -> bool:
        """
        POST *message* to the Telegram Bot API with exponential back-off retry.

        Auth errors (400/401/403) are **not** retried — they are deterministic
        and retrying wastes quota.  Network/server errors are retried up to
        ``self._max_retries`` times with doubling wait intervals.

        Parameters
        ----------
        message:
            Pre-formatted HTML string.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        TelegramAuthError
            On 400/401/403 responses or ``ok=false`` JSON body (not retried).
        TelegramNetworkError
            After all retry attempts are exhausted on network/server errors.
        """
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
                raise  # never retry auth errors
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
                    wait = min(wait * 2, 30.0)  # cap at 30 s

        raise TelegramNetworkError(
            f"All {self._max_retries} Telegram send attempts failed. "
            f"Last error: {last_exc}"
        ) from last_exc

    def _attempt_send(self, payload: dict[str, Any]) -> bool:
        """
        Make one HTTP POST to the Telegram API.

        Called by ``_send_with_retry``; may be called multiple times on retry.

        Raises
        ------
        TelegramAuthError
            Immediately on 400/401 (no retry).
        TelegramNetworkError
            On connection errors, timeouts, or unexpected HTTP status codes
            (will be retried by the caller).
        """
        start = time.monotonic()
        try:
            response = requests.post(
                self._url,
                json=payload,
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            log.warning(
                "telegram_timeout",
                timeout_seconds=self._timeout,
                error=str(exc),
            )
            raise TelegramNetworkError(
                f"Telegram request timed out after {self._timeout}s"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            log.warning("telegram_connection_error", error=str(exc))
            raise TelegramNetworkError(
                f"Connection error while reaching Telegram API: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            log.warning("telegram_request_error", error=str(exc))
            raise TelegramNetworkError(
                f"Unexpected requests error: {exc}"
            ) from exc

        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.debug(
            "telegram_response",
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
        )

        # ── Auth / config errors — do NOT retry ───────────────────────────
        if response.status_code in (400, 401, 403):
            description = ""
            try:
                description = response.json().get("description", "")
            except Exception:
                pass
            log.error(
                "telegram_auth_error",
                status_code=response.status_code,
                description=description,
            )
            raise TelegramAuthError(
                f"Telegram API returned {response.status_code}: {description}",
                status_code=response.status_code,
                telegram_description=description,
            )

        # ── Server errors — retry ─────────────────────────────────────────
        if response.status_code >= 500:
            log.warning(
                "telegram_server_error",
                status_code=response.status_code,
            )
            raise TelegramNetworkError(
                f"Telegram server error: HTTP {response.status_code}"
            )

        # ── Unexpected non-200 — retry ────────────────────────────────────
        if response.status_code != 200:
            raise TelegramNetworkError(
                f"Unexpected Telegram status code: {response.status_code}"
            )

        # ── Success ───────────────────────────────────────────────────────
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

        # Telegram returned 200 but ok=false — treat as auth/config error
        description = response.json().get("description", "unknown error")
        log.error("telegram_ok_false", description=description)
        raise TelegramAuthError(
            f"Telegram returned ok=false: {description}",
            status_code=200,
            telegram_description=description,
        )
