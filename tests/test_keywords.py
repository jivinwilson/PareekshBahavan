"""
tests/test_keywords.py — Unit tests for src/keywords.py
"""

from __future__ import annotations

import pytest

from src.keywords import Keyword, KeywordRegistry


# ---------------------------------------------------------------------------
# Keyword dataclass
# ---------------------------------------------------------------------------

class TestKeyword:
    def test_matches_exact(self):
        kw = Keyword(word="Special Exam")
        assert kw.matches("Special Exam notification published") is True

    def test_matches_case_insensitive(self):
        kw = Keyword(word="Special Exam")
        assert kw.matches("SPECIAL EXAM 2026") is True
        assert kw.matches("special exam results") is True

    def test_no_match(self):
        kw = Keyword(word="Special Exam")
        assert kw.matches("Regular Exam results published") is False

    def test_empty_text_no_match(self):
        kw = Keyword(word="CBCSS")
        assert kw.matches("") is False

    def test_frozen_dataclass(self):
        kw = Keyword(word="Test")
        with pytest.raises(Exception):
            kw.word = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# KeywordRegistry — construction
# ---------------------------------------------------------------------------

class TestKeywordRegistryConstruction:
    def test_empty_registry(self):
        reg = KeywordRegistry()
        assert len(reg) == 0

    def test_none_keywords(self):
        reg = KeywordRegistry(keywords=None)
        assert len(reg) == 0

    def test_deduplication_case_insensitive(self):
        reg = KeywordRegistry(["CBCSS", "cbcss", "Cbcss"])
        assert len(reg) == 1

    def test_empty_strings_ignored(self):
        reg = KeywordRegistry(["Alpha", "", "  ", "Beta"])
        assert len(reg) == 2

    def test_from_string(self):
        reg = KeywordRegistry.from_string("Alpha,Beta,Gamma")
        assert len(reg) == 3
        assert "Alpha" in reg

    def test_from_string_custom_separator(self):
        reg = KeywordRegistry.from_string("Alpha|Beta|Gamma", separator="|")
        assert len(reg) == 3


# ---------------------------------------------------------------------------
# KeywordRegistry — matching
# ---------------------------------------------------------------------------

class TestKeywordRegistryMatching:
    def setup_method(self):
        self.reg = KeywordRegistry([
            "Special Examination",
            "CBCSS",
            "B.Sc",
            "Computer Science",
            "Third Semester",
        ])

    def test_matches_returns_matching_keywords(self):
        text = "CBCSS B.Sc Computer Science Special Examination notice"
        found = self.reg.matches(text)
        assert "CBCSS" in found
        assert "B.Sc" in found
        assert "Computer Science" in found
        assert "Special Examination" in found

    def test_matches_preserves_original_casing(self):
        found = self.reg.matches("cbcss special examination")
        assert "CBCSS" in found          # original casing preserved
        assert "Special Examination" in found

    def test_matches_empty_text(self):
        assert self.reg.matches("") == []

    def test_matches_no_keywords_found(self):
        assert self.reg.matches("Regular exam results") == []

    def test_has_match_true(self):
        assert self.reg.has_match("CBCSS notification") is True

    def test_has_match_false(self):
        assert self.reg.has_match("Unrelated content") is False

    def test_has_match_empty(self):
        assert self.reg.has_match("") is False

    def test_matches_order_preserved(self):
        # Keywords should appear in config order, not text order
        text = "Third Semester CBCSS Special Examination"
        found = self.reg.matches(text)
        idx_special = found.index("Special Examination")
        idx_cbcss = found.index("CBCSS")
        idx_third = found.index("Third Semester")
        # Config order: Special Examination(0), CBCSS(1), Third Semester(4)
        assert idx_special < idx_cbcss < idx_third


# ---------------------------------------------------------------------------
# KeywordRegistry — mutation
# ---------------------------------------------------------------------------

class TestKeywordRegistryMutation:
    def test_add_new_keyword(self):
        reg = KeywordRegistry(["Alpha"])
        reg.add("Beta")
        assert "Beta" in reg
        assert len(reg) == 2

    def test_add_duplicate_ignored(self):
        reg = KeywordRegistry(["Alpha"])
        reg.add("alpha")      # case-insensitive duplicate
        assert len(reg) == 1

    def test_add_empty_ignored(self):
        reg = KeywordRegistry(["Alpha"])
        reg.add("")
        assert len(reg) == 1

    def test_remove_existing(self):
        reg = KeywordRegistry(["Alpha", "Beta"])
        removed = reg.remove("Alpha")
        assert removed is True
        assert "Alpha" not in reg
        assert len(reg) == 1

    def test_remove_nonexistent(self):
        reg = KeywordRegistry(["Alpha"])
        removed = reg.remove("Gamma")
        assert removed is False

    def test_remove_case_insensitive(self):
        reg = KeywordRegistry(["Special Exam"])
        reg.remove("SPECIAL EXAM")
        assert len(reg) == 0


# ---------------------------------------------------------------------------
# KeywordRegistry — collection protocol
# ---------------------------------------------------------------------------

class TestKeywordRegistryCollection:
    def test_len(self):
        reg = KeywordRegistry(["A", "B", "C"])
        assert len(reg) == 3

    def test_iter(self):
        reg = KeywordRegistry(["A", "B"])
        words = [kw.word for kw in reg]
        assert words == ["A", "B"]

    def test_contains_true(self):
        reg = KeywordRegistry(["CBCSS"])
        assert "CBCSS" in reg

    def test_contains_case_insensitive(self):
        reg = KeywordRegistry(["CBCSS"])
        assert "cbcss" in reg

    def test_contains_false(self):
        reg = KeywordRegistry(["CBCSS"])
        assert "Regular" not in reg

    def test_as_list(self):
        reg = KeywordRegistry(["Alpha", "Beta"])
        assert reg.as_list() == ["Alpha", "Beta"]

    def test_repr(self):
        reg = KeywordRegistry(["A", "B"])
        assert "KeywordRegistry" in repr(reg)
