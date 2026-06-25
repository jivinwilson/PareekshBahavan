"""
src/pdf_reader.py — PDF text extraction service.

Responsibility
--------------
Read a downloaded PDF file and extract its full text content, page by page.
Does NOT download PDFs (that is PDFDownloader's job) and does NOT search for
keywords (that is the matcher's job).

Architecture
------------
PageContent
    Immutable dataclass for one page: page_number + raw extracted text.

PDFContent
    Immutable dataclass for a whole document: filename, total_pages,
    full_text (all pages joined), pages (list of PageContent),
    extraction_time, extraction_method, is_scanned, is_encrypted, and
    an optional error_message for partial-failure reporting.

PDFReader
    The main class.  Single public method: ``read(path)``.

    Extraction strategy
    -------------------
    1. ``pdfplumber`` — primary.  Better at preserving layout and handles
       complex table-heavy university PDFs well.
    2. ``pypdf``      — fallback.  Used if pdfplumber raises or returns
       zero text across all pages.

    The strategy pattern is implemented as two private methods
    (``_extract_with_pdfplumber``, ``_extract_with_pypdf``) that both
    return ``list[PageContent]``.  Switching the order or adding a third
    strategy (e.g. OCR) only requires changing ``read()``.

    Post-processing
    ---------------
    ``_normalize_text``         — collapse whitespace, strip leading/trailing
    ``_remove_headers_footers`` — detect lines that repeat on > 50 % of pages
                                  and strip them (catches page numbers, URLs,
                                  institution name repeated on every page)
    ``_is_scanned_pdf``         — True when every page has zero extractable text

    Error handling
    --------------
    - Encrypted PDF:   detected before extraction; returned with is_encrypted=True
    - Corrupted PDF:   pdfplumber raises; fallback to pypdf; if both fail,
                       returned with empty text and error_message set
    - Missing file:    raises ``PDFReadError`` immediately
    - Scanned PDF:     both extractors return empty strings; is_scanned=True,
                       full_text="", no crash

Usage
-----
    from src.pdf_reader import PDFReader

    reader = PDFReader()
    content = reader.read(Path("data/pdfs/abc123.pdf"))
    print(content.full_text[:500])
    if content.is_scanned:
        print("Scanned PDF — OCR needed")
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from src.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PDFReadError(Exception):
    """Raised when a PDF cannot be read at all (missing file, unrecoverable)."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageContent:
    """
    Text content of a single PDF page.

    Attributes
    ----------
    page_number:
        1-based page index.
    text:
        Extracted and normalised text for this page.
        Empty string for blank or image-only pages.
    char_count:
        Number of characters in ``text`` (convenience for callers).
    """

    page_number: int
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class PDFContent:
    """
    Full extraction result for one PDF file.

    Attributes
    ----------
    filename:
        Basename of the source PDF file.
    total_pages:
        Number of pages in the document.
    full_text:
        All page text joined with double newlines, ready for keyword search.
    pages:
        Ordered list of ``PageContent`` objects (one per page).
    extraction_time:
        UTC timestamp when extraction completed.
    extraction_method:
        Which library succeeded: ``"pdfplumber"``, ``"pypdf"``, or
        ``"failed"`` when both raised.
    is_scanned:
        True when text extraction returned nothing (image-only PDF).
        ``full_text`` will be empty; callers may queue for OCR.
    is_encrypted:
        True when the PDF is password-protected.
        ``full_text`` will be empty.
    error_message:
        Human-readable description of any non-fatal error encountered
        during extraction.  Empty string when extraction succeeded fully.
    """

    filename: str
    total_pages: int
    full_text: str
    pages: tuple[PageContent, ...]
    extraction_time: datetime
    extraction_method: str
    is_scanned: bool = False
    is_encrypted: bool = False
    error_message: str = ""

    @property
    def has_text(self) -> bool:
        return bool(self.full_text.strip())

    @property
    def word_count(self) -> int:
        return len(self.full_text.split()) if self.full_text else 0

    def page(self, number: int) -> PageContent | None:
        """Return the ``PageContent`` for 1-based *number*, or None."""
        for p in self.pages:
            if p.page_number == number:
                return p
        return None


# ---------------------------------------------------------------------------
# PDFReader
# ---------------------------------------------------------------------------

class PDFReader:
    """
    Extracts text from PDF files.

    Parameters
    ----------
    remove_headers_footers:
        When ``True`` (default), lines that appear on more than half the
        pages are stripped (page numbers, institution headers, etc.).
    min_header_footer_pages:
        Minimum number of pages needed before attempting header/footer
        removal (default: 3 — no point on a 1- or 2-page document).
    """

    def __init__(
        self,
        remove_headers_footers: bool = True,
        min_header_footer_pages: int = 3,
    ) -> None:
        self._remove_hf = remove_headers_footers
        self._min_hf_pages = min_header_footer_pages

    # ── Public API ────────────────────────────────────────────────────────────

    def read(self, path: Path) -> PDFContent:
        """
        Extract text from the PDF at *path*.

        Parameters
        ----------
        path:
            Absolute or relative path to a downloaded PDF file.

        Returns
        -------
        PDFContent
            Always returns a ``PDFContent`` object — never raises on
            extraction errors.  Check ``is_scanned``, ``is_encrypted``,
            and ``error_message`` to understand partial results.

        Raises
        ------
        PDFReadError
            Only when *path* does not exist or is not a file.
        """
        if not path.exists() or not path.is_file():
            raise PDFReadError(f"PDF file not found: {path!r}")

        filename = path.name
        start = time.monotonic()

        log.info("pdf_read_start", filename=filename, path=str(path))

        # ── Encryption check ─────────────────────────────────────────────
        if self._is_encrypted(path):
            elapsed = time.monotonic() - start
            log.warning("pdf_encrypted", filename=filename)
            return PDFContent(
                filename=filename,
                total_pages=0,
                full_text="",
                pages=(),
                extraction_time=datetime.now(tz=timezone.utc),
                extraction_method="failed",
                is_encrypted=True,
                error_message="PDF is password-protected; text extraction skipped.",
            )

        # ── Attempt 1: pdfplumber ─────────────────────────────────────────
        pages: list[PageContent] = []
        method = "pdfplumber"
        error_msg = ""

        try:
            pages = self._extract_with_pdfplumber(path)
            log.debug("pdf_pdfplumber_ok", filename=filename, pages=len(pages))
        except Exception as exc:
            error_msg = f"pdfplumber failed: {exc}"
            log.warning("pdf_pdfplumber_failed", filename=filename, error=str(exc))

            # ── Attempt 2: pypdf fallback ─────────────────────────────────
            method = "pypdf"
            try:
                pages = self._extract_with_pypdf(path)
                log.debug("pdf_pypdf_ok", filename=filename, pages=len(pages))
                error_msg = ""          # fallback succeeded — clear the error
            except Exception as exc2:
                method = "failed"
                error_msg = f"pdfplumber: {exc}; pypdf: {exc2}"
                log.error("pdf_both_extractors_failed", filename=filename, error=error_msg)

        # ── Post-process ──────────────────────────────────────────────────
        if pages and self._remove_hf and len(pages) >= self._min_hf_pages:
            pages = self._remove_headers_footers(pages)

        is_scanned = bool(pages) and all(p.is_empty for p in pages)
        if is_scanned:
            log.warning("pdf_scanned_detected", filename=filename, pages=len(pages))
            if not error_msg:
                error_msg = (
                    "No text could be extracted. The PDF appears to be "
                    "image-only (scanned). OCR would be needed."
                )

        full_text = "\n\n".join(p.text for p in pages if p.text.strip())
        elapsed_ms = int((time.monotonic() - start) * 1000)

        log.info(
            "pdf_read_complete",
            filename=filename,
            method=method,
            total_pages=len(pages),
            char_count=len(full_text),
            is_scanned=is_scanned,
            elapsed_ms=elapsed_ms,
        )

        return PDFContent(
            filename=filename,
            total_pages=len(pages),
            full_text=full_text,
            pages=tuple(pages),
            extraction_time=datetime.now(tz=timezone.utc),
            extraction_method=method,
            is_scanned=is_scanned,
            is_encrypted=False,
            error_message=error_msg,
        )

    # ── Extraction strategies ─────────────────────────────────────────────────

    def _extract_with_pdfplumber(self, path: Path) -> list[PageContent]:
        """
        Primary extraction using ``pdfplumber``.

        Raises any pdfplumber exception so the caller can fall back to pypdf.
        """
        import pdfplumber  # deferred import — not all environments have it

        pages: list[PageContent] = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                raw = page.extract_text() or ""
                text = self._normalize_text(raw)
                pages.append(PageContent(page_number=i, text=text))
        return pages

    def _extract_with_pypdf(self, path: Path) -> list[PageContent]:
        """
        Fallback extraction using ``pypdf``.

        Raises any pypdf exception so the caller can set extraction_method
        to ``"failed"``.
        """
        from pypdf import PdfReader  # deferred import

        pages: list[PageContent] = []
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages, start=1):
            raw = page.extract_text() or ""
            text = self._normalize_text(raw)
            pages.append(PageContent(page_number=i, text=text))
        return pages

    # ── Post-processing ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Normalise extracted text:
        - Replace form-feeds and vertical tabs with newlines
        - Collapse runs of 3+ newlines to two (preserve paragraph breaks)
        - Strip leading/trailing whitespace from each line
        - Strip leading/trailing whitespace from the whole text
        """
        if not text:
            return ""
        # Replace common non-newline whitespace markers
        text = text.replace("\f", "\n").replace("\v", "\n").replace("\r\n", "\n").replace("\r", "\n")
        # Strip trailing spaces from each line
        lines = [line.rstrip() for line in text.split("\n")]
        text = "\n".join(lines)
        # Collapse 3+ consecutive newlines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _remove_headers_footers(pages: list[PageContent]) -> list[PageContent]:
        """
        Remove lines that appear on more than 50 % of pages.

        University PDFs typically repeat the institution name, the document
        title, and page numbers on every page.  These add noise to keyword
        matching without contributing useful content.

        The algorithm:
        1. Split each page into lines.
        2. Count how many pages each unique stripped line appears on.
        3. Lines appearing on > 50 % of pages are candidates for removal.
        4. Only lines that are short (≤ 120 chars) are removed — long lines
           are content, not headers/footers.
        5. Rebuild each page's text without the removed lines.
        """
        if not pages:
            return pages

        total = len(pages)
        threshold = total * 0.5

        # Count page occurrences per line
        line_page_count: Counter[str] = Counter()
        for page in pages:
            seen_on_this_page: set[str] = set()
            for line in page.text.splitlines():
                stripped = line.strip()
                if stripped and stripped not in seen_on_this_page:
                    line_page_count[stripped] += 1
                    seen_on_this_page.add(stripped)

        # Identify repeated short lines
        repeated: set[str] = {
            line for line, count in line_page_count.items()
            if count > threshold and len(line) <= 120
        }

        if not repeated:
            return pages

        log.debug("pdf_headers_footers_removed", count=len(repeated))

        # Rebuild pages without repeated lines
        cleaned: list[PageContent] = []
        for page in pages:
            new_lines = [
                line for line in page.text.splitlines()
                if line.strip() not in repeated
            ]
            new_text = PDFReader._normalize_text("\n".join(new_lines))
            cleaned.append(PageContent(page_number=page.page_number, text=new_text))
        return cleaned

    @staticmethod
    def _is_scanned(pages: list[PageContent]) -> bool:
        """Return True when all pages have no extractable text."""
        return bool(pages) and all(p.is_empty for p in pages)

    # ── Encryption detection ──────────────────────────────────────────────────

    @staticmethod
    def _is_encrypted(path: Path) -> bool:
        """
        Return True if the PDF is password-protected.

        Tries pypdf first (lightweight check); falls back to raw bytes scan
        if pypdf is unavailable.
        """
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return reader.is_encrypted
        except Exception:
            pass

        # Fallback: scan raw bytes for /Encrypt dictionary marker
        try:
            with path.open("rb") as fh:
                header = fh.read(8192)
            return b"/Encrypt" in header
        except OSError:
            return False
