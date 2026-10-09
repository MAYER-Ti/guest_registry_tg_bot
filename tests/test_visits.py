from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

import storage
from storage import StaleGuestError, Store, Visit, VisitStateError, VisitSummary


UTC = timezone.utc


class VisitStorageTests(unittest.TestCase):
    def setUp(self):
        temporary_base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = temporary_base / ("guest-visits-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "data" / "guests.sqlite3")
        self.guest = self.store.add_guest(name="Алёна", phone="89991234567", comment="Карточка",
                                          photo=b"original-photo", photo_file_id="telegram-photo", actor_id=1001)
        self.start = datetime(2026, 10, 9, 10, 0, 0, 100_000, tzinfo=UTC)

    def current_version(self):
        return self.store.get_guest(self.guest.id).version

    def begin(self, now=None, actor_id=1001):
        return self.store.start_visit(self.guest.id, self.current_version(), actor_id,
                                      now=now if now is not None else self.start)

    def test_visit_duration_floors_seconds_and_never_becomes_negative(self):
        visit = self.begin()
        self.assertEqual(visit.duration_seconds(self.start - timedelta(seconds=1)), 0)
        self.assertEqual(visit.duration_seconds(self.start + timedelta(seconds=1.9)), 1)
        end = self.start + timedelta(days=2, hours=3, seconds=0.9)
        stopped = self.store.stop_visit(self.guest.id, visit.id, 2002, now=end)
        self.assertEqual(stopped.duration_seconds(self.start), 51 * 3600)
        self.assertEqual(stopped.duration_seconds(), 51 * 3600)
        self.assertEqual(stopped.stopped_by, 2002)
        self.assertIsNotNone(stopped.stopped_at)

    def test_accumulated_and_current_totals_survive_restart(self):
        self.assertEqual(self.store.get_visit_summary(self.guest.id, now=self.start), VisitSummary(None, 0, 0, 0))
        first = self.begin()
        self.assertEqual(first.started_by, 1001)
        active = self.store.get_visit_summary(self.guest.id, now=self.start + timedelta(minutes=10))
        self.assertEqual(active.active, first)
        self.assertEqual(active.completed_count, 0)
        self.assertEqual(active.current_seconds, 600)
        stopped = self.store.stop_visit(self.guest.id, first.id, 2002,
                                       now=self.start + timedelta(hours=1))
        self.assertEqual(self.current_version(), 3)
        second_start = self.start + timedelta(days=1)
        second = self.begin(now=second_start, actor_id=3003)
        reopened = Store(self.store.path)
        summary = reopened.get_visit_summary(self.guest.id, now=second_start + timedelta(minutes=5))
        self.assertEqual(summary.active, second)
        self.assertEqual(summary.completed_count, 1)
        self.assertEqual(summary.completed_seconds, 3600)
        self.assertEqual(summary.current_seconds, 300)
        self.assertEqual(summary.total_seconds, 3900)
        self.assertEqual(reopened.list_visits(self.guest.id), [second, stopped])
        card = reopened.get_guest(self.guest.id)
        self.assertEqual(card.version, 4)
        self.assertEqual(card.updated_by, 3003)
        self.assertEqual(card.created_by, self.guest.created_by)
        self.assertEqual(card.photo, self.guest.photo)

    def test_old_start_and_stop_buttons_cannot_affect_next_visit(self):
        first = self.begin()
        with self.assertRaises(StaleGuestError):
            self.store.start_visit(self.guest.id, self.guest.version, 2002, now=self.start)
        with self.assertRaises(VisitStateError):
            self.store.start_visit(self.guest.id, self.current_version(), 2002, now=self.start)
        self.store.stop_visit(self.guest.id, first.id, 2002, now=self.start + timedelta(seconds=10))
        with self.assertRaises(StaleGuestError):
            self.store.start_visit(self.guest.id, self.guest.version, 3003, now=self.start)
        second = self.begin(now=self.start + timedelta(minutes=1))
        version = self.current_version()
        with self.assertRaises(VisitStateError):
            self.store.stop_visit(self.guest.id, first.id, 4004, now=self.start + timedelta(minutes=2))
        self.assertEqual(self.current_version(), version)
        self.assertEqual(self.store.get_visit_summary(self.guest.id, now=self.start).active, second)

    def test_two_staff_start_and_stop_only_once(self):
        start_barrier = Barrier(2)

        def start(actor_id):
            start_barrier.wait()
            try:
                return self.store.start_visit(self.guest.id, self.guest.version, actor_id, now=self.start)
            except (StaleGuestError, VisitStateError):
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            started = list(pool.map(start, [1001, 2002]))
        self.assertEqual(sum(visit is not None for visit in started), 1)
        visit = next(visit for visit in started if visit is not None)
        stop_barrier = Barrier(2)

        def stop(actor_id):
            stop_barrier.wait()
            try:
                return self.store.stop_visit(self.guest.id, visit.id, actor_id,
                                             now=self.start + timedelta(minutes=1))
            except VisitStateError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            stopped = list(pool.map(stop, [3003, 4004]))
        self.assertEqual(sum(visit is not None for visit in stopped), 1)
        self.assertEqual(self.current_version(), 3)
        summary = self.store.get_visit_summary(self.guest.id, now=self.start)
        self.assertEqual(summary.completed_count, 1)
        self.assertEqual(summary.total_seconds, 60)
        self.assertIsNone(summary.active)

    def test_clock_reversal_and_naive_time_leave_database_unchanged(self):
        with self.assertRaises(ValueError):
            self.begin(now=datetime(2026, 10, 9))
        self.assertEqual(self.current_version(), self.guest.version)
        self.assertEqual(self.store.list_visits(self.guest.id), [])
        visit = self.begin()
        version = self.current_version()
        with self.assertRaises(ValueError):
            self.store.stop_visit(self.guest.id, visit.id, 2002, now=self.start - timedelta(microseconds=1))
        with self.assertRaises(ValueError):
            self.store.stop_visit(self.guest.id, visit.id, 2002, now=datetime(2026, 10, 9))
        self.assertEqual(self.current_version(), version)
        self.assertEqual(self.store.get_visit_summary(self.guest.id, now=self.start).active, visit)

    def test_timezone_offsets_are_stored_as_canonical_utc(self):
        moscow = self.start.astimezone(timezone(timedelta(hours=3)))
        visit = self.begin(now=moscow)
        self.assertEqual(visit.started_at, self.start.isoformat(timespec="microseconds"))
        stopped = self.store.stop_visit(self.guest.id, visit.id, 2002, now=moscow + timedelta(hours=1))
        self.assertEqual(stopped.duration_seconds(), 3600)

    def test_history_is_newest_first_stable_and_paginated(self):
        history = []
        # Identical timestamps still have deterministic descending visit IDs.
        for _ in range(7):
            visit = self.begin()
            history.append(self.store.stop_visit(self.guest.id, visit.id, 1001, now=self.start))
        active = self.begin()
        expected = [active, *reversed(history)]
        self.assertEqual(self.store.list_visits(self.guest.id), expected[:5])
        self.assertEqual(self.store.list_visits(self.guest.id, limit=3, offset=5), expected[5:])
        for kwargs in ({"limit": 0}, {"limit": True}, {"offset": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_visits(self.guest.id, **kwargs)

    def test_delete_cascades_all_visits_and_missing_guest_is_empty(self):
        first = self.begin()
        self.store.stop_visit(self.guest.id, first.id, 2002, now=self.start + timedelta(minutes=1))
        active = self.begin(now=self.start + timedelta(hours=1))
        with self.assertRaises(StaleGuestError):
            self.store.delete_guest(self.guest.id, self.guest.version)
        self.store.delete_guest(self.guest.id, self.current_version())
        self.assertEqual(self.store.list_visits(self.guest.id), [])
        self.assertEqual(self.store.get_visit_summary(self.guest.id, now=self.start), VisitSummary(None, 0, 0, 0))
        with self.assertRaises(StaleGuestError):
            self.store.start_visit(self.guest.id, 999, 1001, now=self.start)
        with self.assertRaises(VisitStateError):
            self.store.stop_visit(self.guest.id, active.id, 1001, now=self.start)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_stop_visit_cannot_target_another_guests_session(self):
        visit = self.begin()
        other = self.store.add_guest(name="Другой гость", phone="79990000000", comment="",
                                     photo=b"other-photo", photo_file_id="other-id", actor_id=2002)
        with self.assertRaises(VisitStateError):
            self.store.stop_visit(other.id, visit.id, 2002, now=self.start + timedelta(minutes=1))
        self.assertEqual(self.store.get_visit_summary(self.guest.id, now=self.start).active, visit)

    def test_backup_restores_history_running_visit_and_photos(self):
        first = self.begin()
        completed = self.store.stop_visit(self.guest.id, first.id, 2002, now=self.start + timedelta(hours=1))
        current_start = self.start + timedelta(days=1)
        active = self.begin(now=current_start)
        backup = self.store.backup(self.root / "backup.sqlite3")
        self.store.stop_visit(self.guest.id, active.id, 3003, now=current_start + timedelta(minutes=10))
        restored = Store(backup)
        self.assertEqual(restored.list_visits(self.guest.id), [active, completed])
        summary = restored.get_visit_summary(self.guest.id, now=current_start + timedelta(minutes=5))
        self.assertEqual(summary.total_seconds, 3900)
        self.assertEqual(summary.active, active)
        self.assertEqual(restored.get_guest(self.guest.id).photo, self.guest.photo)

    def create_legacy_database(self, path):
        # Build the exact pre-visits schema without ever invoking new Store.
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""
                CREATE TABLE guests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    name_key TEXT NOT NULL, phone TEXT NOT NULL, phone_key TEXT NOT NULL UNIQUE,
                    comment TEXT NOT NULL, photo BLOB NOT NULL, photo_file_id TEXT NOT NULL,
                    created_by INTEGER NOT NULL, updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
                )
            """)
            connection.execute("""
                INSERT INTO guests (name, name_key, phone, phone_key, comment, photo,
                    photo_file_id, created_by, updated_by, created_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("Алёна Иванова", "алена иванова", "+7 999 123 45 67", "79991234567",
                  "Старая карточка", b"legacy-photo-bytes", "legacy-file-id", 1001, 2002,
                  "2026-10-08T09:00:00.000000+00:00", "2026-10-08T10:00:00.000000+00:00", 7))

    def test_legacy_migration_keeps_card_and_makes_pre_migration_backup_once(self):
        legacy_path = self.root / "legacy.sqlite3"
        self.create_legacy_database(legacy_path)
        upgraded = Store(legacy_path)
        card = upgraded.get_guest(1)
        self.assertEqual(card.name, "Алёна Иванова")
        self.assertEqual(card.photo, b"legacy-photo-bytes")
        self.assertEqual(card.version, 7)
        self.assertEqual(card.updated_by, 2002)
        self.assertEqual(upgraded.get_visit_summary(card.id, now=self.start), VisitSummary(None, 0, 0, 0))
        backups = list((self.root / "backups").glob("guests-before-visits-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertEqual(connection.execute("SELECT photo, version FROM guests WHERE id = 1").fetchone(),
                             (b"legacy-photo-bytes", 7))
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='visits'").fetchone())
        visit = upgraded.start_visit(card.id, card.version, 1001, now=self.start)
        Store(legacy_path)
        self.assertEqual(upgraded.list_visits(card.id), [visit])
        self.assertEqual(list((self.root / "backups").glob("guests-before-visits-*.sqlite3")), backups)
        self.assertFalse((self.store.path.parent / "backups").exists())

    def test_migration_backup_failure_aborts_before_schema_changes(self):
        legacy_path = self.root / "legacy-failure.sqlite3"
        self.create_legacy_database(legacy_path)
        with patch.object(Store, "backup", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                Store(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("SELECT photo, version FROM guests WHERE id = 1").fetchone(),
                             (b"legacy-photo-bytes", 7))
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='visits'").fetchone())

    def test_database_unique_index_rejects_a_second_active_visit(self):
        self.begin()
        with self.store._connection() as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO visits (guest_id, started_at, started_by) VALUES (?, ?, ?)",
                                   (self.guest.id, self.start.isoformat(), 2002))

    def test_real_time_is_captured_only_after_write_lock_is_acquired(self):
        real_clock = storage._utc_datetime
        clock_calls = []

        def assert_locked_then_read_clock(now=None):
            with closing(sqlite3.connect(self.store.path, timeout=0)) as contender:
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
            clock_calls.append(True)
            return real_clock(now)

        with patch("storage._utc_datetime", side_effect=assert_locked_then_read_clock):
            visit = self.store.start_visit(self.guest.id, self.guest.version, 1001)
            self.store.stop_visit(self.guest.id, visit.id, 2002)
        self.assertGreaterEqual(len(clock_calls), 2)

    def test_summary_keeps_one_snapshot_when_visit_stops_between_reads(self):
        visit = self.begin()
        other_employee = Store(self.store.path)
        original_connection = self.store._connection
        stopped_between_reads = []

        class ConnectionProxy:
            def __init__(proxy, connection):
                proxy.connection = connection

            def __enter__(proxy):
                proxy.connection.__enter__()
                return proxy

            def __exit__(proxy, *args):
                return proxy.connection.__exit__(*args)

            def execute(proxy, sql, *args):
                if "SELECT started_at, stopped_at" in sql and not stopped_between_reads:
                    other_employee.stop_visit(self.guest.id, visit.id, 2002,
                                              now=self.start + timedelta(seconds=60))
                    stopped_between_reads.append(True)
                return proxy.connection.execute(sql, *args)

        @contextmanager
        def instrumented_connection():
            with original_connection() as connection:
                yield ConnectionProxy(connection)

        with patch.object(self.store, "_connection", instrumented_connection):
            summary = self.store.get_visit_summary(self.guest.id, now=self.start + timedelta(seconds=60))
        self.assertEqual(stopped_between_reads, [True])
        self.assertEqual(summary.active, visit)
        self.assertEqual(summary.completed_count, 0)
        self.assertEqual(summary.current_seconds, 60)
        self.assertEqual(summary.total_seconds, 60)
        after = self.store.get_visit_summary(self.guest.id, now=self.start + timedelta(seconds=60))
        self.assertIsNone(after.active)
        self.assertEqual(after.completed_count, 1)
        self.assertEqual(after.total_seconds, 60)


if __name__ == "__main__":
    unittest.main()
