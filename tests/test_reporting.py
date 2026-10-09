from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import reporting
from reporting import ReportError, report_snapshot
from storage import Store, VisitSummary


UTC = timezone.utc


class ReportingTests(unittest.TestCase):
    def setUp(self):
        temporary_base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = temporary_base / ("guest-report-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "primary" / "guests.sqlite3")
        self.now = datetime(2026, 10, 9, 12, tzinfo=UTC)

    def add(self, store=None, *, status="open", number=1, name="Тестовый гость"):
        return (store or self.store).add_guest(
            name=name, phone=f"7999000{number:04d}", comment="Тестовый комментарий",
            photo=b"fake-test-photo", photo_file_id="test-photo-id", actor_id=101,
            entry_status=status, entry_reason="Тестовая причина" if status == "closed" else "")

    def legacy(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""CREATE TABLE guests (
                id INTEGER PRIMARY KEY, name TEXT, phone TEXT, phone_key TEXT,
                comment TEXT, photo BLOB, photo_file_id TEXT, created_by INTEGER,
                updated_by INTEGER, created_at TEXT, updated_at TEXT, version INTEGER)""")
            connection.execute("INSERT INTO guests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (1, "Старый тестовый гость", "+79990000001", "79990000001", "Комментарий",
                                b"legacy-photo", "legacy-file-id", 101, 101,
                                self.now.isoformat(), self.now.isoformat(), 1))
        return path

    def test_empty_sources_and_snapshot_immutable(self):
        snapshot = report_snapshot(self.store, now=self.now)
        self.assertEqual(snapshot.total_count, 0)
        self.assertEqual(snapshot.blacklist, ())
        self.assertEqual(snapshot.sources, ("Основная база",))
        self.assertEqual(snapshot.generated_at, self.now)
        with self.assertRaises(FrozenInstanceError):
            snapshot.rows = ()

    def test_all_sources_included_and_guest_ids_do_not_collide(self):
        first = self.add(status="closed")
        extra = Store(self.root / "extra" / "guests.sqlite3")
        second = self.add(extra, number=1, name="Второй тестовый гость", status="closed")
        self.add(extra, number=2)
        snapshot = report_snapshot(self.store.path, (extra.path,), now=self.now)
        self.assertEqual(first.id, second.id)
        self.assertEqual(snapshot.total_count, 3)
        self.assertEqual(snapshot.sources, ("Основная база", "База 2"))
        self.assertEqual([row.source_index for row in snapshot.rows], [1, 2, 2])
        self.assertEqual([row.source_name for row in snapshot.blacklist], ["Основная база", "База 2"])
        self.assertEqual(len(snapshot.blacklist), 2)
        self.assertTrue(all(row.guest.entry_status == "closed" for row in snapshot.blacklist))
        self.assertTrue(all(row.guest.photo == b"" for row in snapshot.rows))
        with self.assertRaises(FrozenInstanceError):
            snapshot.rows[0].guest.name = "changed"

    def test_duplicate_resolved_and_hardlink_paths_read_only_once(self):
        self.add()
        alias = self.root / "hardlink.sqlite3"
        os.link(self.store.path, alias)
        snapshot = report_snapshot(self.store, (self.store.path, self.store.path.parent / ".." /
                                               "primary" / "guests.sqlite3", alias), now=self.now)
        self.assertEqual(snapshot.total_count, 1)
        self.assertEqual(snapshot.sources, ("Основная база",))

    def test_legacy_sources_default_open_and_no_visits_without_migration(self):
        legacy = self.legacy(self.root / "legacy # spaces" / "guests.sqlite3")
        before = legacy.read_bytes()
        snapshot = report_snapshot(legacy, include_photos=True, now=self.now)
        row = snapshot.rows[0]
        self.assertEqual(row.guest.entry_status, "open")
        self.assertEqual(row.guest.entry_reason, "")
        self.assertEqual(row.guest.photo, b"legacy-photo")
        self.assertEqual(row.summary, VisitSummary(None, 0, 0, 0))
        self.assertEqual(row.visits, ())
        self.assertEqual(snapshot.blacklist, ())
        self.assertEqual(legacy.read_bytes(), before)
        with closing(sqlite3.connect(legacy)) as connection, connection:
            self.assertNotIn("entry_status", {row[1] for row in connection.execute("PRAGMA table_info(guests)")})
            self.assertNotIn("visits", {row[0] for row in connection.execute("SELECT name FROM sqlite_master")})

    def test_timing_uses_same_now_and_completed_visits_stay_exact(self):
        guest = self.add(status="closed")
        start = self.now - timedelta(days=2, hours=2, minutes=5)
        visit = self.store.start_visit(guest.id, guest.version, 101, now=start)
        self.store.stop_visit(guest.id, visit.id, 202, now=start + timedelta(hours=26, seconds=0.9))
        guest = self.store.get_guest(guest.id)
        active = self.store.start_visit(guest.id, guest.version, 303, now=self.now - timedelta(minutes=5))
        snapshot = report_snapshot(self.store, now=self.now)
        row = snapshot.rows[0]
        self.assertEqual(row.summary.completed_count, 1)
        self.assertEqual(row.summary.completed_seconds, 26 * 3600)
        self.assertEqual(row.summary.current_seconds, 300)
        self.assertEqual(row.summary.total_seconds, 26 * 3600 + 300)
        self.assertEqual(row.summary.active, active)
        self.assertEqual([item.id for item in row.visits], [active.id, visit.id])

    def test_photo_transform_receives_raw_bytes_and_snapshot_keeps_only_thumbnail(self):
        self.add(status="closed")
        self.add(number=2)
        calls = []

        def thumbnail(photo):
            calls.append(photo)
            return b"small-thumbnail"

        snapshot = report_snapshot(self.store, include_photos=True, photo_transform=thumbnail, now=self.now)
        self.assertEqual(calls, [b"fake-test-photo", b"fake-test-photo"])
        self.assertTrue(all(row.guest.photo == b"small-thumbnail" for row in snapshot.rows))
        self.assertEqual(self.store.get_guest(1).photo, b"fake-test-photo")
        calls.clear()
        without_photos = report_snapshot(self.store, photo_transform=thumbnail, now=self.now)
        self.assertEqual(calls, [])
        self.assertEqual(without_photos.total_count, 2)
        self.assertEqual(len(without_photos.blacklist), 1)
        self.assertTrue(all(row.guest.photo == b"" for row in without_photos.rows))

    def test_photo_transform_failure_aborts_report_and_hides_original_error(self):
        self.add()

        def broken(photo):
            raise RuntimeError("private photo value and internal location")

        for transform in (broken, lambda photo: "wrong type"):
            with self.subTest(transform=transform), self.assertRaises(ReportError) as raised:
                report_snapshot(self.store, include_photos=True, photo_transform=transform, now=self.now)
            self.assertNotIn("private", str(raised.exception))
            self.assertNotIn("wrong type", str(raised.exception))

    def test_missing_extra_is_not_created_and_no_partial_result_returned(self):
        self.add()
        missing = self.root / "secret-location" / "missing.sqlite3"
        with self.assertRaises(ReportError) as raised:
            report_snapshot(self.store, (missing,), now=self.now)
        self.assertIn("База 2", str(raised.exception))
        self.assertNotIn("secret-location", str(raised.exception))
        self.assertNotIn("missing.sqlite3", str(raised.exception))
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def test_missing_primary_never_created(self):
        path = self.root / "absent.sqlite3"
        with self.assertRaises(ReportError) as raised:
            report_snapshot(path, now=self.now)
        self.assertIn("Основная база", str(raised.exception))
        self.assertFalse(path.exists())

    def test_corrupt_incompatible_or_invalid_sources_fail_sanitized(self):
        corrupt = self.root / "corrupt-secret.sqlite3"
        corrupt.write_bytes(b"this is not a database, private-test-value")
        incompatible = self.root / "incompatible-secret.sqlite3"
        with closing(sqlite3.connect(incompatible)) as connection, connection:
            connection.execute("CREATE TABLE guests (id INTEGER PRIMARY KEY)")
        invalid = self.legacy(self.root / "invalid-secret.sqlite3")
        with closing(sqlite3.connect(invalid)) as connection, connection:
            connection.execute("UPDATE guests SET created_at='private-invalid-data'")
        for path in (corrupt, incompatible, invalid, self.root):
            with self.subTest(path=path.name), self.assertRaises(ReportError) as raised:
                report_snapshot(self.store, (path,), now=self.now)
            self.assertNotIn("secret", str(raised.exception))
            self.assertNotIn("private", str(raised.exception))

    def test_corrupt_status_is_not_silently_omitted_from_blacklist(self):
        path = self.legacy(self.root / "invalid-status.sqlite3")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("ALTER TABLE guests ADD COLUMN entry_status TEXT")
            connection.execute("UPDATE guests SET entry_status='CLOSED'")
        with self.assertRaises(ReportError):
            report_snapshot(path, now=self.now)

    def test_invalid_or_orphan_visits_fail_entire_report(self):
        self.add()
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("INSERT INTO visits (guest_id, started_at, started_by) VALUES (?, ?, ?)",
                               (999, self.now.isoformat(), 101))
        with self.assertRaises(ReportError):
            report_snapshot(self.store, now=self.now)

    def test_one_source_uses_coherent_read_transaction_and_query_only(self):
        guest = self.add()
        original_connect = sqlite3.connect
        mutation_done = []
        guards_checked = []
        path = self.store.path
        now = self.now

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def execute(self, query, *args):
                if query.startswith("SELECT") and " FROM visits" in query and not mutation_done:
                    self.assert_guards()
                    # An independent staff write occurs after guests were read
                    # but before visits. WAL permits it; report sees neither.
                    with closing(original_connect(path)) as writer, writer:
                        writer.execute("UPDATE guests SET name='Changed test name' WHERE id=?", (guest.id,))
                        writer.execute("INSERT INTO visits (guest_id, started_at, started_by) VALUES (?, ?, ?)",
                                       (guest.id, now.isoformat(), 101))
                    mutation_done.append(True)
                return self.connection.execute(query, *args)

            def assert_guards(self):
                if not self.connection.in_transaction:
                    raise AssertionError("Report must remain in its read transaction")
                if self.connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                    raise AssertionError("Query-only write guard required")
                guards_checked.append(True)

            def rollback(self):
                self.connection.rollback()

            def close(self):
                self.connection.close()

        def connect(*args, **kwargs):
            self.assertIn("?mode=ro", args[0])
            self.assertTrue(kwargs["uri"])
            return ConnectionProxy(original_connect(*args, **kwargs))

        with patch.object(reporting.sqlite3, "connect", side_effect=connect):
            snapshot = report_snapshot(self.store, now=self.now)
        self.assertEqual(mutation_done, [True])
        self.assertEqual(guards_checked, [True])
        self.assertEqual(snapshot.rows[0].guest.name, guest.name)
        self.assertEqual(snapshot.rows[0].visits, ())
        self.assertEqual(self.store.get_guest(guest.id).name, "Changed test name")
        self.assertEqual(len(self.store.list_visits(guest.id)), 1)

    def test_report_now_requires_timezone(self):
        with self.assertRaises(ValueError):
            report_snapshot(self.store, now=datetime(2026, 10, 9))


if __name__ == "__main__":
    unittest.main()
