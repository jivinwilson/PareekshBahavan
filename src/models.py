"""
src/models.py — Shared domain models.

This module contains pure data models (dataclasses / Pydantic models) that are
used across multiple modules — scraper, matcher, Telegram sender, email sender.

Keeping them here prevents circular imports and ensures every module has the
same definition of a "Notification".

Models
------
Notification
    A single notification scraped from the Pareeksha Bhavan website.
    Produced by the scraper, enriched by the PDF extractor, filtered by the
    matcher, and consumed by the notifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Notification:
    """
    A notification item scraped from the Pareeksha Bhavan website.

    Attributes
    ----------
    title:
        Human-readable notification title as scraped from the page.
        HTML tags are stripped before storage.
    website_url:
        Canonical URL of the notification landing page.
    publication_date:
        Date the notification was published, as a UTC-aware datetime.
        ``None`` if the scraper could not parse a date.
    pdf_url:
        Direct URL to the linked PDF file, if any.  ``None`` if the
        notification has no PDF attachment.
    summary:
        Short text summary (first paragraph / PDF extract snippet).
        Populated by the PDF extractor in Phase 4; empty string until then.
    matched_keywords:
        List of keyword strings (from ``KeywordRegistry``) found in the
        notification title or PDF text.  Empty until the matcher runs.
    pdf_text:
        Full text extracted from the PDF, if downloaded.  Not included in
        notifications sent to Telegram/email — used only internally by the
        matcher.
    checked_time:
        UTC timestamp of the monitoring run that found this notification.
        Defaults to ``datetime.now(UTC)`` at construction time.
    notification_id:
        Stable 16-char SHA-256 hash of (website_url, title).
        Set by the scraper via ``src.utils.make_notification_id``.
        Empty string if not yet assigned.
    """

    # ── Required fields ──────────────────────────────────────────────────────
    title: str
    website_url: str

    # ── Optional / enriched fields ────────────────────────────────────────────
    publication_date: Optional[datetime] = None
    pdf_url: Optional[str] = None
    summary: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    pdf_text: str = ""
    notification_id: str = ""

    # ── Auto-set fields ───────────────────────────────────────────────────────
    checked_time: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def has_pdf(self) -> bool:
        """True if a PDF URL is attached to this notification."""
        return bool(self.pdf_url)

    @property
    def has_keywords(self) -> bool:
        """True if at least one keyword was matched."""
        return bool(self.matched_keywords)

    @property
    def display_date(self) -> str:
        """
        Human-readable publication date string for use in notifications.

        Returns ``"Unknown date"`` if ``publication_date`` is ``None``.
        """
        if self.publication_date is None:
            return "Unknown date"
        return self.publication_date.strftime("%d %b %Y")

    @property
    def display_checked_time(self) -> str:
        """Human-readable checked_time string (UTC)."""
        return self.checked_time.strftime("%d %b %Y, %H:%M UTC")

    def __repr__(self) -> str:
        return (
            f"Notification(id={self.notification_id!r}, "
            f"title={self.title[:50]!r}, "
            f"keywords={self.matched_keywords!r})"
        )
