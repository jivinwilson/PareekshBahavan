"""
monitor.py — Entry point for the Pareeksha Bhavan monitor.

This file is intentionally thin: it parses CLI arguments, bootstraps
logging and settings, then delegates to the appropriate run mode.

Run modes
---------
Normal (default)
    python monitor.py
    Full monitoring cycle: scrape → match → notify → persist.
    Implemented incrementally in Phases 4-6.

Test mode
    python monitor.py --test
    Sends a single Telegram test message to verify bot credentials and
    connectivity.  Does NOT run the scraper or touch last_seen.json.
    Ideal for verifying GitHub Actions secrets before the first real run.

Called by
---------
    - GitHub Actions (monitor.yml) on the 6-hour cron schedule
    - GitHub Actions manual dispatch with test_mode=true
    - Locally: python monitor.py [--test] [--debug]
"""

from __future__ import annotations

import argparse
import sys
import warnings

from src.config import get_settings
from src.logger import configure_logging, get_logger
from src.telegram_sender import (
    TelegramNotConfiguredError,
    TelegramSender,
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Pareeksha Bhavan notification monitor",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        default=False,
        help=(
            "Test mode: send a Telegram connectivity message and exit. "
            "Does not run the scraper."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging (overrides settings).",
    )
    return parser


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_test_mode(log) -> int:
    """
    Send a single Telegram test message and exit.

    Returns
    -------
    int
        0 on success, 1 on failure.
    """
    log.info("monitor_test_mode_start", action="send_telegram_test_message")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = get_settings()

    if not settings.telegram_enabled:
        log.error(
            "telegram_not_configured",
            reason="BOT_TOKEN or CHAT_ID is missing",
            hint="Set BOT_TOKEN and CHAT_ID as environment variables or GitHub Secrets",
        )
        return 1

    log.info(
        "telegram_credentials_found",
        chat_id=settings.chat_id,
        bot_token_length=len(settings.bot_token.get_secret_value()),
    )

    try:
        sender = TelegramSender.from_settings(settings)
        log.info("telegram_sender_created", timeout=settings.request_timeout)

        log.info("telegram_sending_test_message")
        sender.send_test_message()

        log.info(
            "telegram_test_message_sent",
            status="success",
            message="Telegram integration is working correctly",
        )
        return 0

    except TelegramNotConfiguredError as exc:
        log.error("telegram_not_configured", error=str(exc))
        return 1
    except Exception as exc:
        log.error(
            "telegram_test_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 1


def run_normal_mode(log) -> int:
    """
    Run a full monitoring cycle.

    Currently a scaffold -- scraping, matching, and persisting are implemented
    in Phase 4 and beyond.

    Returns
    -------
    int
        0 on success, 1 on unrecoverable error.
    """
    log.info("monitor_run_start", mode="normal")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = get_settings()

    log.info(
        "monitor_config_loaded",
        base_url=settings.base_url,
        keyword_count=len(settings.keywords),
        telegram_enabled=settings.telegram_enabled,
        email_enabled=settings.email_enabled,
        last_seen_path=str(settings.last_seen_path),
    )

    # Future phases slot in here:
    #   Phase 4:  items   = scrape(settings)
    #   Phase 4:  matches = match_keywords(items, settings)
    #   Phase 5:  notify_telegram(matches, settings)
    #   Phase 5:  notify_email(matches, settings)
    #   Phase 6:  persist_seen(matches, settings)

    log.info("monitor_run_complete", new_notifications=0)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Parse arguments, configure logging, and dispatch to the correct run mode.

    Returns
    -------
    int
        Exit code: 0 = success, 1 = failure.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Bootstrap logging before anything else so every subsequent call is logged
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = get_settings()

    log_level = "DEBUG" if args.debug else settings.effective_log_level
    configure_logging(
        level=log_level,
        log_file=settings.log_file,
    )
    log = get_logger(__name__)

    log.info(
        "monitor_startup",
        mode="test" if args.test else "normal",
        log_level=log_level,
        python_argv=sys.argv,
    )

    if args.test:
        return run_test_mode(log)

    return run_normal_mode(log)


if __name__ == "__main__":
    sys.exit(main())
