"""
src/scraper.py — Website scraper for Pareeksha Bhavan.

Architecture
------------
Three focused classes keep responsibilities separate:

PageFetcher
    Makes HTTP GET requests with a browser User-Agent, configurable timeout,
    and exponential back-off retry on network errors.  Returns raw HTML or
    JSON bytes.  Never parses content — only fetches.

PageParser
    Pure HTML/JSON parser.  Receives a string of HTML (or a JSON payload) and
    returns a list of ``ScrapedItem`` objects.  No network I/O — fully
    testable with mocked HTML fixtures.

    Parsing strategy (tried in order, first match wins):
    1. Table rows  — ``<table> <tbody> <tr>`` containing a link/PDF
    2. List items  — ``<ul>/<ol> <li>`` containing a link
    3. Card divs   — ``<div class="...item...">`` containing a link
    4. Embedded JSON — ``<script type="application/json">`` or Angular
       transfer-state blocks with notification arrays

SiteScraper
    Orchestrates fetching the Notifications page of the Pareeksha Bhavan
    site.  Deduplicates by URL before returning.  Hands the raw HTML to
    PageParser; never touches network internals.

Resilience
----------
- Unknown HTML structure → returns empty list, logs a debug message
- Changed column order / missing columns → fields default to empty string
- Network error (after retries) → raises ``ScraperError`` which the
  caller catches; the monitor continues without crashing
- 404 / non-200 → logged as warning, page skipped

Extension point
---------------
The site is a JavaScript SPA (Angular).  When ``requests`` returns only the
shell HTML, ``PageParser`` finds nothing and returns ``[]``.  To support
JS-rendered content in a future phase, replace ``PageFetcher.fetch_html()``
with a Playwright/Selenium call — the rest of the pipeline is unchanged.

Usage
-----
    from src.scraper import SiteScraper
    from src.config import get_settings

    scraper = SiteScraper.from_settings(get_settings())
    items   = scraper.scrape_all()
    for item in items:
        print(item.title, item.pdf_url)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from src.logger import get_logger
from src.utils import normalize_url, parse_date, clean_text, make_notification_id

if TYPE_CHECKING:
    from src.config import Settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Pages to monitor, as (category_name, path) pairs.
# Paths are resolved against base_url at runtime.
_MONITORED_PAGES: list[tuple[str, str]] = [
    ("Notifications", "index.php/examination/notifications"),
]

# API endpoint candidates — tried in order; first 200 JSON response wins.
_API_CANDIDATES: list[str] = [
    "/api/notifications",
    "/api/v1/notifications",
    "/api/getNotifications",
    "/api/Notification/GetAll",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScrapedItem:
    """
    A single notification item as scraped from the website.

    This is the raw output of the scraper — it has not yet been matched
    against keywords or checked for duplicates.

    Attributes
    ----------
    title:
        Notification title, HTML-stripped and whitespace-collapsed.
    page_url:
        Canonical URL of the page this item was found on.
    pdf_url:
        Direct URL to the linked PDF, if any.  Empty string if none found.
    publication_date_raw:
        Raw date string as found on the page (e.g. ``"25/06/2026"``).
        Parsed into a ``datetime`` via ``src.utils.parse_date`` when
        converting to a ``Notification``.
    category:
        Section name (e.g. ``"Notifications"``).
    notification_id:
        Stable 16-char SHA-256 hash of (page_url, title).
    """

    title: str
    page_url: str
    pdf_url: str = ""
    publication_date_raw: str = ""
    category: str = ""
    notification_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.notification_id = make_notification_id(
            url=self.pdf_url or self.page_url,
            title=self.title,
        )

    @property
    def has_pdf(self) -> bool:
        return bool(self.pdf_url)

    def __repr__(self) -> str:
        return (
            f"ScrapedItem(id={self.notification_id!r}, "
            f"title={self.title[:60]!r}, "
            f"has_pdf={self.has_pdf})"
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ScraperError(Exception):
    """Raised when a page cannot be fetched after all retries."""


# ---------------------------------------------------------------------------
# PageFetcher
# ---------------------------------------------------------------------------

class PageFetcher:
    """
    HTTP client for fetching web pages.

    Retries on connection/timeout errors using exponential back-off.
    Auth/config errors (404, 403) are returned as ``None`` without retry.

    Parameters
    ----------
    timeout:
        Request timeout in seconds.
    max_retries:
        Maximum retry attempts on network errors.
    wait_seconds:
        Base back-off interval (doubles on each retry, capped at 30 s).
        Pass ``0`` in tests to skip sleeping.
    extra_headers:
        Optional additional headers merged with the default browser headers.
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        wait_seconds: float = 1.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._wait_seconds = wait_seconds
        self._session = requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)
        if extra_headers:
            self._session.headers.update(extra_headers)

    # ── Public ───────────────────────────────────────────────────────────────

    def fetch_html(self, url: str) -> str | None:
        """
        Fetch *url* and return the response body as a UTF-8 string.

        Parameters
        ----------
        url:
            Absolute URL to fetch.

        Returns
        -------
        str | None
            Response body on HTTP 200.
            ``None`` on non-200 status (logged as warning).

        Raises
        ------
        ScraperError
            After all retries are exhausted on network errors.
        """
        response = self._get_with_retry(url)
        if response is None:
            return None
        if response.status_code != 200:
            log.warning(
                "scraper_non_200",
                url=url,
                status_code=response.status_code,
            )
            return None
        return response.text

    def fetch_json(self, url: str) -> list | dict | None:
        """
        Fetch *url* expecting a JSON response.

        Returns
        -------
        list | dict | None
            Parsed JSON on HTTP 200 with valid JSON body.
            ``None`` on non-200 or invalid JSON.
        """
        response = self._get_with_retry(url)
        if response is None:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            return None

    def close(self) -> None:
        """Close the underlying requests Session."""
        self._session.close()

    def __enter__(self) -> "PageFetcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _get_with_retry(self, url: str) -> requests.Response | None:
        """
        Make a GET request, retrying on network/timeout errors.

        Returns the ``Response`` on any HTTP reply (including 4xx/5xx).
        Returns ``None`` if every attempt raises a network exception.
        Raises ``ScraperError`` after all retries are exhausted.
        """
        last_exc: Exception | None = None
        wait = self._wait_seconds

        for attempt in range(1, self._max_retries + 1):
            try:
                log.debug("scraper_fetch", url=url, attempt=attempt)
                response = self._session.get(url, timeout=self._timeout)
                log.debug(
                    "scraper_response",
                    url=url,
                    status_code=response.status_code,
                    content_length=len(response.content),
                )
                return response

            except requests.exceptions.Timeout as exc:
                last_exc = exc
                log.warning(
                    "scraper_timeout",
                    url=url,
                    attempt=attempt,
                    timeout=self._timeout,
                )
            except requests.exceptions.ConnectionError as exc:
                last_exc = exc
                log.warning(
                    "scraper_connection_error",
                    url=url,
                    attempt=attempt,
                    error=str(exc),
                )
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                log.warning(
                    "scraper_request_error",
                    url=url,
                    attempt=attempt,
                    error=str(exc),
                )

            if attempt < self._max_retries:
                log.debug("scraper_retry_wait", wait_seconds=wait, next_attempt=attempt + 1)
                time.sleep(wait)
                wait = min(wait * 2, 30.0)

        raise ScraperError(
            f"Failed to fetch {url!r} after {self._max_retries} attempts. "
            f"Last error: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# PageParser
# ---------------------------------------------------------------------------

class PageParser:
    """
    Parses an HTML string (or JSON payload) into a list of ``ScrapedItem``.

    All methods are stateless class/static methods — the class is a namespace
    for parsing strategies.  No network I/O.

    Strategy order (HTML parsing)
    -----------------------------
    1. Table rows  — most common for university portals
    2. List items  — common for news/notification lists
    3. Card/div    — common for modern Bootstrap layouts
    4. Embedded JSON — Angular transfer state or inline data blobs
    """

    @classmethod
    def parse(
        cls,
        html: str,
        base_url: str,
        category: str,
        page_url: str,
    ) -> list[ScrapedItem]:
        """
        Parse *html* and return all notification items found.

        Tries each strategy in order; returns results from the first that
        finds at least one item with a non-empty title.

        Parameters
        ----------
        html:
            Raw HTML string as returned by the HTTP response.
        base_url:
            Site root URL used to resolve relative hrefs.
        category:
            Section label (e.g. ``"Notifications"``).
        page_url:
            Absolute URL of the page (used as fallback ``page_url`` on items).
        """
        if not html or not html.strip():
            log.debug("scraper_parse_empty_html", page_url=page_url)
            return []

        soup = BeautifulSoup(html, "lxml")

        for strategy in (
            cls._parse_tables,
            cls._parse_lists,
            cls._parse_cards,
            cls._parse_embedded_json,
        ):
            try:
                items = strategy(soup, base_url, category, page_url)
                if items:
                    log.info(
                        "scraper_parse_success",
                        strategy=strategy.__name__,
                        count=len(items),
                        page_url=page_url,
                    )
                    return items
            except Exception as exc:
                log.debug(
                    "scraper_strategy_error",
                    strategy=strategy.__name__,
                    error=str(exc),
                    page_url=page_url,
                )

        log.debug("scraper_no_items_found", page_url=page_url)
        return []

    @classmethod
    def parse_json(
        cls,
        data: list | dict,
        base_url: str,
        category: str,
    ) -> list[ScrapedItem]:
        """
        Parse a JSON API response into ``ScrapedItem`` objects.

        Handles both list-of-objects and dict-with-items-key shapes.
        Field names are matched case-insensitively against common patterns.
        """
        items_list: list[dict] = []

        if isinstance(data, list):
            items_list = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            # Common API wrapping patterns
            for key in ("data", "items", "notifications", "news", "results", "records"):
                if isinstance(data.get(key), list):
                    items_list = data[key]
                    break

        results: list[ScrapedItem] = []
        for raw in items_list:
            title = cls._find_field(raw, ("title", "subject", "name", "heading", "description"))
            date_raw = cls._find_field(raw, ("date", "publishDate", "publish_date", "createdAt", "created_at"))
            href = cls._find_field(raw, ("url", "link", "pdf", "pdfUrl", "pdf_url", "fileUrl", "file_url", "attachment"))
            if not title:
                continue
            pdf_url = normalize_url(href, base_url) if href and href.lower().endswith(".pdf") else ""
            page_url = normalize_url(href, base_url) if href and not href.lower().endswith(".pdf") else base_url
            results.append(ScrapedItem(
                title=clean_text(title),
                page_url=page_url,
                pdf_url=pdf_url,
                publication_date_raw=str(date_raw) if date_raw else "",
                category=category,
            ))
        return results

    # ── Private strategies ───────────────────────────────────────────────────

    @classmethod
    def _parse_tables(
        cls,
        soup: BeautifulSoup,
        base_url: str,
        category: str,
        page_url: str,
    ) -> list[ScrapedItem]:
        """
        Strategy 1: find notification tables.

        Looks for ``<table>`` elements that contain rows with at least one
        ``<a>`` link.  Skips header rows (``<th>``-only rows).
        """
        items: list[ScrapedItem] = []

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:   # need at least header + one data row
                continue

            for row in rows:
                # Skip pure-header rows
                if row.find("th") and not row.find("td"):
                    continue

                item = cls._extract_from_row(row, base_url, category, page_url)
                if item:
                    items.append(item)

        return items

    @classmethod
    def _parse_lists(
        cls,
        soup: BeautifulSoup,
        base_url: str,
        category: str,
        page_url: str,
    ) -> list[ScrapedItem]:
        """
        Strategy 2: find notification ``<ul>`` / ``<ol>`` lists.

        Only considers lists with at least 2 items to avoid nav menus.
        """
        items: list[ScrapedItem] = []

        for ul in soup.find_all(["ul", "ol"]):
            lis = ul.find_all("li", recursive=False)
            if len(lis) < 2:
                continue

            for li in lis:
                item = cls._extract_from_block(li, base_url, category, page_url)
                if item:
                    items.append(item)

        return items

    @classmethod
    def _parse_cards(
        cls,
        soup: BeautifulSoup,
        base_url: str,
        category: str,
        page_url: str,
    ) -> list[ScrapedItem]:
        """
        Strategy 3: find Bootstrap-style card / notification divs.

        Looks for ``<div>`` elements whose class contains "item", "card",
        "notification", "notice", or "news" and which contain a link.
        """
        CARD_CLASSES = re.compile(
            r"\b(item|card|notification|notice|news|result)\b", re.IGNORECASE
        )
        items: list[ScrapedItem] = []

        for div in soup.find_all("div", class_=True):
            classes = " ".join(div.get("class", []))
            if not CARD_CLASSES.search(classes):
                continue
            if not div.find("a"):
                continue
            item = cls._extract_from_block(div, base_url, category, page_url)
            if item:
                items.append(item)

        return items

    @classmethod
    def _parse_embedded_json(
        cls,
        soup: BeautifulSoup,
        base_url: str,
        category: str,
        page_url: str,
    ) -> list[ScrapedItem]:
        """
        Strategy 4: look for JSON data embedded in ``<script>`` tags.

        Angular apps sometimes embed transfer-state JSON blobs.  Tries to
        parse any ``<script>`` that contains ``[`` or ``{`` and looks like
        a notifications array.
        """
        items: list[ScrapedItem] = []

        for script in soup.find_all("script"):
            text = script.string or ""
            if not text.strip():
                continue

            # Angular transfer state: ngsw:db:...
            # Or plain JSON assignment: var data = [...]
            # Try to find any JSON array containing objects with "title"
            matches = re.findall(r"(\[[\s\S]{20,}\]|\{[\s\S]{20,}\})", text)
            for match in matches:
                try:
                    data = json.loads(match)
                    parsed = cls.parse_json(data, base_url, category)
                    if parsed:
                        items.extend(parsed)
                        break
                except (json.JSONDecodeError, ValueError):
                    continue
            if items:
                break

        return items

    # ── Element extraction helpers ────────────────────────────────────────────

    @classmethod
    def _extract_from_row(
        cls,
        row: Tag,
        base_url: str,
        category: str,
        page_url: str,
    ) -> ScrapedItem | None:
        """Extract a ScrapedItem from a ``<tr>`` row."""
        cells = row.find_all("td")
        if not cells:
            return None

        title, href, date_raw = cls._extract_fields_from_cells(cells, base_url)
        if not title:
            return None

        return cls._build_item(title, href, date_raw, base_url, category, page_url)

    @classmethod
    def _extract_from_block(
        cls,
        block: Tag,
        base_url: str,
        category: str,
        page_url: str,
    ) -> ScrapedItem | None:
        """Extract a ScrapedItem from a ``<li>`` or ``<div>`` block."""
        # Title: prefer a link's text, then the block's full text
        link = block.find("a", href=True)
        if not link:
            return None

        title = clean_text(link.get_text())
        if not title:
            title = clean_text(block.get_text())
        if not title:
            return None

        href = link.get("href", "")
        date_raw = cls._extract_date_text(block)

        return cls._build_item(title, href, date_raw, base_url, category, page_url)

    @classmethod
    def _extract_fields_from_cells(
        cls,
        cells: list[Tag],
        base_url: str,
    ) -> tuple[str, str, str]:
        """
        Return (title, href, date_raw) from a list of ``<td>`` elements.

        Strategy:
        - The cell with a ``<a>`` link is most likely the title cell.
        - Cells containing digits matching a date pattern are the date cell.
        - PDF links (href ending in .pdf) are extracted as the download URL.
        """
        title = ""
        href = ""
        date_raw = ""

        for cell in cells:
            # Look for a PDF or page link
            link = cell.find("a", href=True)
            if link:
                cell_text = clean_text(link.get_text())
                link_href = link.get("href", "")

                if not title and cell_text:
                    title = cell_text

                if link_href:
                    if link_href.lower().endswith(".pdf"):
                        href = link_href
                    elif not href:
                        href = link_href

            # Look for a date pattern (DD/MM/YYYY, DD-MM-YYYY, etc.)
            if not date_raw:
                cell_text = clean_text(cell.get_text())
                if re.search(r"\d{2}[/\-\.]\d{2}[/\-\.]\d{4}", cell_text):
                    date_raw = cell_text

        return title, href, date_raw

    @staticmethod
    def _extract_date_text(element: Tag) -> str:
        """
        Find a date-like string within *element*.

        Checks ``<span>``, ``<small>``, ``<time>`` tags first, then falls
        back to the element's full text.
        """
        for tag in element.find_all(["span", "small", "time", "p", "div"]):
            text = clean_text(tag.get_text())
            if re.search(r"\d{2}[/\-\.]\d{2}[/\-\.]\d{4}", text):
                return text
        return ""

    @staticmethod
    def _build_item(
        title: str,
        href: str,
        date_raw: str,
        base_url: str,
        category: str,
        page_url: str,
    ) -> ScrapedItem | None:
        """Construct and return a ScrapedItem, or None if title is empty."""
        title = clean_text(title)
        if not title:
            return None

        pdf_url = ""
        item_page_url = page_url

        if href:
            abs_href = normalize_url(href, base_url)
            if href.lower().endswith(".pdf"):
                pdf_url = abs_href
            else:
                item_page_url = abs_href

        return ScrapedItem(
            title=title,
            page_url=item_page_url,
            pdf_url=pdf_url,
            publication_date_raw=date_raw,
            category=category,
        )

    @staticmethod
    def _find_field(data: dict, keys: tuple[str, ...]) -> str:
        """Case-insensitive field lookup across multiple candidate key names."""
        data_lower = {k.lower(): v for k, v in data.items()}
        for key in keys:
            val = data_lower.get(key.lower())
            if val and isinstance(val, str):
                return val
        return ""


# ---------------------------------------------------------------------------
# SiteScraper
# ---------------------------------------------------------------------------

class SiteScraper:
    """
    Orchestrates scraping the Notifications page of the Pareeksha Bhavan site.

    Parameters
    ----------
    base_url:
        Site root URL (e.g. ``"https://pareekshabhavan.uoc.ac.in/"``).
    fetcher:
        ``PageFetcher`` instance.  Inject a mock in tests.
    parser:
        ``PageParser`` class.  Override in tests for custom parsing.
    """

    def __init__(
        self,
        base_url: str,
        fetcher: PageFetcher | None = None,
        parser: type[PageParser] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._fetcher = fetcher or PageFetcher()
        self._parser = parser or PageParser

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SiteScraper":
        """Build a ``SiteScraper`` from application settings."""
        fetcher = PageFetcher(
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )
        return cls(base_url=settings.base_url, fetcher=fetcher)

    # ── Public API ────────────────────────────────────────────────────────────

    def scrape_all(self) -> list[ScrapedItem]:
        """
        Fetch and parse all monitored pages.

        Returns
        -------
        list[ScrapedItem]
            Deduplicated items from all pages, ordered by category then
            discovery order.  Empty list if the site is unreachable or
            returns no parseable content.
        """
        log.info("scraper_start", base_url=self._base_url)

        # Try REST API endpoints first — direct JSON is more reliable than HTML
        api_items = self._try_api_endpoints()
        if api_items:
            log.info("scraper_api_success", count=len(api_items))
            return self._deduplicate(api_items)

        # Fall back to HTML scraping
        html_items = self._scrape_html_pages()
        log.info(
            "scraper_complete",
            html_items=len(html_items),
            unique=len(self._deduplicate(html_items)),
        )
        return self._deduplicate(html_items)

    def scrape_page(self, path: str, category: str) -> list[ScrapedItem]:
        """
        Fetch and parse a single page.

        Parameters
        ----------
        path:
            URL path relative to ``base_url`` (e.g. ``"/notifications"``).
        category:
            Section label for items found on this page.

        Returns
        -------
        list[ScrapedItem]
            Items found, or empty list on error / no content.
        """
        url = self._resolve(path)
        try:
            html = self._fetcher.fetch_html(url)
        except ScraperError as exc:
            log.error("scraper_page_failed", url=url, error=str(exc))
            return []

        if not html:
            log.debug("scraper_page_empty", url=url)
            return []

        return self._parser.parse(
            html=html,
            base_url=self._base_url,
            category=category,
            page_url=url,
        )

    # ── Internal ─────────────────────────────────────────────────────────────

    def _try_api_endpoints(self) -> list[ScrapedItem]:
        """Try known REST API paths; return items from the first that responds."""
        for path in _API_CANDIDATES:
            url = self._resolve(path)
            try:
                data = self._fetcher.fetch_json(url)
            except ScraperError:
                continue
            if data is None:
                continue
            category = "Notifications"
            items = self._parser.parse_json(data, self._base_url, category)
            if items:
                log.info("scraper_api_found", path=path, count=len(items))
                return items
        return []

    def _scrape_html_pages(self) -> list[ScrapedItem]:
        """Fetch and parse each entry in ``_MONITORED_PAGES``."""
        all_items: list[ScrapedItem] = []
        seen_pages: set[str] = set()

        for category, path in _MONITORED_PAGES:
            url = self._resolve(path)
            if url in seen_pages:
                continue
            seen_pages.add(url)

            log.info("scraper_fetching_page", url=url, category=category)
            items = self.scrape_page(path, category)
            log.info(
                "scraper_page_complete",
                url=url,
                category=category,
                items_found=len(items),
            )
            all_items.extend(items)

        return all_items

    def _resolve(self, path: str) -> str:
        """Resolve *path* against ``base_url``."""
        if path.startswith("http"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _deduplicate(items: list[ScrapedItem]) -> list[ScrapedItem]:
        """
        Remove duplicate items, keeping the first occurrence.

        Deduplication key: ``notification_id`` (SHA-256 of url + title).
        """
        seen: set[str] = set()
        unique: list[ScrapedItem] = []
        for item in items:
            if item.notification_id not in seen:
                seen.add(item.notification_id)
                unique.append(item)
        return unique
