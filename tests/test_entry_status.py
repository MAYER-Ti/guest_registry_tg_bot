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

from storage import StaleGuestError, Store


class EntryStatusStorageTests(unittest.TestCase):
    def setUp(self):
        temporary_base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = temporary_base / ("guest-entry-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "data" / "guests.sqlite3")
        self.fields = dict(name="Алёна", phone="89991234567", comment="Комментарий",
                           photo=b"guest-photo", photo_file_id="telegram-photo", actor_id=1001)

    def test_new_cards_default_to_open_and_both_fields_survive_restart_and_lookup(self):
        guest = self.store.add_guest(**self.fields)
        self.assertEqual((guest.entry_status, guest.entry_reason), ("open", ""))
        changed = self.store.update_guest(guest.id, guest.version, 2002,
                                          entry_status="closed", entry_reason="  Нужна проверка  ")
        reopened = Store(self.store.path)
        for card in (reopened.get_guest(guest.id), reopened.find_phone(guest.phone),
                     reopened.search_guests("алена")[0]):
            self.assertEqual((card.entry_status, card.entry_reason), ("closed", "Нужна проверка"))
            self.assertEqual(card.version, changed.version)
        self.assertEqual(reopened.get_guest(guest.id).photo, guest.photo)
        self.assertFalse((self.store.path.parent / "backups").exists())

    def test_explicit_status_and_optional_reason_at_creation(self):
        guest = self.store.add_guest(**self.fields, entry_status="closed", entry_reason="  Конфликт  ")
        self.assertEqual((guest.entry_status, guest.entry_reason), ("closed", "Конфликт"))
        reopened = self.store.update_guest(guest.id, guest.version, 2002, entry_status="open")
        self.assertEqual((reopened.entry_status, reopened.entry_reason), ("open", "Конфликт"))
        cleared = self.store.update_guest(guest.id, reopened.version, 3003, entry_reason="   ")
        self.assertEqual((cleared.entry_status, cleared.entry_reason), ("open", ""))
        self.assertEqual(cleared.updated_by, 3003)
        self.assertEqual(cleared.created_by, guest.created_by)

    def test_invalid_entry_fields_are_rejected_without_creating_or_changing_a_card(self):
        invalid = [dict(entry_status="blocked"), dict(entry_status="OPEN"),
                   dict(entry_status=""), dict(entry_status=None), dict(entry_status=True),
                   dict(entry_reason=None), dict(entry_reason=b"reason"),
                   dict(entry_reason="я" * 3001)]
        for fields in invalid:
            with self.subTest(create=fields), self.assertRaises(ValueError):
                self.store.add_guest(**self.fields, **fields)
        self.assertEqual(self.store.count_search("Алёна"), 0)
        guest = self.store.add_guest(**self.fields)
        for fields in invalid:
            with self.subTest(update=fields), self.assertRaises(ValueError):
                self.store.update_guest(guest.id, guest.version, 2002, **fields)
            self.assertEqual(self.store.get_guest(guest.id), guest)
        changed = self.store.update_guest(guest.id, guest.version, 2002,
                                          entry_reason="я" * 3000)
        self.assertEqual(len(changed.entry_reason), 3000)

    def test_two_staff_update_status_and_reason_as_one_atomic_edit(self):
        guest = self.store.add_guest(**self.fields)
        barrier = Barrier(2)

        def edit(pair):
            barrier.wait()
            try:
                return self.store.update_guest(guest.id, guest.version, pair[0],
                                                entry_status="closed", entry_reason=pair[1])
            except StaleGuestError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, [(2002, "Причина первого"), (3003, "Причина второго")]))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        current = self.store.get_guest(guest.id)
        self.assertEqual(current, winners[0])
        self.assertEqual(current.version, guest.version + 1)
        self.assertEqual(current.entry_status, "closed")
        self.assertEqual(current.entry_reason,
                         {2002: "Причина первого", 3003: "Причина второго"}[current.updated_by])
        with self.assertRaises(StaleGuestError):
            self.store.update_guest(guest.id, guest.version, 1001,
                                    entry_status="open", entry_reason="старое изменение")
        self.assertEqual(self.store.get_guest(guest.id), current)

    def test_entry_fields_do_not_reset_or_block_visits(self):
        guest = self.store.add_guest(**self.fields, entry_status="closed", entry_reason="Причина")
        start = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
        visit = self.store.start_visit(guest.id, guest.version, 1001, now=start)
        current = self.store.get_guest(guest.id)
        edited = self.store.update_guest(guest.id, current.version, 2002,
                                         entry_status="open", entry_reason="Уточнено")
        self.assertEqual(self.store.get_visit_summary(guest.id, now=start).active, visit)
        self.store.stop_visit(guest.id, visit.id, 3003, now=start + timedelta(minutes=20))
        self.assertEqual(self.store.get_visit_summary(guest.id).total_seconds, 1200)
        final = self.store.get_guest(guest.id)
        self.assertEqual((final.entry_status, final.entry_reason), ("open", "Уточнено"))
        self.assertEqual(final.photo, guest.photo)
        self.assertEqual(final.comment, guest.comment)
        self.assertEqual(final.version, edited.version + 1)

    def create_previous_database(self, path, *, visits=True, partial_column=None):
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
            if partial_column == "entry_status":
                connection.execute("ALTER TABLE guests ADD COLUMN entry_status TEXT NOT NULL DEFAULT 'open'")
                connection.execute("UPDATE guests SET entry_status='closed'")
            elif partial_column == "entry_reason":
                connection.execute("ALTER TABLE guests ADD COLUMN entry_reason TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE guests SET entry_reason='Ранее сохранённая причина'")
            if visits:
                connection.execute("""
                    CREATE TABLE visits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
                        started_at TEXT NOT NULL, stopped_at TEXT, started_by INTEGER NOT NULL,
                        stopped_by INTEGER,
                        CHECK ((stopped_at IS NULL) = (stopped_by IS NULL)),
                        CHECK (stopped_at IS NULL OR stopped_at >= started_at)
                    )
                """)
                connection.executemany("""
                    INSERT INTO visits (guest_id, started_at, stopped_at, started_by, stopped_by)
                    VALUES (?, ?, ?, ?, ?)
                """, [(1, "2026-10-08T09:00:00+00:00", "2026-10-08T10:00:00+00:00", 1001, 2002),
                      (1, "2026-10-09T09:00:00+00:00", None, 3003, None)])
            card = connection.execute("SELECT * FROM guests").fetchall()
            history = connection.execute("SELECT * FROM visits").fetchall() if visits else []
        return card, history

    def assert_existing_data_unchanged(self, path, card, history, *, visits=True):
        with closing(sqlite3.connect(path)) as connection:
            preserved_columns = "id,name,name_key,phone,phone_key,comment,photo,photo_file_id,created_by,updated_by,created_at,updated_at,version"
            self.assertEqual(connection.execute(f"SELECT {preserved_columns} FROM guests").fetchall(),
                             [tuple(row[:13]) for row in card])
            if visits:
                self.assertEqual(connection.execute("SELECT * FROM visits").fetchall(), history)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_current_visits_database_migrates_with_backup_and_preserves_all_data(self):
        path = self.root / "current.sqlite3"
        card, history = self.create_previous_database(path)
        upgraded = Store(path)
        self.assert_existing_data_unchanged(path, card, history)
        guest = upgraded.get_guest(1)
        self.assertEqual((guest.entry_status, guest.entry_reason), ("open", ""))
        backups = list((self.root / "backups").glob("guests-before-entry-status-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assert_existing_data_unchanged(backups[0], card, history)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertNotIn("entry_status", {row[1] for row in connection.execute("PRAGMA table_info(guests)")})
        Store(path)
        self.assertEqual(list((self.root / "backups").glob("*.sqlite3")), backups)
        self.assertEqual(upgraded.get_visit_summary(1, now=datetime(2026, 10, 9, 9, 1, tzinfo=timezone.utc)).total_seconds,
                         3660)

    def test_old_pre_visits_database_uses_one_backup_and_adds_all_fields(self):
        path = self.root / "oldest.sqlite3"
        card, history = self.create_previous_database(path, visits=False)
        upgraded = Store(path)
        self.assert_existing_data_unchanged(path, card, history)
        guest = upgraded.get_guest(1)
        self.assertEqual((guest.entry_status, guest.entry_reason), ("open", ""))
        backups = list((self.root / "backups").glob("guests-before-visits-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assert_existing_data_unchanged(backups[0], card, history, visits=False)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='visits'").fetchone())

    def test_partially_migrated_schemas_keep_existing_status_or_reason(self):
        for column in ("entry_status", "entry_reason"):
            with self.subTest(column=column):
                folder = self.root / column
                folder.mkdir()
                path = folder / "partial.sqlite3"
                card, history = self.create_previous_database(path, partial_column=column)
                upgraded = Store(path)
                self.assert_existing_data_unchanged(path, card, history)
                guest = upgraded.get_guest(1)
                self.assertEqual(guest.entry_status, "closed" if column == "entry_status" else "open")
                self.assertEqual(guest.entry_reason, "Ранее сохранённая причина" if column == "entry_reason" else "")
                self.assertEqual(len(list((folder / "backups").glob("*.sqlite3"))), 1)
                Store(path)
                self.assertEqual(len(list((folder / "backups").glob("*.sqlite3"))), 1)

    def test_failed_backup_changes_no_schema_or_existing_data(self):
        path = self.root / "failure.sqlite3"
        card, history = self.create_previous_database(path)
        with patch.object(Store, "backup", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                Store(path)
        self.assert_existing_data_unchanged(path, card, history)
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(guests)")}
            self.assertNotIn("entry_status", columns)
            self.assertNotIn("entry_reason", columns)

    def test_second_column_failure_rolls_back_first_column(self):
        path = self.root / "atomic.sqlite3"
        card, history = self.create_previous_database(path)
        original_connection = Store._connection

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if "ADD COLUMN entry_reason" in sql:
                    raise sqlite3.OperationalError("simulated second ALTER failure")
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def instrumented_connection(store):
            with original_connection(store) as connection:
                yield ConnectionProxy(connection)

        with patch.object(Store, "_connection", instrumented_connection):
            with self.assertRaises(sqlite3.OperationalError):
                Store(path)
        self.assert_existing_data_unchanged(path, card, history)
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(guests)")}
            self.assertNotIn("entry_status", columns)
            self.assertNotIn("entry_reason", columns)
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
