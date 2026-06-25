"""
src/pdf_downloader.py — PDF download service.

Responsibility
--------------
Download PDF files from notification URLs, validate them, and persist them
under ``data/pdfs/`` using deterministic SHA-256 filenames.

Design
------
PDFMetadata
    Immutable dataclass returned on every successful download.  Contains
    enough information for the PDF extractor (Phase 6) to locate the file
    and for the notifier to include metadata in alerts.

PDFDownloadError hierarchy
    PDFDownloadError        — base; all errors from this module
    PDFNotFoundError        — HTTP 404 or 410
    PDFTooLargeError        — response exceeds MAX_PDF_BYTES
    PDFInvalidError         — file fails magic-byte / content-type check
    PDFNetworkError         — connection / timeout (retried)
    PDFPermissionError      — filesystem write permission denied

PDFDownloader
    The main class.  One method does the work: ``download(url)``.

    Flow
    ----
    1. Derive a deterministic filename from SHA-256(url).
    2. If the file already exists and is valid, return immediately (skip).
    3. HEAD the URL to check Content-Type and Content-Length before
       downloading the body (fast-fail on obviously wrong responses).
    4. GET with ``stream=True``; read chunks into a temp file, checking
       the cumulative size after each chunk.
    5. Validate: check PDF magic bytes (``%PDF``).
    6. Atomically rename temp file to final path.
    7. Return ``PDFMetadata``.

    Retry
    -----
    Steps 3-6 are wrapped in an exponential back-off retry loop.
    Only ``PDFNetworkError`` is retried.  Auth/content errors are
    raised immediately (deterministic — retrying would not help).

    Security / robustness
    ---------------------
    - Filenames are SHA-256 hashes — no user-supplied path components
      can escape the download directory (no path traversal).
    - Content-Type ``text/html`` is rejected immediately — many broken
      university portals serve an HTML error page with a 200 status.
    - File is written to a ``.tmp`` file first; ``os.replace()`` makes
      the swap atomic so a crashed download never leaves a partial PDF.

Usage
-----
    from src.pdf_downloader import PDFDownloader
    from src.config import get_settings

    downloader = PDFDownloader.from_settings(get_settings())
    metadata   = downloader.download("https://example.com/notice.pdf")
    print(metadata.filepath)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from src.logger import get_logger

if TYPE_CHECKING:
    from src.config import Settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_PDF_BYTES: int = 25 * 1024 * 1024          # 25 MB
CHUNK_SIZE: int = 64 * 1024                     # 64 KB per read chunk
PDF_MAGIC: bytes = b"%PDF"                      # first 4 bytes of every PDF
VALID_CONTENT_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "application/x-pdf",
    "binary/octet-stream",
    "application/octet-stream",
})
REJECTED_CONTENT_TYPES: frozenset[str] = frozenset({
    "text/html",
    "text/plain",
})

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/pdf,application/octet-stream,*/*",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PDFDownloadError(Exception):
    """Base class for all PDF download errors."""


class PDFNotFoundError(PDFDownloadError):
    """HTTP 404/410 — the PDF does not exist at the given URL."""


class PDFTooLargeError(PDFDownloadError):
    """Response body exceeds ``MAX_PDF_BYTES``."""

    def __init__(self, url: str, size_bytes: int) -> None:
        super().__init__(
            f"PDF at {url!r} is too large: "
            f"{size_bytes / 1024 / 1024:.1f} MB > {MAX_PDF_BYTES / 1024 / 1024:.0f} MB limit"
        )
        self.size_bytes = size_bytes


class PDFInvalidError(PDFDownloadError):
    """The downloaded file is not a valid PDF (wrong magic bytes or content-type)."""


class PDFNetworkError(PDFDownloadError):
    """Connection error, timeout, or unexpected HTTP status. May be retried."""


class PDFPermissionError(PDFDownloadError):
    """Filesystem permission denied when writing to the download directory."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PDFMetadata:
    """
    Metadata for a successfully downloaded PDF.

    Attributes
    ----------
    filename:
        Basename of the saved file (e.g. ``"a3f8b2c1d4e5f6a7.pdf"``).
    filepath:
        Absolute ``Path`` to the saved file.
    filesize:
        Size in bytes of the saved file.
    download_time:
        UTC timestamp when the download completed.
    content_type:
        ``Content-Type`` header value returned by the server.
    sha256:
        Full 64-character SHA-256 hex digest of the file contents.
    source_url:
        The URL the PDF was downloaded from.
    was_cached:
        ``True`` if the file already existed and was not re-downloaded.
    """

    filename: str
    filepath: Path
    filesize: int
    download_time: datetime
    content_type: str
    sha256: str
    source_url: str
    was_cached: bool = False

    @property
    def size_kb(self) -> float:
        return self.filesize / 1024

    @property
    def size_mb(self) -> float:
        return self.filesize / 1024 / 1024


# ---------------------------------------------------------------------------
# PDFDownloader
# ---------------------------------------------------------------------------

class PDFDownloader:
    """
    Downloads and validates PDF files.

    Parameters
    ----------
    download_dir:
        Directory under which PDFs are saved.  Created automatically.
    timeout:
        HTTP request timeout in seconds (applied to both HEAD and GET).
    max_retries:
        Maximum retry attempts on network errors.
    wait_seconds:
        Base back-off interval (doubles each retry, capped at 30 s).
        Set to ``0`` in tests to skip sleeping.
    max_size_bytes:
        Maximum allowed PDF size in bytes.  Defaults to 25 MB.
    """

    def __init__(
        self,
        download_dir: Path,
        timeout: int = 30,
        max_retries: int = 3,
        wait_seconds: float = 1.0,
        max_size_bytes: int = MAX_PDF_BYTES,
    ) -> None:
        self._download_dir = download_dir
        self._timeout = timeout
        self._max_retries = max_retries
        self._wait_seconds = wait_seconds
        self._max_size_bytes = max_size_bytes
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: "Settings") -> "PDFDownloader":
        """Construct a ``PDFDownloader`` from application settings."""
        return cls(
            download_dir=settings.pdf_download_dir,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def download(self, url: str) -> PDFMetadata:
        """
        Download the PDF at *url* and return its metadata.

        If the file has already been downloaded (same URL → same filename),
        the cached file is returned immediately without a network request.

        Parameters
        ----------
        url:
            Absolute URL of the PDF to download.

        Returns
        -------
        PDFMetadata
            Metadata for the downloaded (or already-cached) file.

        Raises
        ------
        PDFNotFoundError
            HTTP 404 or 410.
        PDFTooLargeError
            File exceeds ``max_size_bytes``.
        PDFInvalidError
            Content-Type is HTML, or file lacks the ``%PDF`` magic bytes.
        PDFNetworkError
            After all retries are exhausted on connection/timeout errors.
        PDFPermissionError
            Cannot write to ``download_dir``.
        """
        filename = self._url_to_filename(url)
        filepath = self._ensure_dir() / filename

        # ── Cache hit ─────────────────────────────────────────────────────
        if filepath.exists() and filepath.stat().st_size > 0:
            log.info(
                "pdf_cache_hit",
                url=url,
                filepath=str(filepath),
                size_bytes=filepath.stat().st_size,
            )
            return PDFMetadata(
                filename=filename,
                filepath=filepath,
                filesize=filepath.stat().st_size,
                download_time=datetime.now(tz=timezone.utc),
                content_type="application/pdf",
                sha256=self._sha256_file(filepath),
                source_url=url,
                was_cached=True,
            )

        # ── Download with retry ───────────────────────────────────────────
        last_exc: PDFNetworkError | None = None
        wait = self._wait_seconds

        for attempt in range(1, self._max_retries + 1):
            try:
                return self._download_once(url, filepath)
            except PDFNetworkError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    log.warning(
                        "pdf_download_retry",
                        url=url,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        wait_seconds=wait,
                        error=str(exc),
                    )
                    time.sleep(wait)
                    wait = min(wait * 2, 30.0)
            except (PDFNotFoundError, PDFTooLargeError, PDFInvalidError, PDFPermissionError):
                raise  # not retried — deterministic errors

        raise PDFNetworkError(
            f"Failed to download {url!r} after {self._max_retries} attempts. "
            f"Last error: {last_exc}"
        ) from last_exc

    def close(self) -> None:
        """Close the underlying requests Session."""
        self._session.close()

    def __enter__(self) -> "PDFDownloader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _download_once(self, url: str, filepath: Path) -> PDFMetadata:
        """
        One download attempt.  Raises on any error; does not retry.
        """
        # Step 1: HEAD request — fast-fail before downloading the body
        content_type = self._head_check(url)

        # Step 2: GET with streaming
        log.info("pdf_download_start", url=url, filepath=str(filepath))
        try:
            response = self._session.get(url, stream=True, timeout=self._timeout)
        except requests.exceptions.Timeout as exc:
            raise PDFNetworkError(f"GET timed out after {self._timeout}s: {url}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise PDFNetworkError(f"Connection error downloading {url}: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise PDFNetworkError(f"Request error downloading {url}: {exc}") from exc

        if response.status_code in (404, 410):
            raise PDFNotFoundError(f"PDF not found at {url!r} (HTTP {response.status_code})")
        if response.status_code != 200:
            raise PDFNetworkError(f"Unexpected HTTP {response.status_code} downloading {url!r}")

        # Use actual response content-type if HEAD gave a generic one
        actual_ct = response.headers.get("Content-Type", content_type).split(";")[0].strip().lower()
        if actual_ct in REJECTED_CONTENT_TYPES:
            raise PDFInvalidError(
                f"Server returned {actual_ct!r} instead of a PDF for {url!r}. "
                "The URL may point to an HTML error page."
            )

        # Step 3: Stream to temp file, enforcing size limit
        tmp_path = self._stream_to_temp(url, response, filepath.parent)

        # Step 4: Validate magic bytes
        self._validate_pdf_magic(tmp_path, url)

        # Step 5: Compute SHA-256 and final size
        sha256 = self._sha256_file(tmp_path)
        filesize = tmp_path.stat().st_size

        # Step 6: Atomic rename temp → final path
        try:
            os.replace(tmp_path, filepath)
        except PermissionError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise PDFPermissionError(
                f"Permission denied moving downloaded PDF to {filepath!r}: {exc}"
            ) from exc

        log.info(
            "pdf_download_complete",
            url=url,
            filepath=str(filepath),
            size_bytes=filesize,
            size_mb=round(filesize / 1024 / 1024, 2),
            sha256=sha256[:16] + "...",
        )

        return PDFMetadata(
            filename=filepath.name,
            filepath=filepath,
            filesize=filesize,
            download_time=datetime.now(tz=timezone.utc),
            content_type=actual_ct,
            sha256=sha256,
            source_url=url,
            was_cached=False,
        )

    def _head_check(self, url: str) -> str:
        """
        Make a HEAD request to check Content-Type and Content-Length.

        Returns the content-type string (lower-cased, parameters stripped).
        Raises ``PDFNetworkError`` on connection errors (retried by caller).
        Raises ``PDFNotFoundError`` on 404/410 (not retried).
        Raises ``PDFTooLargeError`` if Content-Length exceeds limit.
        Raises ``PDFInvalidError`` if Content-Type is clearly not a PDF.
        """
        try:
            head = self._session.head(url, timeout=self._timeout, allow_redirects=True)
        except requests.exceptions.Timeout as exc:
            raise PDFNetworkError(f"HEAD timed out for {url!r}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise PDFNetworkError(f"HEAD connection error for {url!r}: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            # Some servers reject HEAD — treat as non-fatal, proceed with GET
            log.debug("pdf_head_failed", url=url, error=str(exc))
            return "application/octet-stream"

        if head.status_code in (404, 410):
            raise PDFNotFoundError(f"PDF not found at {url!r} (HTTP {head.status_code})")

        # Some servers return 405 Method Not Allowed for HEAD — skip checks
        if head.status_code == 405:
            return "application/octet-stream"

        content_type = head.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type in REJECTED_CONTENT_TYPES:
            raise PDFInvalidError(
                f"HEAD Content-Type {content_type!r} is not a PDF for {url!r}"
            )

        content_length = head.headers.get("Content-Length")
        if content_length:
            try:
                size = int(content_length)
                if size > self._max_size_bytes:
                    raise PDFTooLargeError(url, size)
            except ValueError:
                pass

        return content_type or "application/octet-stream"

    def _stream_to_temp(
        self,
        url: str,
        response: requests.Response,
        directory: Path,
    ) -> Path:
        """
        Stream *response* body to a temp file in *directory*.

        Returns the temp file ``Path``.
        Raises ``PDFTooLargeError`` if cumulative bytes exceed ``max_size_bytes``.
        Raises ``PDFInvalidError`` if response body is empty.
        """
        try:
            fd, tmp_str = tempfile.mkstemp(dir=directory, prefix=".pdf_dl_", suffix=".tmp")
        except PermissionError as exc:
            raise PDFPermissionError(
                f"Cannot create temp file in {directory!r}: {exc}"
            ) from exc

        tmp_path = Path(tmp_str)
        total_bytes = 0

        try:
            with os.fdopen(fd, "wb") as fh:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > self._max_size_bytes:
                        raise PDFTooLargeError(url, total_bytes)
                    fh.write(chunk)
        except PDFTooLargeError:
            tmp_path.unlink(missing_ok=True)
            raise
        except (OSError, PermissionError) as exc:
            tmp_path.unlink(missing_ok=True)
            raise PDFPermissionError(f"Write error for temp file {tmp_str!r}: {exc}") from exc

        if total_bytes == 0:
            tmp_path.unlink(missing_ok=True)
            raise PDFInvalidError(f"Server returned an empty body for {url!r}")

        log.debug("pdf_stream_complete", url=url, total_bytes=total_bytes)
        return tmp_path

    @staticmethod
    def _validate_pdf_magic(path: Path, url: str) -> None:
        """
        Read the first 4 bytes and confirm they are ``%PDF``.

        Raises ``PDFInvalidError`` if the file is not a PDF.
        """
        try:
            with path.open("rb") as fh:
                magic = fh.read(4)
        except OSError as exc:
            raise PDFInvalidError(f"Cannot read downloaded file for validation: {exc}") from exc

        if magic != PDF_MAGIC:
            path.unlink(missing_ok=True)
            raise PDFInvalidError(
                f"Downloaded file from {url!r} is not a valid PDF "
                f"(magic bytes: {magic!r}, expected {PDF_MAGIC!r})"
            )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        """Compute and return the full SHA-256 hex digest of *path*."""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _url_to_filename(url: str) -> str:
        """
        Derive a deterministic, filesystem-safe filename from *url*.

        Uses SHA-256(url) as the stem so:
        - The same URL always maps to the same file (idempotent).
        - No path-traversal is possible (user-controlled URL has no effect
          on the directory structure).
        """
        stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return f"{stem}.pdf"

    def _ensure_dir(self) -> Path:
        """Create ``download_dir`` if it does not exist and return it."""
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PDFPermissionError(
                f"Cannot create download directory {self._download_dir!r}: {exc}"
            ) from exc
        return self._download_dir
