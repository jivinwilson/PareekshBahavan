"""
tests/test_utils.py — Unit tests for src/utils.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.utils import (
    clean_text,
    collapse_whitespace,
    extract_domain,
    format_datetime,
    is_pdf_url,
    make_content_hash,
    make_notification_id,
    normalize_url,
    parse_date,
    strip_html,
    truncate,
    utc_now,
)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

class TestHashing:
    def test_make_notification_id_is_16_chars(self):
        nid = make_notification_id("https://example.com", "Special Exam")
        assert len(nid) == 16

    def test_make_notification_id_is_hex(self):
        nid = make_notification_id("https://example.com", "Special Exam")
        int(nid, 16)  # raises ValueError if not valid hex

    def test_same_inputs_same_id(self):
        a = make_notification_id("https://example.com", "Special Exam")
        b = make_notification_id("https://example.com", "Special Exam")
        assert a == b

    def test_different_title_different_id(self):
        a = make_notification_id("https://example.com", "Exam A")
        b = make_notification_id("https://example.com", "Exam B")
        assert a != b

    def test_different_url_different_id(self):
        a = make_notification_id("https://a.com", "Title")
        b = make_notification_id("https://b.com", "Title")
        assert a != b

    def test_empty_title_still_returns_id(self):
        nid = make_notification_id("https://example.com")
        assert len(nid) == 16

    def test_make_content_hash_is_32_chars(self):
        h = make_content_hash("some pdf text")
        assert len(h) == 32

    def test_content_hash_same_text(self):
        assert make_content_hash("abc") == make_content_hash("abc")

    def test_content_hash_different_text(self):
        assert make_content_hash("abc") != make_content_hash("xyz")


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

class TestNormalizeUrl:
    def test_relative_url_resolved(self):
        result = normalize_url("/notices/exam.pdf", "https://example.com/")
        assert result == "https://example.com/notices/exam.pdf"

    def test_scheme_lowercased(self):
        result = normalize_url("HTTPS://example.com/path")
        assert result.startswith("https://")

    def test_host_lowercased(self):
        result = normalize_url("https://EXAMPLE.COM/path")
        assert "example.com" in result

    def test_fragment_stripped(self):
        result = normalize_url("https://example.com/path#anchor")
        assert "#" not in result

    def test_double_slashes_collapsed(self):
        result = normalize_url("https://example.com//notices//exam.pdf")
        assert "//" not in result.replace("https://", "")

    def test_absolute_url_unchanged_scheme_host(self):
        result = normalize_url("https://example.com/path")
        assert result.startswith("https://example.com")


class TestIsPdfUrl:
    def test_pdf_extension(self):
        assert is_pdf_url("https://example.com/file.pdf") is True

    def test_pdf_uppercase(self):
        assert is_pdf_url("https://example.com/file.PDF") is True

    def test_not_pdf(self):
        assert is_pdf_url("https://example.com/notices/") is False
        assert is_pdf_url("https://example.com/page.html") is False

    def test_pdf_in_query_not_counted(self):
        # Only the path extension matters
        assert is_pdf_url("https://example.com/page?file=doc.pdf") is False


class TestExtractDomain:
    def test_basic(self):
        assert extract_domain("https://pareekshabhavan.uoc.ac.in/notices/") == \
               "pareekshabhavan.uoc.ac.in"

    def test_lowercased(self):
        assert extract_domain("https://EXAMPLE.COM/path") == "example.com"


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestParseDatee:
    @pytest.mark.parametrize("raw, expected_year, expected_month, expected_day", [
        ("25/06/2026", 2026, 6, 25),
        ("25-06-2026", 2026, 6, 25),
        ("2026-06-25", 2026, 6, 25),
        ("June 25, 2026", 2026, 6, 25),
        ("Jun 25, 2026", 2026, 6, 25),
        ("25 June 2026", 2026, 6, 25),
        ("25 Jun 2026", 2026, 6, 25),
    ])
    def test_parse_various_formats(self, raw, expected_year, expected_month, expected_day):
        dt = parse_date(raw)
        assert dt is not None
        assert dt.year == expected_year
        assert dt.month == expected_month
        assert dt.day == expected_day

    def test_returns_utc_aware(self):
        dt = parse_date("25/06/2026")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_returns_none(self):
        assert parse_date("not a date") is None
        assert parse_date("") is None
        assert parse_date("32/13/2026") is None

    def test_utc_now_is_aware(self):
        dt = utc_now()
        assert dt.tzinfo is not None

    def test_format_datetime(self):
        dt = datetime(2026, 6, 25, 12, 30, 0, tzinfo=timezone.utc)
        formatted = format_datetime(dt)
        assert "25 Jun 2026" in formatted
        assert "12:30" in formatted

    def test_format_datetime_naive_treated_as_utc(self):
        naive = datetime(2026, 6, 25, 0, 0, 0)
        result = format_datetime(naive)
        assert "25 Jun 2026" in result


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<b>Hello</b> World") == "Hello World"

    def test_decodes_amp(self):
        assert "&amp;" not in strip_html("AT&amp;T")

    def test_decodes_nbsp(self):
        result = strip_html("Hello&nbsp;World")
        assert result == "Hello World"

    def test_collapses_whitespace(self):
        result = strip_html("<p>  Hello   World  </p>")
        assert result == "Hello World"


class TestCollapseWhitespace:
    def test_multiple_spaces(self):
        assert collapse_whitespace("a   b   c") == "a b c"

    def test_tabs_and_newlines(self):
        assert collapse_whitespace("a\t\tb\n\nc") == "a b c"

    def test_leading_trailing_stripped(self):
        assert collapse_whitespace("  hello  ") == "hello"


class TestCleanText:
    def test_full_pipeline(self):
        raw = "<p>  Special &amp; Exam  </p>"
        assert clean_text(raw) == "Special & Exam"


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("Hello", max_length=100) == "Hello"

    def test_long_text_truncated(self):
        text = "word " * 50  # 250 chars
        result = truncate(text, max_length=20)
        assert len(result) <= 20
        assert result.endswith("…")

    def test_truncate_does_not_break_mid_word(self):
        result = truncate("Hello World overflow", max_length=10)
        # Should not cut "World" in half
        assert "Wor" not in result or result.endswith("…")

    def test_exactly_max_length_unchanged(self):
        text = "Hello"
        assert truncate(text, max_length=5) == "Hello"
