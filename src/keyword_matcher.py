"""
src/keyword_matcher.py — Keyword matching engine.

Responsibility
--------------
Search extracted PDF text (or any text string) for configured keywords and
return a detailed, structured result that the notifier can act on.

This module has no I/O — it only processes strings.  It never touches the
filesystem, the network, or the Telegram API.

Architecture
------------
KeywordMatch
    One occurrence of one keyword found in the text, with surrounding
    context so the notifier can include a useful snippet in the alert.

MatchResult
    The full result for one text+keyword-set run.  Immutable so callers
    can safely store or pass it around.

KeywordMatcher
    The engine.  Instantiated once (e.g. at startup from settings), then
    called with different text strings.

Matching algorithm
------------------
1. Split the text into numbered lines.
2. For each keyword in the registry, do a case-insensitive substring
   search on every line.
3. On a hit, extract a context window (the matching line plus up to
   CONTEXT_LINES lines before and after).
4. Collect one KeywordMatch per (keyword, line_number) pair — the same
   keyword on the same line is counted only once; the same keyword on
   different lines produces multiple KeywordMatch objects but only ONE
   entry in ``matched_keywords`` (deduplication by keyword string).

Confidence scoring
------------------
Keywords are divided into two tiers:

HIGH-PRIORITY  — strong signal that this is a Special Exam notification
    Special Examination, Special Exam, One Time Supplementary,
    One Time Regular Supplementary

SECONDARY      — supporting context that narrows relevance
    Exhausted Chances, CBCSS, 2020 Admission, B.Sc,
    Computer Science, Third Semester

Score table
    No match at all            →  0.0
    Secondary only (≥ 1)       →  0.4  (LOW)
    High-priority only (≥ 1)   →  0.7  (MEDIUM — unusual but possible)
    High + 1 secondary         →  0.9  (HIGH)
    High + 2 secondary         →  0.95 (HIGH)
    High + 3+ secondary        →  1.0  (VERY HIGH)

Summary generation
------------------
A human-readable sentence explaining the match:
    "HIGH confidence match: found 'Special Examination', 'CBCSS',
     'B.Sc' in <filename>"

Usage
-----
    from src.keyword_matcher import KeywordMatcher
    from src.config import get_settings

    matcher = KeywordMatcher.from_settings(get_settings())
    result  = matcher.match(pdf_content.full_text, "notice.pdf")
    if result.matched:
        print(result.summary)
        print(result.confidence_score)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from src.logger import get_logger

if TYPE_CHECKING:
    from src.config import Settings

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Default keyword configuration
# ---------------------------------------------------------------------------

DEFAULT_HIGH_PRIORITY: tuple[str, ...] = (
    "Special Examination",
    "Special Exam",
    "One Time Supplementary",
    "One Time Regular Supplementary",
)

DEFAULT_SECONDARY: tuple[str, ...] = (
    "Exhausted Chances",
    "CBCSS",
    "2020 Admission",
    "B.Sc",
    "Computer Science",
    "Third Semester",
)

# Lines of context to include before and after a matching line
CONTEXT_LINES: int = 2


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeywordMatch:
    """
    A single keyword hit within the text.

    Attributes
    ----------
    keyword:
        The keyword string as defined in the registry (original casing).
    matched_text:
        The exact substring of the source text that triggered the match.
        Usually the same as *keyword* but may differ in casing.
    line_number:
        1-based line number of the match, or ``None`` if line context is
        unavailable.
    context:
        The matching line plus up to ``CONTEXT_LINES`` surrounding lines,
        joined with newlines.  Useful for the alert summary.
    """

    keyword: str
    matched_text: str
    line_number: int | None
    context: str

    def __repr__(self) -> str:
        return (
            f"KeywordMatch(keyword={self.keyword!r}, "
            f"line={self.line_number}, "
            f"context={self.context[:60]!r})"
        )


@dataclass(frozen=True)
class MatchResult:
    """
    Complete matching result for one text document.

    Attributes
    ----------
    matched:
        ``True`` if at least one keyword was found.
    matched_keywords:
        Deduplicated tuple of keyword strings found (original casing,
        in the order they first appeared).
    all_matches:
        All ``KeywordMatch`` objects, including multiple hits for the
        same keyword on different lines.
    total_matches:
        Total number of keyword occurrences found (may exceed
        ``len(matched_keywords)`` if the same keyword appears multiple times).
    confidence_score:
        Float in ``[0.0, 1.0]``.  See module docstring for the scoring table.
    confidence_label:
        Human-readable tier: ``"NONE"``, ``"LOW"``, ``"MEDIUM"``,
        ``"HIGH"``, or ``"VERY HIGH"``.
    summary:
        Concise human-readable explanation of the match, suitable for
        inclusion in a Telegram notification.
    high_priority_found:
        Subset of ``matched_keywords`` that are high-priority.
    secondary_found:
        Subset of ``matched_keywords`` that are secondary.
    """

    matched: bool
    matched_keywords: tuple[str, ...]
    all_matches: tuple[KeywordMatch, ...]
    total_matches: int
    confidence_score: float
    confidence_label: str
    summary: str
    high_priority_found: tuple[str, ...]
    secondary_found: tuple[str, ...]

    @property
    def is_high_confidence(self) -> bool:
        """True when confidence_score >= 0.85."""
        return self.confidence_score >= 0.85

    def __repr__(self) -> str:
        return (
            f"MatchResult(matched={self.matched}, "
            f"score={self.confidence_score:.2f}, "
            f"label={self.confidence_label!r}, "
            f"keywords={list(self.matched_keywords)!r})"
        )


# ---------------------------------------------------------------------------
# KeywordMatcher
# ---------------------------------------------------------------------------

class KeywordMatcher:
    """
    Searches text for configured keywords and returns a ``MatchResult``.

    Parameters
    ----------
    high_priority:
        Keywords that are strong signals for a Special Exam notification.
    secondary:
        Supporting keywords that increase specificity.

    Both lists are stored lower-cased internally for case-insensitive
    matching; the original casing is preserved in ``KeywordMatch.keyword``
    and ``MatchResult.matched_keywords`` for display.
    """

    def __init__(
        self,
        high_priority: Sequence[str] = DEFAULT_HIGH_PRIORITY,
        secondary: Sequence[str] = DEFAULT_SECONDARY,
    ) -> None:
        # Store as (lower_key → original_key) for O(1) lookup + casing preservation
        self._high_priority: dict[str, str] = {k.lower(): k for k in high_priority}
        self._secondary: dict[str, str] = {k.lower(): k for k in secondary}
        # Combined ordered list for iteration (high-priority first)
        self._all_keywords: dict[str, str] = {**self._high_priority, **self._secondary}

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: "Settings") -> "KeywordMatcher":
        """
        Build a ``KeywordMatcher`` from application settings.

        The ``settings.keywords`` list is used as the full keyword set.
        Keywords matching any DEFAULT_HIGH_PRIORITY entry are treated as
        high-priority; the rest are secondary.
        """
        all_kws = settings.keywords
        high_lower = {k.lower() for k in DEFAULT_HIGH_PRIORITY}
        high = [k for k in all_kws if k.lower() in high_lower]
        secondary = [k for k in all_kws if k.lower() not in high_lower]
        return cls(high_priority=high or list(DEFAULT_HIGH_PRIORITY),
                   secondary=secondary or list(DEFAULT_SECONDARY))

    @classmethod
    def default(cls) -> "KeywordMatcher":
        """Return a ``KeywordMatcher`` with the built-in default keyword lists."""
        return cls()

    # ── Public API ────────────────────────────────────────────────────────────

    def match(self, text: str, filename: str = "") -> MatchResult:
        """
        Search *text* for all configured keywords.

        Parameters
        ----------
        text:
            Full text to search (e.g. ``PDFContent.full_text`` or a
            notification title).
        filename:
            Optional label used in the summary string (e.g. the PDF filename).

        Returns
        -------
        MatchResult
            Always returns a ``MatchResult``.  ``matched`` is ``False``
            and ``confidence_score`` is ``0.0`` when nothing was found.
        """
        if not text or not text.strip():
            log.debug("keyword_match_empty_text", filename=filename)
            return self._no_match(filename)

        lines = text.splitlines()
        all_matches: list[KeywordMatch] = []
        seen_kw_lines: set[tuple[str, int | None]] = set()  # dedup same kw+line

        for lower_kw, original_kw in self._all_keywords.items():
            for line_idx, line in enumerate(lines):
                if lower_kw in line.lower():
                    line_num = line_idx + 1  # 1-based
                    dedup_key = (lower_kw, line_num)
                    if dedup_key in seen_kw_lines:
                        continue
                    seen_kw_lines.add(dedup_key)

                    matched_text = self._extract_matched_text(line, lower_kw)
                    context = self._build_context(lines, line_idx)
                    all_matches.append(KeywordMatch(
                        keyword=original_kw,
                        matched_text=matched_text,
                        line_number=line_num,
                        context=context,
                    ))

        if not all_matches:
            log.debug("keyword_no_match", filename=filename)
            return self._no_match(filename)

        # Deduplicated keyword list in first-appearance order
        seen: set[str] = set()
        matched_keywords: list[str] = []
        for m in all_matches:
            if m.keyword not in seen:
                seen.add(m.keyword)
                matched_keywords.append(m.keyword)

        high_found = [k for k in matched_keywords if k.lower() in self._high_priority]
        secondary_found = [k for k in matched_keywords if k.lower() in self._secondary]

        score, label = self._compute_score(high_found, secondary_found)
        summary = self._build_summary(
            matched_keywords, high_found, secondary_found, label, filename
        )

        log.info(
            "keyword_match_found",
            filename=filename,
            matched_keywords=matched_keywords,
            confidence_score=score,
            confidence_label=label,
            total_matches=len(all_matches),
        )

        return MatchResult(
            matched=True,
            matched_keywords=tuple(matched_keywords),
            all_matches=tuple(all_matches),
            total_matches=len(all_matches),
            confidence_score=score,
            confidence_label=label,
            summary=summary,
            high_priority_found=tuple(high_found),
            secondary_found=tuple(secondary_found),
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _no_match(self, filename: str) -> MatchResult:
        """Return a MatchResult representing no match."""
        label = "NONE"
        summary = (
            f"No matching keywords found in {filename!r}."
            if filename else "No matching keywords found."
        )
        return MatchResult(
            matched=False,
            matched_keywords=(),
            all_matches=(),
            total_matches=0,
            confidence_score=0.0,
            confidence_label=label,
            summary=summary,
            high_priority_found=(),
            secondary_found=(),
        )

    @staticmethod
    def _compute_score(
        high_found: list[str],
        secondary_found: list[str],
    ) -> tuple[float, str]:
        """
        Return ``(score, label)`` based on which tiers matched.

        Score table (see module docstring for rationale):
            high=0, secondary=0  → 0.0, NONE   (unreachable via match())
            high=0, secondary≥1  → 0.4, LOW
            high≥1, secondary=0  → 0.7, MEDIUM
            high≥1, secondary=1  → 0.9, HIGH
            high≥1, secondary=2  → 0.95, HIGH
            high≥1, secondary≥3  → 1.0, VERY HIGH
        """
        n_high = len(high_found)
        n_sec  = len(secondary_found)

        if n_high == 0 and n_sec == 0:
            return 0.0, "NONE"
        if n_high == 0:
            return 0.4, "LOW"
        if n_sec == 0:
            return 0.7, "MEDIUM"
        if n_sec == 1:
            return 0.9, "HIGH"
        if n_sec == 2:
            return 0.95, "HIGH"
        return 1.0, "VERY HIGH"

    @staticmethod
    def _extract_matched_text(line: str, lower_kw: str) -> str:
        """
        Return the exact substring from *line* that matched *lower_kw*,
        preserving the original casing of the source text.
        """
        idx = line.lower().find(lower_kw)
        if idx == -1:
            return lower_kw
        return line[idx: idx + len(lower_kw)]

    @staticmethod
    def _build_context(lines: list[str], match_idx: int) -> str:
        """
        Return the matching line plus up to CONTEXT_LINES before and after,
        joined with newlines, stripped of leading/trailing blank lines.
        """
        start = max(0, match_idx - CONTEXT_LINES)
        end   = min(len(lines), match_idx + CONTEXT_LINES + 1)
        context_lines = [l.strip() for l in lines[start:end] if l.strip()]
        return "\n".join(context_lines)

    @staticmethod
    def _build_summary(
        matched_keywords: list[str],
        high_found: list[str],
        secondary_found: list[str],
        label: str,
        filename: str,
    ) -> str:
        """Generate a concise human-readable summary of the match."""
        kw_list = ", ".join(f"'{k}'" for k in matched_keywords[:6])
        suffix = f" in '{filename}'" if filename else ""

        if label == "VERY HIGH":
            return (
                f"VERY HIGH confidence match: found {kw_list}{suffix}. "
                f"This notification strongly matches the Special Examination criteria."
            )
        if label == "HIGH":
            return (
                f"HIGH confidence match: found {kw_list}{suffix}. "
                f"High-priority keyword(s) present with supporting context."
            )
        if label == "MEDIUM":
            hp = ", ".join(f"'{k}'" for k in high_found)
            return (
                f"MEDIUM confidence match: found high-priority keyword(s) {hp}{suffix}. "
                f"No secondary keywords found — verify manually."
            )
        # LOW
        sec = ", ".join(f"'{k}'" for k in secondary_found)
        return (
            f"LOW confidence match: found secondary keyword(s) {sec}{suffix}. "
            f"No high-priority Special Examination keywords detected."
        )
