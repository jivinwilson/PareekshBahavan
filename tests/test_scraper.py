"""
tests/test_scraper.py — Unit tests for src/scraper.py

All network calls are mocked with the ``responses`` library.
No real HTTP traffic occurs.

HTML fixtures model the typical University of Calicut Pareeksha Bhavan
structure: Bootstrap table-based notification lists.

Coverage
--------
PageParser
  - Table with PDF links
  - Table without PDF links (page-link only)
  - List items
  - Card/div items
  - Embedded JSON in <script>
  - Empty HTML
  - Malformed HTML (no crash)
  - Deduplication across multiple items

PageFetcher
  - Successful fetch
  - 404 returns None
  - Timeout raises ScraperError after retries
  - Connection error raises ScraperError after retries
  - Retry count verified

SiteScraper
  - scrape_all() success path
  - scrape_all() with JSON API response
  - scrape_page() with successful HTML
  - scrape_page() returns empty on non-200
  - scrape_page() returns empty on network error
  - Deduplication across pages
  - from_settings() factory
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import responses as resp_lib

from src.scraper import (
    PageFetcher,
    PageParser,
    ScrapedItem,
    ScraperError,
    SiteScraper,
)

BASE_URL = "https://pareekshabhavan.uoc.ac.in"

# ---------------------------------------------------------------------------
# HTML fixtures — realistic University of Calicut portal structure
# ---------------------------------------------------------------------------

HTML_TABLE_WITH_PDF = """
<!DOCTYPE html>
<html>
<head><title>Notifications</title></head>
<body>
<div class="container">
  <h2>Notifications</h2>
  <table class="table table-bordered table-striped">
    <thead>
      <tr>
        <th>Sl.No</th>
        <th>Subject</th>
        <th>Date</th>
        <th>Download</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>1</td>
        <td><a href="/uploads/special_exam_2026.pdf">Special Examination - B.Sc Computer Science (2020 Admission) CBCSS</a></td>
        <td>25/06/2026</td>
        <td><a href="/uploads/special_exam_2026.pdf">Download</a></td>
      </tr>
      <tr>
        <td>2</td>
        <td><a href="/uploads/timetable_june_2026.pdf">Time Table - Third Semester Examinations June 2026</a></td>
        <td>20/06/2026</td>
        <td><a href="/uploads/timetable_june_2026.pdf">Download</a></td>
      </tr>
      <tr>
        <td>3</td>
        <td><a href="/uploads/results_april_2026.pdf">Results - One Time Supplementary Examinations April 2026</a></td>
        <td>15/06/2026</td>
        <td><a href="/uploads/results_april_2026.pdf">Download</a></td>
      </tr>
    </tbody>
  </table>
</div>
</body>
</html>
"""

HTML_TABLE_NO_PDF = """
<!DOCTYPE html>
<html>
<body>
<table class="table">
  <thead><tr><th>Title</th><th>Date</th></tr></thead>
  <tbody>
    <tr>
      <td><a href="/notifications/123">Exam Notification - Regular</a></td>
      <td>10/06/2026</td>
    </tr>
    <tr>
      <td><a href="/notifications/124">Hall Ticket Download</a></td>
      <td>05/06/2026</td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""

HTML_LIST_ITEMS = """
<!DOCTYPE html>
<html>
<body>
<div class="latest-news">
  <h3>Latest News</h3>
  <ul class="notification-list">
    <li>
      <a href="/uploads/circular_june_2026.pdf">Circular: Special Exam Schedule June 2026</a>
      <span class="date">22/06/2026</span>
    </li>
    <li>
      <a href="/uploads/notice_july_2026.pdf">Notice: Exhausted Chances - B.Sc Students</a>
      <span class="date">18/06/2026</span>
    </li>
    <li>
      <a href="/news/125">General Notice for All Students</a>
      <span class="date">12/06/2026</span>
    </li>
  </ul>
</div>
</body>
</html>
"""

HTML_CARD_DIVS = """
<!DOCTYPE html>
<html>
<body>
<div class="notifications-container">
  <div class="notification-item card">
    <div class="card-body">
      <a href="/uploads/card_exam.pdf">CBCSS Exam Notification 2026</a>
      <small class="text-muted">25/06/2026</small>
    </div>
  </div>
  <div class="notification-item card">
    <div class="card-body">
      <a href="/uploads/card_result.pdf">Results: Third Semester B.Sc</a>
      <small class="text-muted">20/06/2026</small>
    </div>
  </div>
</div>
</body>
</html>
"""

HTML_EMBEDDED_JSON = """
<!DOCTYPE html>
<html>
<body>
<app-root></app-root>
<script type="application/json" id="transfer-state">
[
  {
    "title": "Special Examination Notification - B.Sc Computer Science",
    "date": "25/06/2026",
    "pdfUrl": "/uploads/embedded_exam.pdf",
    "url": "https://pareekshabhavan.uoc.ac.in/notifications/99"
  },
  {
    "title": "Time Table June 2026 - Third Semester",
    "date": "20/06/2026",
    "pdfUrl": "/uploads/embedded_tt.pdf",
    "url": "https://pareekshabhavan.uoc.ac.in/notifications/100"
  }
]
</script>
</body>
</html>
"""

HTML_EMPTY_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Notifications</title></head>
<body>
<app-root></app-root>
</body>
</html>
"""

HTML_NO_ITEMS_TABLE = """
<!DOCTYPE html>
<html>
<body>
<table>
  <thead><tr><th>Title</th><th>Date</th></tr></thead>
  <tbody></tbody>
</table>
</body>
</html>
"""

HTML_MALFORMED = """
<!DOCTYPE html>
<html>
<body>
<table>
  <tr><td><a href="/uploads/malformed.pdf">Exam Notice</td>
  <tr><td>25/06/2026<td>
  <!-- unclosed tags everywhere -->
  <div class=broken><a href=/no-quotes>Link without quotes</div>
</body>
"""

JSON_API_RESPONSE = [
    {
        "title": "Special Examination - CBCSS B.Sc Computer Science 2020",
        "date": "25/06/2026",
        "pdfUrl": "/uploads/api_exam.pdf",
    },
    {
        "title": "One Time Supplementary Exam Results",
        "publishDate": "20/06/2026",
        "pdf_url": "/uploads/api_results.pdf",
    },
]


# ---------------------------------------------------------------------------
# ScrapedItem model tests
# ---------------------------------------------------------------------------

class TestScrapedItem:
    def test_has_pdf_true(self):
        item = ScrapedItem(
            title="Test",
            page_url="https://example.com",
            pdf_url="https://example.com/file.pdf",
        )
        assert item.has_pdf is True

    def test_has_pdf_false(self):
        item = ScrapedItem(title="Test", page_url="https://example.com")
        assert item.has_pdf is False

    def test_notification_id_generated(self):
        item = ScrapedItem(title="Test", page_url="https://example.com")
        assert len(item.notification_id) == 16
        assert isinstance(item.notification_id, str)

    def test_notification_id_stable(self):
        a = ScrapedItem(title="Test", page_url="https://example.com")
        b = ScrapedItem(title="Test", page_url="https://example.com")
        assert a.notification_id == b.notification_id

    def test_notification_id_different_for_different_urls(self):
        a = ScrapedItem(title="Test", page_url="https://a.com")
        b = ScrapedItem(title="Test", page_url="https://b.com")
        assert a.notification_id != b.notification_id


# ---------------------------------------------------------------------------
# PageParser — HTML strategies
# ---------------------------------------------------------------------------

class TestPageParserTable:
    def test_table_with_pdf_links(self):
        items = PageParser.parse(HTML_TABLE_WITH_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        assert len(items) == 3

    def test_table_titles_extracted(self):
        items = PageParser.parse(HTML_TABLE_WITH_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        titles = [i.title for i in items]
        assert any("Special Examination" in t for t in titles)
        assert any("Time Table" in t for t in titles)

    def test_table_pdf_urls_absolute(self):
        items = PageParser.parse(HTML_TABLE_WITH_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        pdf_items = [i for i in items if i.has_pdf]
        assert len(pdf_items) == 3
        for item in pdf_items:
            assert item.pdf_url.startswith("https://")

    def test_table_dates_extracted(self):
        items = PageParser.parse(HTML_TABLE_WITH_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        dates = [i.publication_date_raw for i in items if i.publication_date_raw]
        assert len(dates) == 3
        assert "25/06/2026" in dates

    def test_table_category_set(self):
        items = PageParser.parse(HTML_TABLE_WITH_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        for item in items:
            assert item.category == "Notifications"

    def test_table_without_pdf(self):
        items = PageParser.parse(HTML_TABLE_NO_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        assert len(items) == 2
        for item in items:
            assert not item.has_pdf

    def test_table_page_url_set_when_no_pdf(self):
        items = PageParser.parse(HTML_TABLE_NO_PDF, BASE_URL, "Notifications", BASE_URL + "/")
        assert all(item.page_url.startswith("https://") for item in items)

    def test_empty_table_body_returns_empty(self):
        items = PageParser.parse(HTML_NO_ITEMS_TABLE, BASE_URL, "Notifications", BASE_URL + "/")
        assert items == []


class TestPageParserList:
    def test_list_items_found(self):
        items = PageParser.parse(HTML_LIST_ITEMS, BASE_URL, "Latest News", BASE_URL + "/")
        assert len(items) == 3

    def test_list_titles_extracted(self):
        items = PageParser.parse(HTML_LIST_ITEMS, BASE_URL, "Latest News", BASE_URL + "/")
        titles = [i.title for i in items]
        assert any("Special Exam" in t for t in titles)
        assert any("Exhausted Chances" in t for t in titles)

    def test_list_pdf_urls_absolute(self):
        items = PageParser.parse(HTML_LIST_ITEMS, BASE_URL, "Latest News", BASE_URL + "/")
        pdf_items = [i for i in items if i.has_pdf]
        assert len(pdf_items) == 2  # 2 PDFs, 1 page link
        for item in pdf_items:
            assert item.pdf_url.startswith("https://")


class TestPageParserCards:
    def test_card_items_found(self):
        items = PageParser.parse(HTML_CARD_DIVS, BASE_URL, "Notifications", BASE_URL + "/")
        assert len(items) >= 2

    def test_card_titles_extracted(self):
        items = PageParser.parse(HTML_CARD_DIVS, BASE_URL, "Notifications", BASE_URL + "/")
        titles = [i.title for i in items]
        assert any("CBCSS" in t for t in titles)


class TestPageParserEmbeddedJSON:
    def test_embedded_json_found(self):
        items = PageParser.parse(HTML_EMBEDDED_JSON, BASE_URL, "Notifications", BASE_URL + "/")
        assert len(items) == 2

    def test_embedded_json_titles(self):
        items = PageParser.parse(HTML_EMBEDDED_JSON, BASE_URL, "Notifications", BASE_URL + "/")
        titles = [i.title for i in items]
        assert any("Special Examination" in t for t in titles)
        assert any("Time Table" in t for t in titles)


class TestPageParserEdgeCases:
    def test_empty_html_returns_empty(self):
        assert PageParser.parse("", BASE_URL, "Notifications", BASE_URL + "/") == []

    def test_whitespace_only_html_returns_empty(self):
        assert PageParser.parse("   \n\t  ", BASE_URL, "Notifications", BASE_URL + "/") == []

    def test_malformed_html_does_not_crash(self):
        # Should not raise; returns whatever it can parse
        items = PageParser.parse(HTML_MALFORMED, BASE_URL, "Notifications", BASE_URL + "/")
        assert isinstance(items, list)

    def test_plain_text_returns_empty(self):
        items = PageParser.parse("No HTML here at all.", BASE_URL, "N", BASE_URL + "/")
        assert items == []

    def test_angular_shell_returns_empty(self):
        items = PageParser.parse(HTML_EMPTY_PAGE, BASE_URL, "Notifications", BASE_URL + "/")
        assert items == []


class TestPageParserJSON:
    def test_json_list_parsed(self):
        items = PageParser.parse_json(JSON_API_RESPONSE, BASE_URL, "Notifications")
        assert len(items) == 2

    def test_json_titles_extracted(self):
        items = PageParser.parse_json(JSON_API_RESPONSE, BASE_URL, "Notifications")
        titles = [i.title for i in items]
        assert any("Special Examination" in t for t in titles)

    def test_json_pdf_urls_absolute(self):
        items = PageParser.parse_json(JSON_API_RESPONSE, BASE_URL, "Notifications")
        pdf_items = [i for i in items if i.has_pdf]
        assert len(pdf_items) == 2
        for item in pdf_items:
            assert item.pdf_url.startswith("https://")

    def test_json_wrapped_in_dict(self):
        wrapped = {"data": JSON_API_RESPONSE}
        items = PageParser.parse_json(wrapped, BASE_URL, "Notifications")
        assert len(items) == 2

    def test_json_empty_list(self):
        assert PageParser.parse_json([], BASE_URL, "Notifications") == []

    def test_json_no_title_field_skipped(self):
        data = [{"date": "25/06/2026", "pdf": "/file.pdf"}]  # no title
        assert PageParser.parse_json(data, BASE_URL, "Notifications") == []


# ---------------------------------------------------------------------------
# PageFetcher tests
# ---------------------------------------------------------------------------

class TestPageFetcher:
    @resp_lib.activate
    def test_fetch_html_success(self):
        resp_lib.add(resp_lib.GET, BASE_URL + "/notifications", body=HTML_TABLE_WITH_PDF, status=200)
        fetcher = PageFetcher(wait_seconds=0)
        result = fetcher.fetch_html(BASE_URL + "/notifications")
        assert result is not None
        assert "Special Examination" in result

    @resp_lib.activate
    def test_fetch_html_404_returns_none(self):
        resp_lib.add(resp_lib.GET, BASE_URL + "/missing", body="Not Found", status=404)
        fetcher = PageFetcher(wait_seconds=0)
        result = fetcher.fetch_html(BASE_URL + "/missing")
        assert result is None

    @resp_lib.activate
    def test_fetch_html_500_returns_none(self):
        resp_lib.add(resp_lib.GET, BASE_URL + "/error", body="Server Error", status=500)
        fetcher = PageFetcher(wait_seconds=0)
        result = fetcher.fetch_html(BASE_URL + "/error")
        assert result is None

    @resp_lib.activate
    def test_fetch_html_timeout_raises_scraper_error(self):
        import requests as req
        resp_lib.add(resp_lib.GET, BASE_URL + "/slow", body=req.exceptions.Timeout("timed out"))
        fetcher = PageFetcher(max_retries=1, wait_seconds=0)
        with pytest.raises(ScraperError, match="Failed to fetch"):
            fetcher.fetch_html(BASE_URL + "/slow")

    @resp_lib.activate
    def test_fetch_html_connection_error_raises_scraper_error(self):
        import requests as req
        resp_lib.add(resp_lib.GET, BASE_URL + "/down", body=req.exceptions.ConnectionError("refused"))
        fetcher = PageFetcher(max_retries=1, wait_seconds=0)
        with pytest.raises(ScraperError):
            fetcher.fetch_html(BASE_URL + "/down")

    @resp_lib.activate
    def test_retry_count_on_timeout(self):
        """Verifies exactly max_retries HTTP calls are made before giving up."""
        import requests as req
        for _ in range(3):
            resp_lib.add(resp_lib.GET, BASE_URL + "/flaky", body=req.exceptions.Timeout())
        fetcher = PageFetcher(max_retries=3, wait_seconds=0)
        with pytest.raises(ScraperError):
            fetcher.fetch_html(BASE_URL + "/flaky")
        assert len(resp_lib.calls) == 3

    @resp_lib.activate
    def test_succeeds_on_retry_after_transient_failure(self):
        """Fails once with connection error, succeeds on second attempt."""
        import requests as req
        resp_lib.add(resp_lib.GET, BASE_URL + "/flaky", body=req.exceptions.ConnectionError())
        resp_lib.add(resp_lib.GET, BASE_URL + "/flaky", body=HTML_TABLE_WITH_PDF, status=200)
        fetcher = PageFetcher(max_retries=2, wait_seconds=0)
        result = fetcher.fetch_html(BASE_URL + "/flaky")
        assert result is not None
        assert len(resp_lib.calls) == 2

    @resp_lib.activate
    def test_fetch_json_success(self):
        resp_lib.add(
            resp_lib.GET, BASE_URL + "/api/notifications",
            json=JSON_API_RESPONSE, status=200,
        )
        fetcher = PageFetcher(wait_seconds=0)
        result = fetcher.fetch_json(BASE_URL + "/api/notifications")
        assert isinstance(result, list)
        assert len(result) == 2

    @resp_lib.activate
    def test_fetch_json_non_200_returns_none(self):
        resp_lib.add(resp_lib.GET, BASE_URL + "/api/missing", status=404)
        fetcher = PageFetcher(wait_seconds=0)
        assert fetcher.fetch_json(BASE_URL + "/api/missing") is None


# ---------------------------------------------------------------------------
# SiteScraper tests
# ---------------------------------------------------------------------------

class TestSiteScraper:

    def _make_scraper(
        self,
        mock_html: str | None = None,
        mock_json: list | dict | None = None,
        fetch_raises: Exception | None = None,
    ) -> SiteScraper:
        """Build a SiteScraper with a mock PageFetcher."""
        mock_fetcher = MagicMock(spec=PageFetcher)
        if fetch_raises:
            mock_fetcher.fetch_html.side_effect = fetch_raises
            mock_fetcher.fetch_json.side_effect = fetch_raises
        else:
            mock_fetcher.fetch_html.return_value = mock_html
            mock_fetcher.fetch_json.return_value = mock_json
        return SiteScraper(base_url=BASE_URL, fetcher=mock_fetcher)

    def test_scrape_page_success(self):
        scraper = self._make_scraper(mock_html=HTML_TABLE_WITH_PDF)
        items = scraper.scrape_page("/notifications", "Notifications")
        assert len(items) == 3

    def test_scrape_page_empty_html_returns_empty(self):
        scraper = self._make_scraper(mock_html=HTML_EMPTY_PAGE)
        items = scraper.scrape_page("/notifications", "Notifications")
        assert items == []

    def test_scrape_page_none_html_returns_empty(self):
        scraper = self._make_scraper(mock_html=None)
        items = scraper.scrape_page("/notifications", "Notifications")
        assert items == []

    def test_scrape_page_scraper_error_returns_empty(self):
        """Network errors on a page are caught; empty list returned."""
        scraper = self._make_scraper(fetch_raises=ScraperError("down"))
        items = scraper.scrape_page("/notifications", "Notifications")
        assert items == []

    def test_scrape_all_deduplicates(self):
        """Same HTML served from multiple pages — deduplication must fire."""
        mock_fetcher = MagicMock(spec=PageFetcher)
        mock_fetcher.fetch_html.return_value = HTML_TABLE_WITH_PDF
        mock_fetcher.fetch_json.return_value = None  # no API
        scraper = SiteScraper(base_url=BASE_URL, fetcher=mock_fetcher)
        items = scraper.scrape_all()
        # The same 3 items returned from each page, but must be deduplicated
        ids = [i.notification_id for i in items]
        assert len(ids) == len(set(ids)), "Duplicates found after deduplication"

    def test_scrape_all_uses_api_when_available(self):
        """If a JSON API endpoint responds, HTML pages are skipped."""
        mock_fetcher = MagicMock(spec=PageFetcher)
        mock_fetcher.fetch_json.return_value = JSON_API_RESPONSE
        mock_fetcher.fetch_html.return_value = HTML_TABLE_WITH_PDF
        scraper = SiteScraper(base_url=BASE_URL, fetcher=mock_fetcher)
        items = scraper.scrape_all()
        # API was used — HTML would have 3 items; API has 2
        assert len(items) == 2
        mock_fetcher.fetch_html.assert_not_called()

    def test_scrape_all_falls_back_to_html_when_api_empty(self):
        """No API response → HTML scraping is attempted."""
        mock_fetcher = MagicMock(spec=PageFetcher)
        mock_fetcher.fetch_json.return_value = None
        mock_fetcher.fetch_html.return_value = HTML_TABLE_WITH_PDF
        scraper = SiteScraper(base_url=BASE_URL, fetcher=mock_fetcher)
        items = scraper.scrape_all()
        assert len(items) > 0
        mock_fetcher.fetch_html.assert_called()

    def test_scrape_all_all_errors_returns_empty(self):
        """All pages fail — returns empty list, does not raise."""
        scraper = self._make_scraper(fetch_raises=ScraperError("all down"))
        items = scraper.scrape_all()
        assert items == []

    def test_from_settings_factory(self):
        mock_settings = MagicMock()
        mock_settings.base_url = BASE_URL + "/"
        mock_settings.request_timeout = 30
        mock_settings.max_retries = 3
        scraper = SiteScraper.from_settings(mock_settings)
        assert isinstance(scraper, SiteScraper)

    def test_deduplication_static_method(self):
        a = ScrapedItem(title="Same", page_url="https://x.com", pdf_url="https://x.com/a.pdf")
        b = ScrapedItem(title="Same", page_url="https://x.com", pdf_url="https://x.com/a.pdf")
        c = ScrapedItem(title="Different", page_url="https://x.com", pdf_url="https://x.com/b.pdf")
        result = SiteScraper._deduplicate([a, b, c])
        assert len(result) == 2
