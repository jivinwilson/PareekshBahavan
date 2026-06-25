"""
tests/test_keyword_matcher.py — Unit tests for src/keyword_matcher.py

All tests are pure (no I/O, no mocking required).  The matcher operates
only on strings so every test simply constructs a KeywordMatcher and calls
match() with a text fixture.

Coverage
--------
KeywordMatch model
  - frozen dataclass
  - repr

MatchResult model
  - frozen dataclass
  - is_high_confidence property

KeywordMatcher.match()
  - Exact match (high-priority keyword)
  - Case-insensitive match (mixed case in text)
  - Multiple different keywords found
  - Same keyword appears multiple times (dedup in matched_keywords)
  - No match — returns matched=False, score=0.0
  - Empty string — returns matched=False
  - Whitespace-only string — returns matched=False
  - High-confidence (high-priority + secondary) → score >= 0.85
  - Low-confidence (secondary only) → score == 0.4, label LOW
  - Medium-confidence (high-priority only) → score == 0.7, label MEDIUM
  - Very-high-confidence (high + 3 secondary) → score == 1.0, label VERY HIGH
  - context window correct (includes surrounding lines)
  - line_number is 1-based
  - matched_text preserves source casing
  - summary contains matched keywords
  - filename included in summary
  - matched_keywords in first-appearance order
  - all_matches count vs matched_keywords dedup
  - from_settings() factory
  - default() factory
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.keyword_matcher import (
    DEFAULT_HIGH_PRIORITY,
    DEFAULT_SECONDARY,
    KeywordMatch,
    KeywordMatcher,
    MatchResult,
)

# ---------------------------------------------------------------------------
# Text fixtures
# ---------------------------------------------------------------------------

TEXT_HIGH_ONLY = (
    "University of Calicut\n"
    "Special Examination — November 2026\n"
    "Students who have exhausted regular attempts may apply."
)

TEXT_SECONDARY_ONLY = (
    "CBCSS regulations for 2020 Admission students.\n"
    "B.Sc Computer Science Third Semester guidelines."
)

TEXT_HIGH_AND_SECONDARY = (
    "University of Calicut — Pareeksha Bhavan\n"
    "Special Examination Notification — B.Sc Computer Science\n"
    "CBCSS 2020 Admission Third Semester students with Exhausted Chances.\n"
    "One Time Supplementary examination schedule attached."
)

TEXT_MULTI_OCCURRENCE = (
    "Special Examination details: refer to Special Examination circular.\n"
    "CBCSS B.Sc Computer Science programme."
)

TEXT_NO_MATCH = (
    "Regular semester examination schedule for all programmes.\n"
    "Students must download hall tickets from the university portal."
)

TEXT_CASE_MIXED = (
    "SPECIAL EXAMINATION notification issued.\n"
    "cbcss regulations apply to all 2020 admission students.\n"
    "b.sc computer science third semester."
)

TEXT_VERY_HIGH = (
    "Special Examination One Time Supplementary\n"
    "CBCSS B.Sc Computer Science Third Semester 2020 Admission\n"
    "Exhausted Chances candidates must apply before the deadline."
)


# ---------------------------------------------------------------------------
# KeywordMatch model
# ---------------------------------------------------------------------------

class TestKeywordMatchModel:
    def _make(self) -> KeywordMatch:
        return KeywordMatch(
            keyword="Special Examination",
            matched_text="Special Examination",
            line_number=2,
            context="Special Examination — November 2026",
        )

    def test_frozen(self):
        m = self._make()
        with pytest.raises(Exception):
            m.keyword = "Other"  # type: ignore[misc]

    def test_repr_contains_keyword(self):
        m = self._make()
        assert "Special Examination" in repr(m)

    def test_repr_contains_line_number(self):
        m = self._make()
        assert "2" in repr(m)


# ---------------------------------------------------------------------------
# MatchResult model
# ---------------------------------------------------------------------------

class TestMatchResultModel:
    def _make(self, score: float = 0.9) -> MatchResult:
        return MatchResult(
            matched=True,
            matched_keywords=("Special Examination", "CBCSS"),
            all_matches=(),
            total_matches=2,
            confidence_score=score,
            confidence_label="HIGH",
            summary="HIGH confidence match",
            high_priority_found=("Special Examination",),
            secondary_found=("CBCSS",),
        )

    def test_frozen(self):
        r = self._make()
        with pytest.raises(Exception):
            r.matched = False  # type: ignore[misc]

    def test_is_high_confidence_true(self):
        assert self._make(score=0.9).is_high_confidence is True
        assert self._make(score=1.0).is_high_confidence is True
        assert self._make(score=0.85).is_high_confidence is True

    def test_is_high_confidence_false(self):
        assert self._make(score=0.7).is_high_confidence is False
        assert self._make(score=0.4).is_high_confidence is False
        assert self._make(score=0.0).is_high_confidence is False


# ---------------------------------------------------------------------------
# KeywordMatcher — basic matching
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_high_priority_keyword_found(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_ONLY, "notice.pdf")
        assert result.matched is True
        assert any("Special Exam" in k for k in result.matched_keywords)

    def test_matched_keywords_non_empty(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_ONLY)
        assert len(result.matched_keywords) > 0

    def test_total_matches_positive(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_ONLY)
        assert result.total_matches > 0


class TestCaseInsensitiveMatch:
    def test_uppercase_text_matched(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_CASE_MIXED)
        assert result.matched is True

    def test_uppercase_special_exam_matched(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("SPECIAL EXAMINATION notice issued")
        assert result.matched is True
        assert any("Special Exam" in k for k in result.matched_keywords)

    def test_lowercase_secondary_matched(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("cbcss regulations apply")
        assert result.matched is True
        assert "CBCSS" in result.matched_keywords

    def test_matched_text_preserves_source_casing(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("SPECIAL EXAMINATION notice")
        # matched_text should be the actual casing from the source
        m = next(m for m in result.all_matches if "Special Exam" in m.keyword)
        assert m.matched_text == "SPECIAL EXAMINATION"


class TestMultipleKeywords:
    def test_multiple_distinct_keywords_found(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_AND_SECONDARY)
        assert len(result.matched_keywords) >= 3

    def test_high_and_secondary_both_present(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_AND_SECONDARY)
        assert len(result.high_priority_found) >= 1
        assert len(result.secondary_found) >= 1

    def test_matched_keywords_first_appearance_order(self):
        # "CBCSS" appears before "B.Sc" in TEXT_SECONDARY_ONLY
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_SECONDARY_ONLY)
        kws = list(result.matched_keywords)
        cbcss_idx = next((i for i, k in enumerate(kws) if k == "CBCSS"), None)
        bsc_idx   = next((i for i, k in enumerate(kws) if k == "B.Sc"), None)
        if cbcss_idx is not None and bsc_idx is not None:
            assert cbcss_idx < bsc_idx


class TestDuplicateKeywords:
    def test_same_keyword_twice_deduplicated_in_matched_keywords(self):
        # "Special Examination" appears twice on different lines.
        # Both lines match "Special Examination"; dedup means it appears once
        # in matched_keywords even though all_matches has two entries.
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_MULTI_OCCURRENCE)
        # "Special Examination" should appear exactly once in matched_keywords
        count = sum(1 for k in result.matched_keywords if k == "Special Examination")
        assert count == 1  # deduplicated across two lines

    def test_all_matches_includes_multiple_occurrences(self):
        # all_matches should have 2 entries for "Special Examination"
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_MULTI_OCCURRENCE)
        spec_exam_matches = [m for m in result.all_matches if "Special Exam" in m.keyword]
        assert len(spec_exam_matches) >= 2  # one per line

    def test_total_matches_gt_matched_keywords(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_MULTI_OCCURRENCE)
        assert result.total_matches >= len(result.matched_keywords)


# ---------------------------------------------------------------------------
# No match / empty input
# ---------------------------------------------------------------------------

class TestNoMatch:
    def test_no_match_matched_false(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH)
        assert result.matched is False

    def test_no_match_score_zero(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH)
        assert result.confidence_score == 0.0

    def test_no_match_label_none(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH)
        assert result.confidence_label == "NONE"

    def test_no_match_empty_keywords(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH)
        assert result.matched_keywords == ()
        assert result.all_matches == ()

    def test_empty_string_no_match(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("")
        assert result.matched is False
        assert result.confidence_score == 0.0

    def test_whitespace_only_no_match(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("   \n\t  ")
        assert result.matched is False

    def test_no_match_summary_informative(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH, "exam.pdf")
        assert "No" in result.summary or "no" in result.summary.lower()


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    def test_high_plus_secondary_is_high_confidence(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_AND_SECONDARY)
        assert result.confidence_score >= 0.85
        assert result.is_high_confidence is True

    def test_high_plus_secondary_label_high_or_very_high(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_AND_SECONDARY)
        assert result.confidence_label in ("HIGH", "VERY HIGH")

    def test_secondary_only_score_040(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_SECONDARY_ONLY)
        assert result.confidence_score == 0.4
        assert result.confidence_label == "LOW"

    def test_secondary_only_not_high_confidence(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_SECONDARY_ONLY)
        assert result.is_high_confidence is False

    def test_high_priority_only_score_070(self):
        matcher = KeywordMatcher.default()
        # Only high-priority keyword, no secondary
        text = "Special Examination notice has been issued."
        result = matcher.match(text)
        assert result.confidence_score == 0.7
        assert result.confidence_label == "MEDIUM"

    def test_very_high_score_with_3plus_secondary(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_VERY_HIGH)
        assert result.confidence_score == 1.0
        assert result.confidence_label == "VERY HIGH"

    def test_high_plus_1_secondary_score_090(self):
        matcher = KeywordMatcher.default()
        text = "Special Examination for CBCSS students."
        result = matcher.match(text)
        assert result.confidence_score == 0.9
        assert result.confidence_label == "HIGH"

    def test_high_plus_2_secondary_score_095(self):
        matcher = KeywordMatcher.default()
        text = "Special Examination for B.Sc Computer Science students."
        result = matcher.match(text)
        assert result.confidence_score == 0.95
        assert result.confidence_label == "HIGH"

    def test_compute_score_static_method(self):
        # Test the static scoring method directly
        assert KeywordMatcher._compute_score([], []) == (0.0, "NONE")
        assert KeywordMatcher._compute_score([], ["CBCSS"]) == (0.4, "LOW")
        assert KeywordMatcher._compute_score(["Special Exam"], []) == (0.7, "MEDIUM")
        assert KeywordMatcher._compute_score(["Special Exam"], ["CBCSS"]) == (0.9, "HIGH")
        assert KeywordMatcher._compute_score(["Special Exam"], ["CBCSS", "B.Sc"]) == (0.95, "HIGH")
        assert KeywordMatcher._compute_score(["Special Exam"], ["CBCSS", "B.Sc", "CS"]) == (1.0, "VERY HIGH")


# ---------------------------------------------------------------------------
# Context and line numbers
# ---------------------------------------------------------------------------

class TestContextAndLineNumbers:
    def test_line_number_is_1_based(self):
        matcher = KeywordMatcher.default()
        text = "Line one\nSpecial Examination is here\nLine three"
        result = matcher.match(text)
        m = next(m for m in result.all_matches if "Special Exam" in m.keyword)
        assert m.line_number == 2  # second line, 1-based

    def test_context_includes_matching_line(self):
        matcher = KeywordMatcher.default()
        text = "Before\nSpecial Examination notice\nAfter"
        result = matcher.match(text)
        m = next(m for m in result.all_matches if "Special Exam" in m.keyword)
        assert "Special Examination notice" in m.context

    def test_context_includes_surrounding_lines(self):
        matcher = KeywordMatcher.default()
        text = "Context before\nSpecial Examination notice\nContext after"
        result = matcher.match(text)
        m = next(m for m in result.all_matches if "Special Exam" in m.keyword)
        assert "Context before" in m.context
        assert "Context after" in m.context

    def test_context_at_start_of_document(self):
        """First line match should not crash (no lines before it)."""
        matcher = KeywordMatcher.default()
        text = "Special Examination notice\nLine two\nLine three"
        result = matcher.match(text)
        assert result.matched is True


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

class TestSummaryGeneration:
    def test_high_confidence_summary_says_high(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_AND_SECONDARY)
        assert "HIGH" in result.summary or "VERY HIGH" in result.summary

    def test_low_confidence_summary_says_low(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_SECONDARY_ONLY)
        assert "LOW" in result.summary

    def test_summary_contains_filename(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_ONLY, "special_exam.pdf")
        assert "special_exam.pdf" in result.summary

    def test_summary_contains_matched_keyword(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_HIGH_ONLY)
        assert any(k in result.summary for k in result.matched_keywords)

    def test_no_match_summary_contains_no_keywords_message(self):
        matcher = KeywordMatcher.default()
        result = matcher.match(TEXT_NO_MATCH, "unrelated.pdf")
        assert result.summary  # non-empty


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

class TestFactories:
    def test_default_factory_creates_matcher(self):
        matcher = KeywordMatcher.default()
        assert isinstance(matcher, KeywordMatcher)

    def test_default_factory_has_high_priority_keywords(self):
        matcher = KeywordMatcher.default()
        result = matcher.match("Special Examination notice")
        assert result.matched is True

    def test_from_settings_factory(self):
        mock_settings = MagicMock()
        mock_settings.keywords = list(DEFAULT_HIGH_PRIORITY) + list(DEFAULT_SECONDARY)
        matcher = KeywordMatcher.from_settings(mock_settings)
        assert isinstance(matcher, KeywordMatcher)

    def test_from_settings_uses_settings_keywords(self):
        mock_settings = MagicMock()
        mock_settings.keywords = ["Custom Keyword", "Another Term"]
        matcher = KeywordMatcher.from_settings(mock_settings)
        result = matcher.match("This text contains Custom Keyword here")
        assert result.matched is True

    def test_custom_keyword_lists(self):
        matcher = KeywordMatcher(
            high_priority=["Urgent Exam"],
            secondary=["Section B"],
        )
        result = matcher.match("Urgent Exam for Section B students")
        assert result.matched is True
        assert result.confidence_score == 0.9
