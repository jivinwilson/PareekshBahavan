"""
tests/test_storage.py — Unit tests for src/storage.py
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest

from src.storage import NotificationStore, SeenNotification, StoreData


# ---------------------------------------------------------------------------
# Fixtures
# Use /tmp (native Linux filesystem) rather than pytest's tmp_path to avoid
# recursion errors when pytest tries to clean up the mounted Windows filesystem.
# ---------------------------------------------------------------------------

@pytest.fixture
def store_path() -> Generator[Path, None, None]:
    import tempfile, os
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d) / "last_seen.json"


@pytest.fixture
def store(store_path: Path) -> NotificationStore:
    return NotificationStore(path=store_path)


# ---------------------------------------------------------------------------
# SeenNotification model
# ---------------------------------------------------------------------------

class TestSeenNotification:
    def test_frozen(self):
        n = SeenNotification(
            notification_id="abc", title="Test", url="https://example.com"
        )
        with pytest.raises(Exception):
            n.notification_id = "xyz"  # type: ignore[misc]

    def test_model_dump_json_safe_keys(self):
        n = SeenNotification(
            notification_id="abc", title="Test", url="https://example.com"
        )
        d = n.model_dump_json_safe()
        assert set(d.keys()) == {"notification_id", "title", "url", "seen_at"}

    def test_seen_at_defaults_to_utc(self):
        n = SeenNotification(
            notification_id="abc", title="Test", url="https://example.com"
        )
        assert n.seen_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# NotificationStore — basic CRUD
# ---------------------------------------------------------------------------

class TestNotificationStoreBasic:
    def test_empty_store_is_seen_false(self, store: NotificationStore):
        assert store.is_seen("nonexistent") is False

    def test_mark_seen_makes_is_seen_true(self, store: NotificationStore):
        store.mark_seen("id1", title="Title", url="https://example.com")
        assert store.is_seen("id1") is True

    def test_mark_seen_returns_seen_notification(self, store: NotificationStore):
        record = store.mark_seen("id1", title="Title", url="https://example.com")
        assert isinstance(record, SeenNotification)
        assert record.notification_id == "id1"

    def test_mark_seen_idempotent(self, store: NotificationStore):
        store.mark_seen("id1", title="Title A", url="https://a.com")
        record2 = store.mark_seen("id1", title="Title B", url="https://b.com")
        # Second call returns the original record unchanged
        assert record2.title == "Title A"
        assert store.count() == 1

    def test_count_increments(self, store: NotificationStore):
        assert store.count() == 0
        store.mark_seen("id1", title="T1", url="https://1.com")
        store.mark_seen("id2", title="T2", url="https://2.com")
        assert store.count() == 2

    def test_all_seen_ids_returns_frozenset(self, store: NotificationStore):
        store.mark_seen("id1", title="T", url="https://x.com")
        ids = store.all_seen_ids()
        assert isinstance(ids, frozenset)
        assert "id1" in ids

    def test_clear_resets_store(self, store: NotificationStore):
        store.mark_seen("id1", title="T", url="https://x.com")
        store.clear()
        assert store.count() == 0
        assert store.is_seen("id1") is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestNotificationStorePersistence:
    def test_data_survives_reload(self, store_path: Path):
        s1 = NotificationStore(store_path)
        s1.mark_seen("id1", title="Title", url="https://example.com")

        s2 = NotificationStore(store_path)
        assert s2.is_seen("id1") is True
        assert s2.count() == 1

    def test_json_file_is_valid_json(self, store: NotificationStore, store_path: Path):
        store.mark_seen("id1", title="Title", url="https://example.com")
        raw = store_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert "notifications" in data
        assert "last_checked" in data

    def test_update_last_checked_persists(self, store_path: Path):
        s1 = NotificationStore(store_path)
        ts = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
        s1.update_last_checked(ts)

        s2 = NotificationStore(store_path)
        assert s2.last_checked is not None
        assert s2.last_checked.year == 2026

    def test_corrupt_file_does_not_crash(self, store_path: Path):
        store_path.write_text("NOT VALID JSON {{{{", encoding="utf-8")
        store = NotificationStore(store_path)
        assert store.count() == 0  # graceful fallback

    def test_missing_file_does_not_crash(self, store_path: Path):
        assert not store_path.exists()
        store = NotificationStore(store_path)
        assert store.count() == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestNotificationStoreThreadSafety:
    def test_concurrent_mark_seen(self, store: NotificationStore):
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                store.mark_seen(
                    f"id{idx}", title=f"Title {idx}", url=f"https://example.com/{idx}"
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert store.count() == 20
