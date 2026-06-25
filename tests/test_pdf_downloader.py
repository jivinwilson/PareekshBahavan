"""
tests/test_pdf_downloader.py — Unit tests for src/pdf_downloader.py

All HTTP calls are mocked with the ``responses`` library.
Filesystem operations use /tmp directories (not pytest tmp_path) to
avoid the null-byte cleanup issue on the mounted Windows filesystem.

Coverage
--------
PDFMetadata
  - frozen dataclass
  - size_kb / size_mb properties
  - was_cached default

PDFDownloader.download()
  - Successful first-time download
  - Duplicate download (cache hit, no HTTP request)
  - Invalid PDF (wrong magic bytes)
  - Invalid PDF (HTML content-type from HEAD)
  - Invalid PDF (HTML content-type from GET response)
  - Empty response body
  - Timeout on HEAD
  - Timeout on GET
  - Connection error
  - 404 Not Found
  - 410 Gone
  - File too large (Content-Length header)
  - File too large (streaming exceeds limit)
  - Retry on network error, succeeds on 3rd attempt
  - Retry exhausted raises PDFNetworkError
  - Permission error on directory creation
  - from_settings() factory
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses as resp_lib

from src.pdf_downloader import (
    CHUNK_SIZE,
    MAX_PDF_BYTES,
    PDF_MAGIC,
    PDFDownloadError,
    PDFDownloader,
    PDFInvalidError,
    PDFMetadata,
    PDFNetworkError,
    PDFNotFoundError,
    PDFPermissionError,
    PDFTooLargeError,
)

PDF_URL = "https://pareekshabhavan.uoc.ac.in/uploads/special_exam.pdf"
VALID_PDF_BODY = b"%PDF-1.4 1 0 obj\n<< /Type /Catalog >>\nendobj\n"
HTML_BODY = b"<html><body><h1>404 Not Found</h1></body></html>"


@pytest.fixture
def download_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d) / "pdfs"


@pytest.fixture
def downloader(download_dir: Path) -> PDFDownloader:
    return PDFDownloader(
        download_dir=download_dir,
        timeout=5,
        max_retries=3,
        wait_seconds=0,
    )


# ---------------------------------------------------------------------------
# PDFMetadata model
# ---------------------------------------------------------------------------

class TestPDFMetadata:
    def _make(self, **kw) -> PDFMetadata:
        defaults = dict(
            filename="abc.pdf",
            filepath=Path("/tmp/abc.pdf"),
            filesize=1024,
            download_time=datetime.now(tz=timezone.utc),
            content_type="application/pdf",
            sha256="a" * 64,
            source_url=PDF_URL,
        )
        defaults.update(kw)
        return PDFMetadata(**defaults)

    def test_frozen(self):
        m = self._make()
        with pytest.raises(Exception):
            m.filename = "other.pdf"  # type: ignore[misc]

    def test_size_kb(self):
        m = self._make(filesize=2048)
        assert m.size_kb == 2.0

    def test_size_mb(self):
        m = self._make(filesize=1024 * 1024)
        assert m.size_mb == 1.0

    def test_was_cached_default_false(self):
        m = self._make()
        assert m.was_cached is False


# ---------------------------------------------------------------------------
# Successful download
# ---------------------------------------------------------------------------

class TestSuccessfulDownload:
    @resp_lib.activate
    def test_download_success_returns_metadata(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf", "Content-Length": str(len(VALID_PDF_BODY))})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        assert isinstance(meta, PDFMetadata)
        assert meta.was_cached is False

    @resp_lib.activate
    def test_download_success_file_exists(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        assert meta.filepath.exists()

    @resp_lib.activate
    def test_download_success_valid_sha256(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        expected = hashlib.sha256(VALID_PDF_BODY).hexdigest()
        assert meta.sha256 == expected

    @resp_lib.activate
    def test_download_success_correct_filesize(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        assert meta.filesize == len(VALID_PDF_BODY)

    @resp_lib.activate
    def test_download_deterministic_filename(self, downloader: PDFDownloader):
        """Same URL always produces the same filename."""
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        expected_stem = hashlib.sha256(PDF_URL.encode()).hexdigest()[:32]
        assert meta.filename == f"{expected_stem}.pdf"

    @resp_lib.activate
    def test_download_creates_directory(self, download_dir: Path):
        """Directory is created automatically if it does not exist."""
        assert not download_dir.exists()
        dl = PDFDownloader(download_dir=download_dir, timeout=5, max_retries=1, wait_seconds=0)
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        dl.download(PDF_URL)
        assert download_dir.exists()


# ---------------------------------------------------------------------------
# Duplicate / cache hit
# ---------------------------------------------------------------------------

class TestCacheHit:
    @resp_lib.activate
    def test_duplicate_returns_cached(self, downloader: PDFDownloader):
        """Second call with same URL returns cached result, no new HTTP request."""
        # First download
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        downloader.download(PDF_URL)

        call_count_after_first = len(resp_lib.calls)

        # Second call — must NOT make new HTTP requests
        meta2 = downloader.download(PDF_URL)
        assert meta2.was_cached is True
        assert len(resp_lib.calls) == call_count_after_first  # no new calls

    @resp_lib.activate
    def test_cached_metadata_has_correct_fields(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        downloader.download(PDF_URL)
        meta = downloader.download(PDF_URL)
        assert meta.filename.endswith(".pdf")
        assert meta.filepath.exists()
        assert meta.filesize == len(VALID_PDF_BODY)


# ---------------------------------------------------------------------------
# Invalid PDF
# ---------------------------------------------------------------------------

class TestInvalidPDF:
    @resp_lib.activate
    def test_html_content_type_from_head_raises(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "text/html; charset=utf-8"})
        with pytest.raises(PDFInvalidError, match="text/html"):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_html_content_type_from_get_raises(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=HTML_BODY, status=200,
                     headers={"Content-Type": "text/html"})
        with pytest.raises(PDFInvalidError):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_wrong_magic_bytes_raises(self, downloader: PDFDownloader):
        """A binary file that starts with wrong magic bytes."""
        bad_body = b"\x89PNG\r\n" + b"\x00" * 100  # PNG header
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=bad_body, status=200,
                     headers={"Content-Type": "application/pdf"})
        with pytest.raises(PDFInvalidError, match="magic bytes"):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_empty_body_raises(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=b"", status=200,
                     headers={"Content-Type": "application/pdf"})
        with pytest.raises(PDFInvalidError, match="empty"):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_wrong_magic_temp_file_cleaned_up(self, downloader: PDFDownloader, download_dir: Path):
        """No orphan temp files left after a failed magic-byte check."""
        bad_body = b"NOT A PDF AT ALL"
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=bad_body, status=200,
                     headers={"Content-Type": "application/pdf"})
        with pytest.raises(PDFInvalidError):
            downloader.download(PDF_URL)
        # download_dir may not even exist yet; if it does, no .tmp files
        if download_dir.exists():
            tmp_files = list(download_dir.glob("*.tmp"))
            assert tmp_files == [], f"Orphan temp files: {tmp_files}"


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------

class TestHTTPErrors:
    @resp_lib.activate
    def test_404_raises_not_found(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=404)
        with pytest.raises(PDFNotFoundError):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_410_raises_not_found(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=410)
        with pytest.raises(PDFNotFoundError):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_404_on_get_raises_not_found(self, downloader: PDFDownloader):
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, status=404)
        with pytest.raises(PDFNotFoundError):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_500_raises_network_error(self, downloader: PDFDownloader):
        for _ in range(3):
            resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                         headers={"Content-Type": "application/pdf"})
            resp_lib.add(resp_lib.GET, PDF_URL, status=500)
        with pytest.raises(PDFNetworkError):
            downloader.download(PDF_URL)


# ---------------------------------------------------------------------------
# Network failures
# ---------------------------------------------------------------------------

class TestNetworkFailures:
    @resp_lib.activate
    def test_head_timeout_retried(self, downloader: PDFDownloader):
        """Timeout on HEAD is retried; succeeds on third attempt."""
        resp_lib.add(resp_lib.HEAD, PDF_URL, body=requests.exceptions.Timeout())
        resp_lib.add(resp_lib.HEAD, PDF_URL, body=requests.exceptions.Timeout())
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        assert meta.was_cached is False

    @resp_lib.activate
    def test_get_timeout_raises_after_retries(self, downloader: PDFDownloader):
        for _ in range(3):
            resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                         headers={"Content-Type": "application/pdf"})
            resp_lib.add(resp_lib.GET, PDF_URL,
                         body=requests.exceptions.Timeout("timed out"))
        dl = PDFDownloader(download_dir=downloader._download_dir,
                           timeout=5, max_retries=3, wait_seconds=0)
        with pytest.raises(PDFNetworkError, match="Failed to download"):
            dl.download(PDF_URL)

    @resp_lib.activate
    def test_connection_error_retried(self, downloader: PDFDownloader):
        """Connection error is retried; succeeds on second attempt."""
        resp_lib.add(resp_lib.HEAD, PDF_URL,
                     body=requests.exceptions.ConnectionError("refused"))
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=VALID_PDF_BODY, status=200,
                     headers={"Content-Type": "application/pdf"})
        meta = downloader.download(PDF_URL)
        assert meta.was_cached is False

    @resp_lib.activate
    def test_retry_exhausted_raises_network_error(self, downloader: PDFDownloader):
        for _ in range(3):
            resp_lib.add(resp_lib.HEAD, PDF_URL,
                         body=requests.exceptions.ConnectionError())
        with pytest.raises(PDFNetworkError, match="3 attempts"):
            downloader.download(PDF_URL)


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------

class TestSizeLimit:
    @resp_lib.activate
    def test_content_length_too_large_raises(self, downloader: PDFDownloader):
        """Content-Length header exceeds limit — rejected before downloading."""
        big_size = MAX_PDF_BYTES + 1
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf",
                              "Content-Length": str(big_size)})
        with pytest.raises(PDFTooLargeError):
            downloader.download(PDF_URL)

    @resp_lib.activate
    def test_streaming_too_large_raises(self, download_dir: Path):
        """Body exceeds limit during streaming."""
        small_limit = 10
        dl = PDFDownloader(download_dir=download_dir, timeout=5,
                           max_retries=1, wait_seconds=0,
                           max_size_bytes=small_limit)
        big_body = PDF_MAGIC + b"X" * 100  # 104 bytes > 10 byte limit
        resp_lib.add(resp_lib.HEAD, PDF_URL, status=200,
                     headers={"Content-Type": "application/pdf"})
        resp_lib.add(resp_lib.GET, PDF_URL, body=big_body, status=200,
                     headers={"Content-Type": "application/pdf"})
        with pytest.raises(PDFTooLargeError):
            dl.download(PDF_URL)


# ---------------------------------------------------------------------------
# Permission error
# ---------------------------------------------------------------------------

class TestPermissionError:
    def test_permission_error_on_mkdir_raises(self, download_dir: Path):
        dl = PDFDownloader(download_dir=download_dir, timeout=5,
                           max_retries=1, wait_seconds=0)
        with patch("src.pdf_downloader.Path.mkdir",
                   side_effect=PermissionError("denied")):
            with pytest.raises(PDFPermissionError, match="Cannot create download directory"):
                dl.download(PDF_URL)


# ---------------------------------------------------------------------------
# from_settings factory
# ---------------------------------------------------------------------------

class TestFromSettings:
    def test_from_settings_creates_downloader(self, download_dir: Path):
        mock_settings = MagicMock()
        mock_settings.pdf_download_dir = download_dir
        mock_settings.request_timeout = 30
        mock_settings.max_retries = 3
        dl = PDFDownloader.from_settings(mock_settings)
        assert isinstance(dl, PDFDownloader)
        assert dl._download_dir == download_dir
        assert dl._timeout == 30
        assert dl._max_retries == 3
