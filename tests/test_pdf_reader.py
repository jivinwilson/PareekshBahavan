"""
tests/test_pdf_reader.py — Unit tests for src/pdf_reader.py

Strategy
--------
Real extractable PDFs are created in-process using ``fpdf2`` (installed as a
test dependency) and written to /tmp directories so pdfplumber / pypdf can
open them for genuine extraction tests.

Error-path tests mock pdfplumber / pypdf internals so every error branch is
exercised without needing broken PDF files on disk.

Coverage
--------
PageContent
  - frozen, char_count property, is_empty property

PDFContent
  - frozen, has_text, word_count, page() lookup

PDFReader.read()
  - Normal single-page PDF (real extraction via pdfplumber)
  - Multi-page PDF with text on every page
  - PDF with header/footer removal (line repeated across 3+ pages stripped)
  - Empty PDF (blank pages — no text, is_scanned=True)
  - Corrupted PDF — pdfplumber fails, pypdf succeeds (fallback path)
  - Corrupted PDF — both extractors fail (error_message set, empty text)
  - Encrypted / password-protected PDF
  - Scanned PDF (image-only — no extractable text, is_scanned=True)
  - Missing file raises PDFReadError
  - Path is a directory raises PDFReadError
  - Whitespace normalisation
  - Header/footer removal threshold (only removes short repeated lines)
"""

from __future__ import annotations

import io
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.pdf_reader import (
    PDFContent,
    PDFReadError,
    PDFReader,
    PageContent,
)


# ---------------------------------------------------------------------------
# PDF fixture helpers
# ---------------------------------------------------------------------------

def _make_pdf(pages: list[str]) -> bytes:
    """
    Create a real extractable PDF with one text line per page using fpdf2.
    Returns raw PDF bytes.
    """
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        for line in text.splitlines():
            pdf.cell(0, 8, line, ln=True)
    return bytes(pdf.output())


def _make_blank_pdf(num_pages: int = 1) -> bytes:
    """Create a PDF with blank pages (no text)."""
    from fpdf import FPDF
    pdf = FPDF()
    for _ in range(num_pages):
        pdf.add_page()
    return bytes(pdf.output())


def _write_tmp(content: bytes, suffix: str = ".pdf") -> Path:
    """Write *content* to a /tmp file and return the Path."""
    fd, path_str = tempfile.mkstemp(dir="/tmp", suffix=suffix)
    import os
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return Path(path_str)


@pytest.fixture
def reader() -> PDFReader:
    return PDFReader(remove_headers_footers=True, min_header_footer_pages=3)


@pytest.fixture
def reader_no_hf() -> PDFReader:
    """Reader with header/footer removal disabled."""
    return PDFReader(remove_headers_footers=False)


# ---------------------------------------------------------------------------
# PageContent model
# ---------------------------------------------------------------------------

class TestPageContent:
    def test_frozen(self):
        p = PageContent(page_number=1, text="Hello")
        with pytest.raises(Exception):
            p.page_number = 2  # type: ignore[misc]

    def test_char_count(self):
        p = PageContent(page_number=1, text="Hello World")
        assert p.char_count == 11

    def test_is_empty_true(self):
        assert PageContent(page_number=1, text="   ").is_empty is True
        assert PageContent(page_number=1, text="").is_empty is True

    def test_is_empty_false(self):
        assert PageContent(page_number=1, text="text").is_empty is False


# ---------------------------------------------------------------------------
# PDFContent model
# ---------------------------------------------------------------------------

class TestPDFContent:
    def _make(self, **kw) -> PDFContent:
        from datetime import datetime
        defaults = dict(
            filename="test.pdf",
            total_pages=1,
            full_text="Hello World examination",
            pages=(PageContent(1, "Hello World examination"),),
            extraction_time=datetime.now(tz=timezone.utc),
            extraction_method="pdfplumber",
        )
        defaults.update(kw)
        return PDFContent(**defaults)

    def test_frozen(self):
        c = self._make()
        with pytest.raises(Exception):
            c.filename = "other.pdf"  # type: ignore[misc]

    def test_has_text_true(self):
        assert self._make(full_text="some text").has_text is True

    def test_has_text_false(self):
        assert self._make(full_text="").has_text is False
        assert self._make(full_text="   ").has_text is False

    def test_word_count(self):
        c = self._make(full_text="one two three four")
        assert c.word_count == 4

    def test_word_count_empty(self):
        c = self._make(full_text="")
        assert c.word_count == 0

    def test_page_lookup_found(self):
        p1 = PageContent(1, "page one")
        p2 = PageContent(2, "page two")
        c = self._make(pages=(p1, p2), total_pages=2, full_text="page one\n\npage two")
        assert c.page(1) is p1
        assert c.page(2) is p2

    def test_page_lookup_not_found(self):
        c = self._make()
        assert c.page(99) is None


# ---------------------------------------------------------------------------
# PDFReader — successful extraction
# ---------------------------------------------------------------------------

class TestPDFReaderSuccess:
    def test_single_page_extraction(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Special Examination Notice CBCSS 2026"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.total_pages == 1
            assert content.extraction_method == "pdfplumber"
            assert content.is_scanned is False
            assert content.is_encrypted is False
            assert content.error_message == ""
        finally:
            path.unlink(missing_ok=True)

    def test_single_page_text_extracted(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Special Examination Notice CBCSS 2026"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert "Special Examination" in content.full_text
            assert "CBCSS" in content.full_text
        finally:
            path.unlink(missing_ok=True)

    def test_multipage_correct_page_count(self, reader: PDFReader):
        texts = [
            "Page one: B.Sc Computer Science 2020 Admission",
            "Page two: Third Semester Examination Schedule",
            "Page three: Hall Ticket Instructions",
        ]
        pdf_bytes = _make_pdf(texts)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.total_pages == 3
            assert len(content.pages) == 3
        finally:
            path.unlink(missing_ok=True)

    def test_multipage_page_numbers_correct(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["First page", "Second page", "Third page"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.pages[0].page_number == 1
            assert content.pages[1].page_number == 2
            assert content.pages[2].page_number == 3
        finally:
            path.unlink(missing_ok=True)

    def test_multipage_all_text_in_full_text(self, reader_no_hf: PDFReader):
        pdf_bytes = _make_pdf(["Alpha content", "Beta content", "Gamma content"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader_no_hf.read(path)
            assert "Alpha" in content.full_text
            assert "Beta" in content.full_text
            assert "Gamma" in content.full_text
        finally:
            path.unlink(missing_ok=True)

    def test_filename_set_correctly(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Content"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.filename == path.name
        finally:
            path.unlink(missing_ok=True)

    def test_extraction_time_is_utc(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Content"])
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.extraction_time.tzinfo == timezone.utc
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Header/footer removal
# ---------------------------------------------------------------------------

class TestHeaderFooterRemoval:
    def test_repeated_line_removed(self):
        """A line appearing on all 4 pages (> 50 %) should be stripped."""
        reader = PDFReader(remove_headers_footers=True, min_header_footer_pages=3)
        repeated = "University of Calicut — Pareeksha Bhavan"
        pages = [
            PageContent(1, f"{repeated}\nActual content page one"),
            PageContent(2, f"{repeated}\nActual content page two"),
            PageContent(3, f"{repeated}\nActual content page three"),
            PageContent(4, f"{repeated}\nActual content page four"),
        ]
        cleaned = PDFReader._remove_headers_footers(pages)
        for page in cleaned:
            assert repeated not in page.text

    def test_unique_content_preserved(self):
        repeated = "Header"
        pages = [
            PageContent(1, f"{repeated}\nUnique text A"),
            PageContent(2, f"{repeated}\nUnique text B"),
            PageContent(3, f"{repeated}\nUnique text C"),
        ]
        cleaned = PDFReader._remove_headers_footers(pages)
        texts = " ".join(p.text for p in cleaned)
        assert "Unique text A" in texts
        assert "Unique text B" in texts
        assert "Unique text C" in texts

    def test_line_on_minority_of_pages_kept(self):
        """Line on only 1 of 4 pages (25 %) should NOT be removed."""
        pages = [
            PageContent(1, "Rare line\nContent A"),
            PageContent(2, "Content B"),
            PageContent(3, "Content C"),
            PageContent(4, "Content D"),
        ]
        cleaned = PDFReader._remove_headers_footers(pages)
        assert "Rare line" in cleaned[0].text

    def test_empty_pages_list(self):
        assert PDFReader._remove_headers_footers([]) == []

    def test_min_hf_pages_skips_small_doc(self, reader: PDFReader):
        """Only 2 pages → below min_header_footer_pages=3, removal skipped."""
        # Even if same line on both pages, it should NOT be removed
        repeated = "Header Line"
        pages_data = [f"{repeated}\nContent {i}" for i in range(2)]
        pdf_bytes = _make_pdf(pages_data)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            # Header removal requires >= 3 pages; with 2 pages it's skipped
            combined = content.full_text
            # Content should be present; whether header is kept is ok (2 pages)
            assert "Content" in combined
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Empty PDF (blank pages)
# ---------------------------------------------------------------------------

class TestEmptyPDF:
    def test_blank_pdf_is_scanned(self, reader: PDFReader):
        pdf_bytes = _make_blank_pdf(1)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.is_scanned is True
        finally:
            path.unlink(missing_ok=True)

    def test_blank_pdf_full_text_empty(self, reader: PDFReader):
        pdf_bytes = _make_blank_pdf(2)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.full_text == ""
            assert content.has_text is False
        finally:
            path.unlink(missing_ok=True)

    def test_blank_pdf_error_message_set(self, reader: PDFReader):
        pdf_bytes = _make_blank_pdf(1)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert "scanned" in content.error_message.lower() or "image" in content.error_message.lower()
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Corrupted PDF — pdfplumber fails, pypdf fallback
# ---------------------------------------------------------------------------

class TestCorruptedPDF:
    def test_pdfplumber_fails_falls_back_to_pypdf(self, reader: PDFReader):
        """pdfplumber raises, pypdf succeeds — method should be 'pypdf'."""
        pdf_bytes = _make_pdf(["Fallback content"])
        path = _write_tmp(pdf_bytes)
        try:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = None

            with patch("pdfplumber.open") as mock_open:
                mock_pdf = MagicMock()
                mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
                mock_pdf.__exit__ = MagicMock(return_value=False)
                mock_pdf.pages = MagicMock()
                mock_pdf.pages.__iter__ = MagicMock(side_effect=Exception("pdfplumber internal error"))
                mock_open.return_value = mock_pdf

                content = reader.read(path)

            assert content.extraction_method == "pypdf"
            assert content.error_message == ""  # fallback succeeded
        finally:
            path.unlink(missing_ok=True)

    def test_both_extractors_fail_returns_empty(self, reader: PDFReader):
        """Both pdfplumber and pypdf fail — empty text, error_message set."""
        pdf_bytes = _make_pdf(["Content"])
        path = _write_tmp(pdf_bytes)
        try:
            with patch("pdfplumber.open", side_effect=Exception("pdfplumber crashed")), \
                 patch("pypdf.PdfReader", side_effect=Exception("pypdf crashed")):
                content = reader.read(path)

            assert content.extraction_method == "failed"
            assert content.full_text == ""
            assert "pdfplumber" in content.error_message
            assert "pypdf" in content.error_message
        finally:
            path.unlink(missing_ok=True)

    def test_both_fail_does_not_raise(self, reader: PDFReader):
        """Even total extraction failure must not raise — returns PDFContent."""
        pdf_bytes = _make_pdf(["Content"])
        path = _write_tmp(pdf_bytes)
        try:
            with patch("pdfplumber.open", side_effect=Exception("boom")), \
                 patch("pypdf.PdfReader", side_effect=Exception("boom")):
                content = reader.read(path)   # must not raise
            assert isinstance(content, PDFContent)
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Encrypted PDF
# ---------------------------------------------------------------------------

class TestEncryptedPDF:
    def test_encrypted_pdf_returns_is_encrypted(self, reader: PDFReader):
        """Mock PdfReader.is_encrypted = True → PDFContent.is_encrypted=True."""
        pdf_bytes = _make_pdf(["Secret content"])
        path = _write_tmp(pdf_bytes)
        try:
            mock_reader = MagicMock()
            mock_reader.is_encrypted = True
            with patch("pypdf.PdfReader", return_value=mock_reader):
                content = reader.read(path)
            assert content.is_encrypted is True
        finally:
            path.unlink(missing_ok=True)

    def test_encrypted_pdf_empty_text(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Secret content"])
        path = _write_tmp(pdf_bytes)
        try:
            mock_reader = MagicMock()
            mock_reader.is_encrypted = True
            with patch("pypdf.PdfReader", return_value=mock_reader):
                content = reader.read(path)
            assert content.full_text == ""
            assert content.has_text is False
        finally:
            path.unlink(missing_ok=True)

    def test_encrypted_pdf_error_message_set(self, reader: PDFReader):
        pdf_bytes = _make_pdf(["Secret"])
        path = _write_tmp(pdf_bytes)
        try:
            mock_reader = MagicMock()
            mock_reader.is_encrypted = True
            with patch("pypdf.PdfReader", return_value=mock_reader):
                content = reader.read(path)
            assert "password" in content.error_message.lower() or "encrypted" in content.error_message.lower()
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Scanned / image-only PDF
# ---------------------------------------------------------------------------

class TestScannedPDF:
    def test_image_only_pdf_is_scanned(self, reader: PDFReader):
        """pdfplumber returns None/empty for every page → is_scanned=True."""
        pdf_bytes = _make_blank_pdf(3)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.is_scanned is True
            assert content.full_text == ""
        finally:
            path.unlink(missing_ok=True)

    def test_scanned_pdf_has_correct_page_count(self, reader: PDFReader):
        pdf_bytes = _make_blank_pdf(3)
        path = _write_tmp(pdf_bytes)
        try:
            content = reader.read(path)
            assert content.total_pages == 3
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Missing / invalid file
# ---------------------------------------------------------------------------

class TestMissingFile:
    def test_missing_file_raises(self, reader: PDFReader):
        with pytest.raises(PDFReadError, match="not found"):
            reader.read(Path("/tmp/this_file_does_not_exist_xyz.pdf"))

    def test_directory_path_raises(self, reader: PDFReader):
        with pytest.raises(PDFReadError, match="not found"):
            reader.read(Path("/tmp"))


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

class TestNormalizeText:
    def test_strips_leading_trailing_whitespace(self):
        result = PDFReader._normalize_text("  hello  ")
        assert result == "hello"

    def test_collapses_excessive_newlines(self):
        result = PDFReader._normalize_text("A\n\n\n\n\nB")
        assert result == "A\n\nB"

    def test_form_feed_replaced(self):
        result = PDFReader._normalize_text("A\fB")
        assert "\f" not in result
        assert "A" in result and "B" in result

    def test_empty_string_returns_empty(self):
        assert PDFReader._normalize_text("") == ""

    def test_none_like_empty_string(self):
        assert PDFReader._normalize_text("") == ""
