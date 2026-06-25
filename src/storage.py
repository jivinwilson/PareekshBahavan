"""
src/storage.py — Persistent duplicate-suppression store.

Responsibility
--------------
Read and write ``data/last_seen.json``.  The file tracks which notification
IDs have already been processed so the monitor never sends the same alert
twice.

Design decisions
----------------
Thread safety
    A ``threading.Lock`` guards every read-modify-write cycle.  GitHub Actions
    runs a single process, but unit tests may call the store concurrently.

Atomic writes
    Updates are written to a sibling temp file first, then ``os.replace()``
    swaps it in atomically.  This guarantees the file is never left in a
    half-written state if the process is killed mid-write.

Schema
    The JSON file has two top-level keys:
        ``notifications`` — list of SeenNotification dicts
        ``last_checked``  — ISO-8601 UTC timestamp of the most recent run

Immutability at the model layer
    ``SeenNotification`` is a frozen Pydantic model.  This prevents accidental
    mutation of objects stored in the in-memory set.

Usage
-----
    from src.storage import NotificationStore

    store = NotificationStore(path=settings.last_seen_path)
    if not store.is_seen("abc123"):
        # … send notification …
        store.mark_seen("abc123", title="Special Exam Notice", url="https://…")
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SeenNotification(BaseModel, frozen=True):
    """
    Immutable record of a notification that has already been processed.

    Attributes
    ----------
    notification_id:
        Stable hash/identifier for the notification (see ``src/utils.py``).
    title:
        Human-readable title as scraped from the website.
    url:
        Canonical URL of the notification or its PDF.
    seen_at:
        UTC timestamp when the notification was first processed.
    """

    notification_id: str
    title: str
    url: str
    seen_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def model_dump_json_safe(self) -> dict[str, str]:
        """Return a JSON-serialisable dict (datetime → ISO string)."""
        return {
            "notification_id": self.notification_id,
            "title": self.title,
            "url": self.url,
            "seen_at": self.seen_at.isoformat(),
        }


class StoreData(BaseModel):
    """
    Root schema of ``last_seen.json``.

    Attributes
    ----------
    notifications:
        All notifications seen so far, in insertion order.
    last_checked:
        UTC timestamp of the most recent monitoring run (``None`` if never run).
    """

    notifications: list[SeenNotification] = Field(default_factory=list)
    last_checked: datetime | None = None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class NotificationStore:
    """
    Thread-safe, atomic-write store for seen notifications.

    Parameters
    ----------
    path:
        Filesystem path to ``last_seen.json``.  The parent directory is
        created on first write if it does not exist.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # Eagerly load into memory; keeps subsequent reads O(1) via the set.
        self._data: StoreData = self._load()
        self._seen_ids: set[str] = {
            n.notification_id for n in self._data.notifications
        }

    # ── Public query API ────────────────────────────────────────────────────

    def is_seen(self, notification_id: str) -> bool:
        """
        Return ``True`` if *notification_id* has already been processed.

        O(1) — backed by an in-memory set.
        """
        with self._lock:
            return notification_id in self._seen_ids

    def all_seen_ids(self) -> frozenset[str]:
        """Return a snapshot of all seen IDs as an immutable frozenset."""
        with self._lock:
            return frozenset(self._seen_ids)

    def count(self) -> int:
        """Return the number of notifications stored."""
        with self._lock:
            return len(self._data.notifications)

    def __iter__(self) -> Iterator[SeenNotification]:
        """Iterate over stored notifications in insertion order."""
        with self._lock:
            return iter(list(self._data.notifications))

    @property
    def last_checked(self) -> datetime | None:
        """UTC timestamp of the most recent monitoring run."""
        with self._lock:
            return self._data.last_checked

    # ── Public mutation API ─────────────────────────────────────────────────

    def mark_seen(
        self,
        notification_id: str,
        title: str,
        url: str,
        seen_at: datetime | None = None,
    ) -> SeenNotification:
        """
        Record *notification_id* as seen and persist to disk atomically.

        If *notification_id* is already present, this is a no-op (returns the
        existing record without writing to disk again).

        Parameters
        ----------
        notification_id:
            Stable hash for this notification.
        title:
            Human-readable title.
        url:
            Canonical URL of the notification or its PDF.
        seen_at:
            Override timestamp (defaults to ``datetime.now(UTC)``).

        Returns
        -------
        SeenNotification
            The newly created (or pre-existing) record.
        """
        with self._lock:
            if notification_id in self._seen_ids:
                # Return existing record
                for n in self._data.notifications:
                    if n.notification_id == notification_id:
                        return n
            record = SeenNotification(
                notification_id=notification_id,
                title=title,
                url=url,
                seen_at=seen_at or datetime.now(tz=timezone.utc),
            )
            self._data.notifications.append(record)
            self._seen_ids.add(notification_id)
            self._flush()
            return record

    def update_last_checked(self, ts: datetime | None = None) -> None:
        """
        Persist the timestamp of the most recent monitoring run.

        Parameters
        ----------
        ts:
            Timestamp to store (defaults to ``datetime.now(UTC)``).
        """
        with self._lock:
            self._data.last_checked = ts or datetime.now(tz=timezone.utc)
            self._flush()

    def clear(self) -> None:
        """
        Remove all stored notifications and reset the store.

        Primarily used in tests.  Writes an empty store to disk.
        """
        with self._lock:
            self._data = StoreData()
            self._seen_ids = set()
            self._flush()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _load(self) -> StoreData:
        """
        Read ``last_seen.json`` from disk.

        Returns an empty ``StoreData`` if the file does not exist or contains
        invalid JSON — the monitor must never crash on a corrupt store.
        """
        if not self._path.exists():
            return StoreData()
        try:
            raw = self._path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            return StoreData.model_validate(payload)
        except (json.JSONDecodeError, ValueError, OSError):
            # Corrupt or unreadable file — start fresh rather than crashing.
            return StoreData()

    def _flush(self) -> None:
        """
        Write the current in-memory state to disk **atomically**.

        Uses a sibling temp file + ``os.replace()`` so the store file is never
        left half-written.

        Must be called **inside** ``self._lock``.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "notifications": [
                n.model_dump_json_safe() for n in self._data.notifications
            ],
            "last_checked": (
                self._data.last_checked.isoformat()
                if self._data.last_checked
                else None
            ),
        }

        # Write to a temp file in the same directory so os.replace() is atomic
        # (same filesystem, so rename is guaranteed atomic on POSIX).
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".last_seen_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, self._path)
        except OSError:
            # Clean up temp file if replace fails
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
