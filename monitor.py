"""
monitor.py — Entry point for the Pareeksha Bhavan monitor.

Run modes
---------
Normal (default)
    python monitor.py
    Full pipeline: scrape → download PDF → extract text → match keywords
    → send Telegram → persist seen-state.

Test mode
    python monitor.py --test
    Sends a single Telegram test message and exits.  No scraping.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src.config import get_settings
from src.keyword_matcher import KeywordMatcher, MatchResult
from src.logger import configure_logging, get_logger
from src.models import Notification
from src.pdf_downloader import PDFDownloader, PDFDownloadError
from src.pdf_reader import PDFContent, PDFReadError, PDFReader
from src.scraper import ScrapedItem, SiteScraper
from src.storage import NotificationStore
from src.telegram_sender import TelegramNotConfiguredError, TelegramSender
from src.utils import parse_date

if TYPE_CHECKING:
    from src.config import Settings


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
        help="Test mode: send a Telegram test message and exit. No scraping.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging (overrides settings).",
    )
    return parser


# ---------------------------------------------------------------------------
# Pipeline result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    """Counters accumulated during one monitoring run."""
    scraped:     int = 0
    skipped_seen: int = 0
    skipped_no_pdf: int = 0
    downloaded:  int = 0
    matched:     int = 0
    notified:    int = 0
    errors:      int = 0

    def log_summary(self, log) -> None:
        log.info(
            "monitor_run_summary",
            scraped=self.scraped,
            skipped_seen=self.skipped_seen,
            skipped_no_pdf=self.skipped_no_pdf,
            downloaded=self.downloaded,
            matched=self.matched,
            notified=self.notified,
            errors=self.errors,
        )


# ---------------------------------------------------------------------------
# Helper: ScrapedItem → Notification
# ---------------------------------------------------------------------------

def _to_notification(
    item: ScrapedItem,
    result: MatchResult,
    pdf_content: PDFContent | None,
) -> Notification:
    """Convert a matched ScrapedItem into a Notification for the sender."""
    pub_date = parse_date(item.publication_date_raw) if item.publication_date_raw else None
    return Notification(
        title=item.title,
        website_url=item.page_url,
        publication_date=pub_date,
        pdf_url=item.pdf_url,
        summary=result.summary,
        matched_keywords=list(result.matched_keywords),
        pdf_text=pdf_content.full_text if pdf_content else "",
        notification_id=item.notification_id,
    )


# ---------------------------------------------------------------------------
# Single-notification processor
# ---------------------------------------------------------------------------

def _process_notification(
    item: ScrapedItem,
    store: NotificationStore,
    downloader: PDFDownloader,
    reader: PDFReader,
    matcher: KeywordMatcher,
    sender: TelegramSender | None,
    stats: RunStats,
    log,
) -> None:
    """
    Process one scraped notification through the full pipeline.

    Any exception raised inside is caught by the caller; this function
    only raises when logic errors (bugs) occur, not on expected failures.
    """
    log.info(
        "notification_processing",
        notification_id=item.notification_id,
        title=item.title[:80],
        has_pdf=item.has_pdf,
    )

    # ── Step 1: Skip if no PDF ────────────────────────────────────────────
    if not item.has_pdf:
        log.info("notification_skip_no_pdf", title=item.title[:80])
        store.mark_seen(item.notification_id, item.title, item.page_url)
        stats.skipped_no_pdf += 1
        return

    # ── Step 2: Download PDF ──────────────────────────────────────────────
    pdf_meta = downloader.download(item.pdf_url)
    stats.downloaded += 1
    log.info(
        "pdf_downloaded",
        filename=pdf_meta.filename,
        size_kb=round(pdf_meta.size_kb, 1),
        cached=pdf_meta.was_cached,
    )

    # ── Step 3: Read PDF text ─────────────────────────────────────────────
    pdf_content = reader.read(pdf_meta.filepath)
    if pdf_content.is_scanned:
        log.warning(
            "pdf_scanned_skipping_match",
            filename=pdf_meta.filename,
        )

    # ── Step 4: Match keywords ────────────────────────────────────────────
    # Search both the notification title and the PDF full text so a match
    # in the title alone (when the PDF is scanned) can still trigger.
    search_text = f"{item.title}\n{pdf_content.full_text}"
    result = matcher.match(search_text, item.title)

    log.info(
        "keyword_match_result",
        title=item.title[:80],
        matched=result.matched,
        score=result.confidence_score,
        label=result.confidence_label,
        keywords=list(result.matched_keywords),
    )

    if not result.matched:
        # Mark as seen so we don't re-check on the next run
        store.mark_seen(item.notification_id, item.title, item.page_url)
        log.info("notification_no_match", title=item.title[:80])
        return

    stats.matched += 1

    # ── Step 5: Send Telegram ─────────────────────────────────────────────
    notification = _to_notification(item, result, pdf_content)

    if sender is None:
        log.warning(
            "telegram_not_configured_skip_notify",
            title=item.title[:80],
            hint="Set BOT_TOKEN and CHAT_ID to enable Telegram notifications",
        )
    else:
        sender.send_notification(notification)
        stats.notified += 1
        log.info("telegram_notification_sent", title=item.title[:80])

    # ── Step 6: Persist ───────────────────────────────────────────────────
    store.mark_seen(
        item.notification_id,
        item.title,
        item.pdf_url or item.page_url,
    )
    log.info("notification_persisted", notification_id=item.notification_id)


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_normal_mode(log) -> int:
    """
    Execute one full monitoring cycle.

    Returns
    -------
    int
        0 on success (even if some notifications had errors).
        1 on unrecoverable startup failure (bad config, unreachable storage).
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
        last_seen_path=str(settings.last_seen_path),
    )

    # ── Initialise components ─────────────────────────────────────────────
    try:
        store      = NotificationStore(path=settings.last_seen_path)
        scraper    = SiteScraper.from_settings(settings)
        downloader = PDFDownloader.from_settings(settings)
        reader     = PDFReader()
        matcher    = KeywordMatcher.from_settings(settings)
    except Exception as exc:
        log.error("monitor_init_failed", error=str(exc), error_type=type(exc).__name__)
        return 1

    # Telegram sender — optional; warn but continue if not configured
    sender: TelegramSender | None = None
    if settings.telegram_enabled:
        try:
            sender = TelegramSender.from_settings(settings)
        except TelegramNotConfiguredError as exc:
            log.warning("telegram_sender_init_failed", error=str(exc))

    store.update_last_checked()

    # ── Scrape ────────────────────────────────────────────────────────────
    log.info("scraper_starting", base_url=settings.base_url)
    try:
        scraped_items = scraper.scrape_all()
    except Exception as exc:
        log.error("scraper_failed", error=str(exc), error_type=type(exc).__name__)
        return 1

    stats = RunStats(scraped=len(scraped_items))
    log.info("scraper_complete", total_items=len(scraped_items))

    # ── Process each notification ─────────────────────────────────────────
    for item in scraped_items:
        # Duplicate check — skip immediately if already seen
        if store.is_seen(item.notification_id):
            log.debug("notification_already_seen", notification_id=item.notification_id)
            stats.skipped_seen += 1
            continue

        try:
            _process_notification(
                item=item,
                store=store,
                downloader=downloader,
                reader=reader,
                matcher=matcher,
                sender=sender,
                stats=stats,
                log=log,
            )
        except PDFDownloadError as exc:
            log.error(
                "pdf_download_failed",
                notification_id=item.notification_id,
                url=item.pdf_url,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            stats.errors += 1
            # Don't mark as seen — retry on next run when the PDF may be available
        except PDFReadError as exc:
            log.error(
                "pdf_read_failed",
                notification_id=item.notification_id,
                error=str(exc),
            )
            stats.errors += 1
        except Exception as exc:
            log.error(
                "notification_unexpected_error",
                notification_id=item.notification_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            stats.errors += 1

    stats.log_summary(log)
    log.info("monitor_run_complete", new_matched=stats.matched)
    return 0


def run_test_mode(log) -> int:
    """Send a Telegram test message and exit."""
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
        bot_token_length=len(settings.bot_token.get_secret_value()),  # type: ignore[union-attr]
    )

    try:
        sender = TelegramSender.from_settings(settings)
        log.info("telegram_sender_created", timeout=settings.request_timeout)
        log.info("telegram_sending_test_message")
        sender.send_test_message()
        log.info("telegram_test_message_sent", status="success")
        return 0
    except TelegramNotConfiguredError as exc:
        log.error("telegram_not_configured", error=str(exc))
        return 1
    except Exception as exc:
        log.error("telegram_test_failed", error=str(exc), error_type=type(exc).__name__)
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = get_settings()

    log_level = "DEBUG" if args.debug else settings.effective_log_level
    configure_logging(level=log_level, log_file=settings.log_file)
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
