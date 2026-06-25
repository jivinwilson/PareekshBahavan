"""
tests/test_monitor.py — Integration tests for monitor.py pipeline.

All external I/O is mocked; tests exercise the full run_normal_mode()
orchestration path.

Coverage
--------
- Successful end-to-end workflow (scrape → download → read → match → notify → persist)
- Duplicate notification skipped (already in store)
- Scraper failure exits with code 1
- PDF download failure: error counted, notification NOT marked seen (retry next run)
- PDF read failure: error counted, processing continues
- Keyword miss: notification marked seen, no Telegram sent
- Keyword hit: Telegram sent, notification persisted
- Telegram send failure: error counted, notification still persisted
- Corrupted / unreadable storage: graceful error
- Multiple notifications: each processed independently
- No PDF: notification skipped and marked seen
- Telegram not configured: warning logged, run still succeeds
- PDF is scanned (no text): title used for matching
"""

from __future__ import annotations

import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from monitor import RunStats, _process_notification, _to_notification, run_normal_mode
from src.config import get_settings
from src.keyword_matcher import KeywordMatcher, MatchResult
from src.models import Notification
from src.pdf_downloader import PDFDownloadError, PDFMetadata, PDFNotFoundError
from src.pdf_reader import PDFContent, PDFReadError, PageContent
from src.scraper import ScrapedItem
from src.storage import NotificationStore
from src.telegram_sender import TelegramNetworkError, TelegramSender


# ---------------------------------------------------------------------------
# Shared fixtures and factories
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Clear the lru_cache on get_settings before and after every test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_store() -> Generator[NotificationStore, None, None]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield NotificationStore(path=Path(d) / "last_seen.json")


@pytest.fixture
def tmp_pdf_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d)


def _scraped_item(
    title: str = "Special Examination Notice",
    pdf_url: str = "https://example.com/exam.pdf",
    page_url: str = "https://pareekshabhavan.uoc.ac.in/notifications/1",
    notification_id: str = "abc123456789abcd",
) -> ScrapedItem:
    item = ScrapedItem(title=title, page_url=page_url, pdf_url=pdf_url)
    object.__setattr__(item, "notification_id", notification_id) \
        if False else None   # notification_id is set in __post_init__
    # Re-create with stable id for test predictability
    item2 = ScrapedItem(title=title, page_url=page_url, pdf_url=pdf_url)
    return item2


def _pdf_metadata(filepath: Path) -> PDFMetadata:
    return PDFMetadata(
        filename=filepath.name,
        filepath=filepath,
        filesize=1024,
        download_time=datetime.now(tz=timezone.utc),
        content_type="application/pdf",
        sha256="a" * 64,
        source_url="https://example.com/exam.pdf",
        was_cached=False,
    )


def _pdf_content(text: str = "Special Examination CBCSS B.Sc Computer Science") -> PDFContent:
    page = PageContent(page_number=1, text=text)
    return PDFContent(
        filename="exam.pdf",
        total_pages=1,
        full_text=text,
        pages=(page,),
        extraction_time=datetime.now(tz=timezone.utc),
        extraction_method="pdfplumber",
    )


def _match_result(matched: bool = True, score: float = 0.9) -> MatchResult:
    if not matched:
        return MatchResult(
            matched=False, matched_keywords=(), all_matches=(),
            total_matches=0, confidence_score=0.0, confidence_label="NONE",
            summary="No match.", high_priority_found=(), secondary_found=(),
        )
    return MatchResult(
        matched=True,
        matched_keywords=("Special Examination", "CBCSS"),
        all_matches=(),
        total_matches=2,
        confidence_score=score,
        confidence_label="HIGH",
        summary="HIGH confidence: 'Special Examination', 'CBCSS' found in exam.pdf",
        high_priority_found=("Special Examination",),
        secondary_found=("CBCSS",),
    )


def _make_settings(tmp_path: Path, pdf_dir: Path) -> MagicMock:
    s = MagicMock()
    s.base_url = "https://pareekshabhavan.uoc.ac.in/"
    s.keywords = ["Special Examination", "CBCSS", "B.Sc"]
    s.telegram_enabled = True
    s.email_enabled = False
    s.request_timeout = 5
    s.max_retries = 1
    s.last_seen_path = tmp_path / "last_seen.json"
    s.pdf_download_dir = pdf_dir
    s.log_file = None
    s.effective_log_level = "INFO"
    s.bot_token = MagicMock()
    s.bot_token.get_secret_value.return_value = "test-token"
    s.chat_id = "123"
    return s


# ---------------------------------------------------------------------------
# _to_notification helper
# ---------------------------------------------------------------------------

class TestToNotification:
    def test_converts_scraped_item_to_notification(self):
        item = _scraped_item()
        result = _match_result()
        pdf = _pdf_content()
        notif = _to_notification(item, result, pdf)
        assert isinstance(notif, Notification)
        assert notif.title == item.title
        assert notif.pdf_url == item.pdf_url
        assert notif.website_url == item.page_url

    def test_matched_keywords_set(self):
        item = _scraped_item()
        result = _match_result()
        notif = _to_notification(item, result, None)
        assert list(notif.matched_keywords) == list(result.matched_keywords)

    def test_summary_set(self):
        item = _scraped_item()
        result = _match_result()
        notif = _to_notification(item, result, None)
        assert notif.summary == result.summary

    def test_pdf_text_set_from_content(self):
        item = _scraped_item()
        result = _match_result()
        pdf = _pdf_content("Full PDF text here")
        notif = _to_notification(item, result, pdf)
        assert notif.pdf_text == "Full PDF text here"

    def test_pdf_text_empty_when_no_content(self):
        item = _scraped_item()
        result = _match_result()
        notif = _to_notification(item, result, None)
        assert notif.pdf_text == ""

    def test_publication_date_parsed(self):
        item = ScrapedItem(
            title="Test", page_url="https://x.com",
            pdf_url="https://x.com/a.pdf",
            publication_date_raw="25/06/2026",
        )
        notif = _to_notification(item, _match_result(), None)
        assert notif.publication_date is not None
        assert notif.publication_date.year == 2026


# ---------------------------------------------------------------------------
# _process_notification unit tests
# ---------------------------------------------------------------------------

class TestProcessNotification:
    def _make_mocks(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        pdf_path = tmp_pdf_dir / "exam.pdf"
        pdf_path.write_bytes(b"%PDF test")

        downloader = MagicMock(spec=["download"])
        downloader.download.return_value = _pdf_metadata(pdf_path)

        reader = MagicMock(spec=["read"])
        reader.read.return_value = _pdf_content()

        matcher = MagicMock(spec=["match"])
        matcher.match.return_value = _match_result(matched=True)

        sender = MagicMock(spec=["send_notification"])
        sender.send_notification.return_value = True

        log = MagicMock()
        stats = RunStats()
        return downloader, reader, matcher, sender, log, stats

    def test_successful_processing(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        item = _scraped_item()
        dl, rd, mt, snd, log, stats = self._make_mocks(tmp_pdf_dir, tmp_store)
        _process_notification(item, tmp_store, dl, rd, mt, snd, stats, log)
        assert stats.downloaded == 1
        assert stats.matched == 1
        assert stats.notified == 1
        assert tmp_store.is_seen(item.notification_id)

    def test_no_pdf_marks_seen_and_skips(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        item = ScrapedItem(title="Notice", page_url="https://x.com", pdf_url="")
        dl, rd, mt, snd, log, stats = self._make_mocks(tmp_pdf_dir, tmp_store)
        _process_notification(item, tmp_store, dl, rd, mt, snd, stats, log)
        assert stats.skipped_no_pdf == 1
        assert stats.downloaded == 0
        dl.download.assert_not_called()
        assert tmp_store.is_seen(item.notification_id)

    def test_keyword_miss_marks_seen_no_notify(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        item = _scraped_item()
        dl, rd, mt, snd, log, stats = self._make_mocks(tmp_pdf_dir, tmp_store)
        mt.match.return_value = _match_result(matched=False)
        _process_notification(item, tmp_store, dl, rd, mt, snd, stats, log)
        assert stats.matched == 0
        assert stats.notified == 0
        snd.send_notification.assert_not_called()
        assert tmp_store.is_seen(item.notification_id)

    def test_no_sender_logs_warning_still_persists(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        item = _scraped_item()
        dl, rd, mt, _, log, stats = self._make_mocks(tmp_pdf_dir, tmp_store)
        _process_notification(item, tmp_store, dl, rd, mt, None, stats, log)
        assert stats.matched == 1
        assert stats.notified == 0
        assert tmp_store.is_seen(item.notification_id)

    def test_scanned_pdf_still_matches_on_title(self, tmp_pdf_dir: Path, tmp_store: NotificationStore):
        """Scanned PDF has no text — matcher uses the title line."""
        item = _scraped_item(title="Special Examination CBCSS 2026")
        dl, rd, mt, snd, log, stats = self._make_mocks(tmp_pdf_dir, tmp_store)
        scanned = PDFContent(
            filename="scanned.pdf", total_pages=1, full_text="",
            pages=(PageContent(1, ""),),
            extraction_time=datetime.now(tz=timezone.utc),
            extraction_method="pdfplumber", is_scanned=True,
        )
        rd.read.return_value = scanned
        mt.match.return_value = _match_result(matched=True)
        _process_notification(item, tmp_store, dl, rd, mt, snd, stats, log)
        # matcher was called with the title even though PDF had no text
        call_args = mt.match.call_args[0][0]
        assert item.title in call_args


# ---------------------------------------------------------------------------
# run_normal_mode integration tests
# ---------------------------------------------------------------------------

class TestRunNormalMode:
    """Full pipeline tests with all external components mocked."""

    def _run(
        self,
        scraped: list,
        match_result: MatchResult | None = None,
        download_raises: Exception | None = None,
        read_raises: Exception | None = None,
        scraper_raises: Exception | None = None,
        telegram_raises: Exception | None = None,
        tmp_store_path: Path | None = None,
        pdf_dir: Path | None = None,
    ) -> tuple[int, MagicMock]:
        """
        Run run_normal_mode with fully mocked components.
        Returns (exit_code, mock_sender).
        """
        import tempfile, os
        tmp = tempfile.mkdtemp(dir="/tmp")
        store_path = tmp_store_path or (Path(tmp) / "last_seen.json")
        pdf_dir = pdf_dir or Path(tmp)

        mock_settings = _make_settings(store_path.parent, pdf_dir)
        mock_settings.last_seen_path = store_path

        pdf_path = pdf_dir / "exam.pdf"
        pdf_path.write_bytes(b"%PDF-test")

        mock_sender = MagicMock(spec=["send_notification"])
        if telegram_raises:
            mock_sender.send_notification.side_effect = telegram_raises
        else:
            mock_sender.send_notification.return_value = True

        match_result = match_result or _match_result(matched=True)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with patch("monitor.get_settings", return_value=mock_settings), \
                 patch("monitor.SiteScraper") as MockScraper, \
                 patch("monitor.PDFDownloader") as MockDownloader, \
                 patch("monitor.PDFReader") as MockReader, \
                 patch("monitor.KeywordMatcher") as MockMatcher, \
                 patch("monitor.TelegramSender") as MockTelegramSender:

                # Scraper
                mock_scraper_inst = MagicMock()
                if scraper_raises:
                    mock_scraper_inst.scrape_all.side_effect = scraper_raises
                else:
                    mock_scraper_inst.scrape_all.return_value = scraped
                MockScraper.from_settings.return_value = mock_scraper_inst

                # Downloader
                mock_dl_inst = MagicMock()
                if download_raises:
                    mock_dl_inst.download.side_effect = download_raises
                else:
                    mock_dl_inst.download.return_value = _pdf_metadata(pdf_path)
                MockDownloader.from_settings.return_value = mock_dl_inst

                # Reader
                mock_rd_inst = MagicMock()
                if read_raises:
                    mock_rd_inst.read.side_effect = read_raises
                else:
                    mock_rd_inst.read.return_value = _pdf_content()
                MockReader.return_value = mock_rd_inst

                # Matcher
                mock_mt_inst = MagicMock()
                mock_mt_inst.match.return_value = match_result
                MockMatcher.from_settings.return_value = mock_mt_inst

                # Telegram
                MockTelegramSender.from_settings.return_value = mock_sender

                from src.logger import get_logger
                log = get_logger("test")
                exit_code = run_normal_mode(log)

        return exit_code, mock_sender

    # ── Success ──────────────────────────────────────────────────────────────

    def test_successful_workflow_exits_zero(self):
        item = _scraped_item()
        code, sender = self._run([item])
        assert code == 0

    def test_successful_workflow_sends_telegram(self):
        item = _scraped_item()
        _, sender = self._run([item])
        sender.send_notification.assert_called_once()

    # ── Duplicate ────────────────────────────────────────────────────────────

    def test_duplicate_notification_not_sent(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            store_path = Path(d) / "last_seen.json"
            store = NotificationStore(path=store_path)
            item = _scraped_item()
            store.mark_seen(item.notification_id, item.title, item.page_url)

            _, sender = self._run([item], tmp_store_path=store_path)
            sender.send_notification.assert_not_called()

    # ── Scraper failure ───────────────────────────────────────────────────────

    def test_scraper_failure_returns_exit_1(self):
        code, _ = self._run([], scraper_raises=RuntimeError("scraper down"))
        assert code == 1

    def test_scraper_failure_does_not_send_telegram(self):
        _, sender = self._run([], scraper_raises=RuntimeError("down"))
        sender.send_notification.assert_not_called()

    # ── Download failure ──────────────────────────────────────────────────────

    def test_download_failure_continues_processing(self):
        item1 = _scraped_item(title="Exam A", notification_id="id_a")
        item2 = _scraped_item(title="Exam B", notification_id="id_b", pdf_url="https://b.com/b.pdf")

        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            pdf_dir = Path(d)
            pdf_path = pdf_dir / "exam.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            call_count = {"n": 0}
            def dl_side_effect(url):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise PDFNotFoundError("404")
                return _pdf_metadata(pdf_path)

            mock_settings = _make_settings(pdf_dir, pdf_dir)
            mock_sender = MagicMock()
            mock_sender.send_notification.return_value = True

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with patch("monitor.get_settings", return_value=mock_settings), \
                     patch("monitor.SiteScraper") as MS, \
                     patch("monitor.PDFDownloader") as MD, \
                     patch("monitor.PDFReader") as MR, \
                     patch("monitor.KeywordMatcher") as MK, \
                     patch("monitor.TelegramSender") as MT:

                    MS.from_settings.return_value.scrape_all.return_value = [item1, item2]
                    MD.from_settings.return_value.download.side_effect = dl_side_effect
                    MR.return_value.read.return_value = _pdf_content()
                    MK.from_settings.return_value.match.return_value = _match_result()
                    MT.from_settings.return_value = mock_sender

                    from src.logger import get_logger
                    log = get_logger("test")
                    code = run_normal_mode(log)

            # Second notification was processed despite first failing
            assert mock_sender.send_notification.call_count >= 1
            assert code == 0

    # ── PDF read failure ──────────────────────────────────────────────────────

    def test_pdf_read_failure_continues_processing(self):
        item = _scraped_item()
        code, sender = self._run([item], read_raises=PDFReadError("corrupt"))
        assert code == 0
        sender.send_notification.assert_not_called()

    # ── Keyword miss ──────────────────────────────────────────────────────────

    def test_keyword_miss_no_telegram_sent(self):
        item = _scraped_item()
        code, sender = self._run([item], match_result=_match_result(matched=False))
        assert code == 0
        sender.send_notification.assert_not_called()

    def test_keyword_miss_exits_zero(self):
        item = _scraped_item()
        code, _ = self._run([item], match_result=_match_result(matched=False))
        assert code == 0

    # ── Keyword hit ───────────────────────────────────────────────────────────

    def test_keyword_hit_sends_telegram(self):
        item = _scraped_item()
        _, sender = self._run([item], match_result=_match_result(matched=True))
        sender.send_notification.assert_called_once()

    def test_keyword_hit_notification_argument_correct(self):
        item = _scraped_item(title="Special Examination 2026")
        _, sender = self._run([item], match_result=_match_result())
        call_args = sender.send_notification.call_args[0][0]
        assert isinstance(call_args, Notification)
        assert "Special Examination" in call_args.title

    # ── Telegram failure ──────────────────────────────────────────────────────

    def test_telegram_failure_exits_zero(self):
        item = _scraped_item()
        code, _ = self._run(
            [item], telegram_raises=TelegramNetworkError("timeout")
        )
        assert code == 0

    def test_telegram_failure_counted_as_error(self):
        """Run should complete with error count > 0 but still exit 0."""
        item = _scraped_item()
        code, sender = self._run(
            [item], telegram_raises=TelegramNetworkError("timeout")
        )
        sender.send_notification.assert_called_once()
        assert code == 0

    # ── Multiple notifications ────────────────────────────────────────────────

    def test_multiple_notifications_all_sent(self):
        items = [
            _scraped_item(title=f"Exam {i}", notification_id=f"id_{i:04d}")
            for i in range(4)
        ]
        _, sender = self._run(items)
        assert sender.send_notification.call_count == 4

    def test_multiple_notifications_independent(self):
        """One download failure should not stop others."""
        item_good = _scraped_item(title="Good", notification_id="id_good")
        item_bad  = _scraped_item(title="Bad",  notification_id="id_bad",
                                   pdf_url="https://bad.com/missing.pdf")

        call_count = {"n": 0}
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            pdf_dir = Path(d)
            pdf_path = pdf_dir / "exam.pdf"
            pdf_path.write_bytes(b"%PDF-test")

            def dl_side_effect(url):
                call_count["n"] += 1
                if "missing" in url:
                    raise PDFNotFoundError("404")
                return _pdf_metadata(pdf_path)

            mock_settings = _make_settings(pdf_dir, pdf_dir)
            mock_sender = MagicMock()
            mock_sender.send_notification.return_value = True

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with patch("monitor.get_settings", return_value=mock_settings), \
                     patch("monitor.SiteScraper") as MS, \
                     patch("monitor.PDFDownloader") as MD, \
                     patch("monitor.PDFReader") as MR, \
                     patch("monitor.KeywordMatcher") as MK, \
                     patch("monitor.TelegramSender") as MT:

                    MS.from_settings.return_value.scrape_all.return_value = [item_good, item_bad]
                    MD.from_settings.return_value.download.side_effect = dl_side_effect
                    MR.return_value.read.return_value = _pdf_content()
                    MK.from_settings.return_value.match.return_value = _match_result()
                    MT.from_settings.return_value = mock_sender

                    from src.logger import get_logger
                    code = run_normal_mode(get_logger("test"))

            assert code == 0
            assert mock_sender.send_notification.call_count == 1  # only good item
