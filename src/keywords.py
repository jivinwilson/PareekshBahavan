"""
src/keywords.py — Configurable keyword registry.

Responsibility
--------------
Centralise all keyword-matching logic so:

1. Keywords are defined in one place (environment / .env), not scattered
   across the scraper or notifier.
2. New keywords can be added via the ``KEYWORDS`` environment variable without
   touching any Python code.
3. The matching strategy (case-insensitive substring by default) can be
   changed in one place without touching callers.

Design
------
``KeywordRegistry`` is a lightweight value object wrapping an ordered list of
``Keyword`` dataclass instances.  It exposes:

- ``matches(text)``     — return every keyword found in *text*
- ``has_match(text)``   — True/False shorthand
- ``add(word)``         — add a keyword at runtime (useful in tests / CLI)
- ``__len__`` / ``__iter__`` — treat the registry as a collection

The matching is intentionally simple: case-insensitive substring search.  If
more sophisticated matching is needed in the future (regex, fuzzy, NLP), only
this module needs to change.

Usage
-----
    from src.keywords import KeywordRegistry
    from src.config import get_settings

    registry = KeywordRegistry.from_settings(get_settings())

    matches = registry.matches("CBCSS B.Sc Computer Science Special Exam")
    # → ["CBCSS", "B.Sc", "Computer Science", "Special Exam"]

    if registry.has_match(pdf_text):
        notify(...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from src.config import Settings


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Keyword:
    """
    A single keyword entry.

    Attributes
    ----------
    word:
        The raw keyword string as configured (preserves original casing for
        display in notifications).
    _lower:
        Lower-cased version used for case-insensitive comparison.
        Computed once at construction time.
    """

    word: str
    _lower: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # frozen=True means we must use object.__setattr__ to set computed fields
        object.__setattr__(self, "_lower", self.word.lower())

    def matches(self, text: str) -> bool:
        """
        Return ``True`` if this keyword appears in *text* (case-insensitive).

        Parameters
        ----------
        text:
            The text to search (page content, PDF extract, etc.).
        """
        return self._lower in text.lower()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class KeywordRegistry:
    """
    An ordered collection of ``Keyword`` instances.

    Matching is case-insensitive substring search.  Order is preserved so
    notification messages list keywords in the same order they are configured.

    Parameters
    ----------
    keywords:
        Initial list of keyword strings.
    """

    def __init__(self, keywords: list[str] | None = None) -> None:
        self._keywords: list[Keyword] = []
        seen: set[str] = set()
        for word in (keywords or []):
            word = word.strip()
            if word and word.lower() not in seen:
                self._keywords.append(Keyword(word=word))
                seen.add(word.lower())

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: "Settings") -> "KeywordRegistry":
        """
        Build a ``KeywordRegistry`` from the application ``Settings``.

        Parameters
        ----------
        settings:
            Loaded settings (``src.config.Settings``).
        """
        return cls(keywords=settings.keywords)

    @classmethod
    def from_string(cls, raw: str, separator: str = ",") -> "KeywordRegistry":
        """
        Build a ``KeywordRegistry`` from a delimited string.

        Parameters
        ----------
        raw:
            Keyword string, e.g. ``"Special Exam,CBCSS,B.Sc"``.
        separator:
            Delimiter between keywords (default: comma).
        """
        parts = [p.strip() for p in raw.split(separator) if p.strip()]
        return cls(keywords=parts)

    # ── Matching API ─────────────────────────────────────────────────────────

    def matches(self, text: str) -> list[str]:
        """
        Return the list of keywords found in *text*, preserving config order.

        Parameters
        ----------
        text:
            Arbitrary text to search (notification title, PDF body, etc.).

        Returns
        -------
        list[str]
            Original keyword strings (with their original casing) that appear
            in *text*.  Empty list if nothing matched.
        """
        if not text:
            return []
        return [kw.word for kw in self._keywords if kw.matches(text)]

    def has_match(self, text: str) -> bool:
        """
        Return ``True`` if at least one keyword is found in *text*.

        Slightly more efficient than ``bool(matches(text))`` because it
        short-circuits on the first match.
        """
        if not text:
            return False
        return any(kw.matches(text) for kw in self._keywords)

    # ── Mutation ─────────────────────────────────────────────────────────────

    def add(self, word: str) -> None:
        """
        Add a keyword to the registry at runtime.

        Duplicate keywords (case-insensitive) are silently ignored.

        Parameters
        ----------
        word:
            New keyword to add.
        """
        word = word.strip()
        if not word:
            return
        existing_lowers = {kw._lower for kw in self._keywords}
        if word.lower() not in existing_lowers:
            self._keywords.append(Keyword(word=word))

    def remove(self, word: str) -> bool:
        """
        Remove a keyword from the registry (case-insensitive).

        Parameters
        ----------
        word:
            Keyword to remove.

        Returns
        -------
        bool
            ``True`` if the keyword was found and removed, ``False`` otherwise.
        """
        lower = word.lower().strip()
        before = len(self._keywords)
        self._keywords = [kw for kw in self._keywords if kw._lower != lower]
        return len(self._keywords) < before

    # ── Collection protocol ──────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._keywords)

    def __iter__(self) -> Iterator[Keyword]:
        return iter(self._keywords)

    def __contains__(self, word: object) -> bool:
        """
        Support ``"Special Exam" in registry`` (case-insensitive).
        """
        if not isinstance(word, str):
            return False
        lower = word.lower().strip()
        return any(kw._lower == lower for kw in self._keywords)

    def __repr__(self) -> str:
        words = [kw.word for kw in self._keywords]
        return f"KeywordRegistry({words!r})"

    # ── Utility ──────────────────────────────────────────────────────────────

    def as_list(self) -> list[str]:
        """Return keyword strings as a plain list (original casing)."""
        return [kw.word for kw in self._keywords]
