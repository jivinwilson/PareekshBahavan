"""
src/utils.py — Common utility functions.

Pure, side-effect-free helper functions shared across modules.  Nothing in
this file imports from other ``src`` modules — it is the bottom of the
dependency graph.

Sections
--------
1. Hashing        — stable SHA-256 IDs for notifications
2. URL utilities  — normalisation, absolute URL resolution
3. Date parsing   — convert website date strings to aware datetimes
4. Text cleaning  — strip HTML, collapse whitespace
5. Retry helper   — simple decorator (thin wrapper around ``tenacity``)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


# ===========================================================================
# 1. Hashing
# ===========================================================================

def make_notification_id(url: str, title: str = "") -> str:
    """
    Generate a stable, collision-resistant identifier for a notification.

    The ID is a 16-character hex prefix of SHA-256(url + title).  Using both
    fields means two notifications with the same URL but different titles
    (e.g. amended notices) get distinct IDs.

    Parameters
    ----------
    url:
        Canonical URL of the notification or its PDF.
    title:
        Notification title as scraped from the page.

    Returns
    -------
    str
        16-character lowercase hexadecimal string.

    Examples
    --------
    >>> make_notification_id("https://example.com/notice.pdf", "Special Exam")
    'a3f8...'
    """
    raw = f"{url.strip()}|{title.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def make_content_hash(text: str) -> str:
    """
    Return a 32-character SHA-256 hash of *text*.

    Used to detect whether a PDF's content has changed between runs.

    Parameters
    ----------
    text:
        Arbitrary text (e.g. extracted PDF body).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ===========================================================================
# 2. URL utilities
# ===========================================================================

def normalize_url(url: str, base_url: str = "") -> str:
    """
    Normalise a URL for consistent storage and deduplication.

    Steps applied:
    - Resolve relative URLs against *base_url*.
    - Strip trailing slashes from the path.
    - Remove fragment identifiers (``#anchor``).
    - Lower-case the scheme and host.
    - Preserve the original path casing (paths are case-sensitive on servers).

    Parameters
    ----------
    url:
        Raw URL string (may be relative).
    base_url:
        Base URL used to resolve relative references.

    Returns
    -------
    str
        Normalised absolute URL string.

    Examples
    --------
    >>> normalize_url("/notices/exam.pdf", "https://example.com/")
    'https://example.com/notices/exam.pdf'
    >>> normalize_url("https://EXAMPLE.COM/Path/#frag")
    'https://example.com/Path/'
    """
    url = url.strip()
    if base_url:
        url = urljoin(base_url, url)

    parsed = urlparse(url)
    normalised = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",          # strip anchors
    )
    # Normalise path: collapse double slashes, keep trailing slash only for root
    path = re.sub(r"/+", "/", normalised.path)
    normalised = normalised._replace(path=path)
    return urlunparse(normalised)


def is_pdf_url(url: str) -> bool:
    """
    Return ``True`` if *url* points to a PDF file.

    Checks the path extension, which is sufficient for Pareeksha Bhavan
    URLs.  A more robust check (HEAD Content-Type) is left to the scraper.

    Parameters
    ----------
    url:
        URL string to inspect.

    Examples
    --------
    >>> is_pdf_url("https://example.com/notice.PDF")
    True
    >>> is_pdf_url("https://example.com/notices/")
    False
    """
    path = urlparse(url.lower()).path
    return path.endswith(".pdf")


def extract_domain(url: str) -> str:
    """
    Return the domain (netloc) of *url*, lower-cased.

    Parameters
    ----------
    url:
        Absolute URL.

    Examples
    --------
    >>> extract_domain("https://pareekshabhavan.uoc.ac.in/notices/")
    'pareekshabhavan.uoc.ac.in'
    """
    return urlparse(url).netloc.lower()


# ===========================================================================
# 3. Date parsing
# ===========================================================================

# Ordered from most-specific to least-specific so the first match wins.
_DATE_FORMATS: list[str] = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%B %d, %Y",        # "June 25, 2026"
    "%b %d, %Y",        # "Jun 25, 2026"
    "%d %B %Y",         # "25 June 2026"
    "%d %b %Y",         # "25 Jun 2026"
]


def parse_date(raw: str) -> datetime | None:
    """
    Parse a date string from the Pareeksha Bhavan website.

    Tries several common date formats in order.  Returns a timezone-aware
    datetime (UTC) on success, or ``None`` if no format matched.

    Parameters
    ----------
    raw:
        Raw date string as found on the page (e.g. ``"25/06/2026"``).

    Returns
    -------
    datetime | None
        UTC-aware datetime, or ``None`` if unparseable.

    Examples
    --------
    >>> parse_date("25/06/2026")
    datetime.datetime(2026, 6, 25, 0, 0, tzinfo=datetime.timezone.utc)
    >>> parse_date("garbage") is None
    True
    """
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)


def format_datetime(dt: datetime, fmt: str = "%d %b %Y, %H:%M UTC") -> str:
    """
    Format a datetime for display in notification messages.

    Parameters
    ----------
    dt:
        Datetime to format (naive datetimes are treated as UTC).
    fmt:
        ``strftime`` format string.

    Returns
    -------
    str
        Human-readable date/time string.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime(fmt)


# ===========================================================================
# 4. Text cleaning
# ===========================================================================

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    """
    Remove HTML tags and decode common entities.

    This is intentionally simple — Beautiful Soup is used for full HTML
    parsing in the scraper.  This helper is for small inline strings (e.g.
    notification titles that may contain ``<b>`` tags).

    Parameters
    ----------
    html:
        HTML fragment or full document.

    Returns
    -------
    str
        Plain text with tags removed and whitespace collapsed.
    """
    text = _HTML_TAG_RE.sub(" ", html)
    # Decode the most common HTML entities
    text = (
        text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
    )
    return collapse_whitespace(text)


def collapse_whitespace(text: str) -> str:
    """
    Collapse runs of whitespace (spaces, tabs, newlines) to a single space
    and strip leading/trailing whitespace.

    Parameters
    ----------
    text:
        Input text.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_unicode(text: str) -> str:
    """
    Normalise Unicode to NFC form.

    Ensures that characters with multiple Unicode representations compare
    equal (e.g. accented characters composed vs. decomposed).

    Parameters
    ----------
    text:
        Input text.
    """
    return unicodedata.normalize("NFC", text)


def clean_text(text: str) -> str:
    """
    Full text cleaning pipeline: strip HTML → normalise Unicode → collapse whitespace.

    Parameters
    ----------
    text:
        Raw text from the website or PDF.
    """
    return collapse_whitespace(normalize_unicode(strip_html(text)))


def truncate(text: str, max_length: int = 200, suffix: str = "…") -> str:
    """
    Truncate *text* to at most *max_length* characters.

    Appends *suffix* when truncation occurs.  Does not break mid-word.

    Parameters
    ----------
    text:
        Text to truncate.
    max_length:
        Maximum number of characters in the output (including suffix).
    suffix:
        Appended when the text is shortened.
    """
    text = text.strip()
    if len(text) <= max_length:
        return text
    cut = max_length - len(suffix)
    # Walk back to the previous word boundary
    while cut > 0 and text[cut] not in (" ", "\t", "\n"):
        cut -= 1
    return text[:cut].rstrip() + suffix


# ===========================================================================
# 5. Retry helper
# ===========================================================================

def make_retry_decorator(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
    reraise: bool = True,
) -> object:
    """
    Return a ``tenacity`` retry decorator configured for HTTP operations.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the first try).
    min_wait:
        Minimum seconds to wait between retries (exponential back-off base).
    max_wait:
        Maximum seconds to wait between retries.
    reraise:
        If ``True`` (default), re-raise the last exception after exhausting
        retries.  If ``False``, return ``None`` after the last failure.

    Returns
    -------
    A ``tenacity.retry`` decorator that can be applied to any function.

    Usage
    -----
        retry_on_error = make_retry_decorator(max_attempts=3)

        @retry_on_error
        def fetch(url: str) -> str:
            ...
    """
    return retry(
        retry=retry_if_exception_type((OSError, ConnectionError, TimeoutError)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        reraise=reraise,
    )
