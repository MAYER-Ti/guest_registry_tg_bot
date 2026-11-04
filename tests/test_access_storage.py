"""Durable employee grants stay separate from remembered Telegram profiles."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from storage import EmployeeAccess, MAX_ACCESS_ID, Store


ADMIN = 7011639945


class EmployeeAccessStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ".test-runtime" / ("access-" + uuid4().hex)
        self.root.mkdir(parents=True, mode=0o777)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "data" / "guests.sqlite3")

    def bootstrap(self, initial=(101, 102, ADMIN)):
        self.store.bootstrap_employee_access(initial, ADMIN)

    def test_seed_excludes_admin_duplicates_and_historical_placeholder(self):
        self.bootstrap((ADMIN, 101, 101, 102, 2**53))
        self.assertEqual(self.store.employee_ids(), frozenset({101, 102}))
        self.assertEqual(self.store.count_employees(), 2)
        self.assertFalse(self.store.has_employee_access(ADMIN))
        entries = self.store.list_employees()
        self.assertTrue(all(isinstance(entry, EmployeeAccess) for entry in entries))
        self.assertEqual([entry.user_id for entry in entries], [101, 102])
        self.assertTrue(all(entry.added_by == ADMIN for entry in entries))
        self.assertIsNotNone(datetime.fromisoformat(entries[0].added_at).utcoffset())
        with self.assertRaises(FrozenInstanceError):
            entries[0].user_id = 999
        self.assertFalse((self.store.path.parent / "backups").exists())

    def test_restart_never_reseeds_environment_or_regrants_a_missing_employee(self):
        self.bootstrap()
        self.assertTrue(self.store.add_employee(103, ADMIN))
        with self.store._connection() as connection, connection:
            connection.execute("DELETE FROM employee_access WHERE user_id = 101")
        reopened = Store(self.store.path)
        reopened.bootstrap_employee_access((101, 102, 104, ADMIN), ADMIN)
        self.assertEqual(reopened.employee_ids(), frozenset({102, 103}))
        self.assertEqual(reopened.count_employees(), 2)

    def test_different_administrator_cannot_change_pinned_owner_or_grants(self):
        self.bootstrap()
        before = self.store.list_employees()
        with self.assertRaises(ValueError):
            self.store.bootstrap_employee_access((999,), 202)
        self.assertEqual(self.store.list_employees(), before)
        with self.assertRaises(PermissionError):
            self.store.add_employee(999, 202)
        self.assertTrue(self.store.add_employee(103, ADMIN))

    def test_addition_is_idempotent_and_profiles_never_grant_access(self):
        self.store.remember_staff(999, "Знакомое имя", "remembered_staff")
        self.assertFalse(self.store.has_employee_access(999))
        self.bootstrap(())
        self.assertTrue(self.store.add_employee(999, ADMIN))
        before = self.store.list_employees()
        self.assertFalse(self.store.add_employee(999, ADMIN))
        self.assertFalse(self.store.add_employee(ADMIN, ADMIN))
        self.assertEqual(self.store.list_employees(), before)
        self.assertEqual(self.store.staff_label(999), "Знакомое имя (@remembered_staff)")
        self.assertEqual(self.store.count_audit(), 0)
        self.assertTrue(Store(self.store.path).has_employee_access(999))

    def test_uninitialized_and_nonadministrator_cannot_add_employees(self):
        for actor in (ADMIN, 101):
            with self.assertRaises(PermissionError):
                self.store.add_employee(999, actor)
        self.bootstrap()
        before = self.store.list_employees()
        for actor in (101, 102, 999):
            with self.assertRaises(PermissionError):
                self.store.add_employee(999, actor)
        self.assertEqual(self.store.list_employees(), before)

    def test_full_ids_are_not_limited_by_pagination(self):
        self.bootstrap(range(1, 206))
        self.assertEqual(self.store.employee_ids(), frozenset(range(1, 206)))
        self.assertEqual(self.store.count_employees(), 205)
        combined = []
        for offset in range(0, 205, 8):
            combined.extend(entry.user_id for entry in self.store.list_employees(8, offset))
        self.assertEqual(combined, list(range(1, 206)))
        self.assertEqual(self.store.list_employees(offset=1000), [])

    def test_racing_duplicate_grants_commit_exactly_once(self):
        self.bootstrap(())
        barrier = Barrier(2)

        def add(_):
            barrier.wait()
            return self.store.add_employee(999, ADMIN)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(add, range(2))), [False, True])
        self.assertEqual(self.store.count_employees(), 1)
        self.assertEqual(self.store.list_employees()[0].added_by, ADMIN)

    def test_racing_bootstrap_is_one_complete_seed(self):
        barrier = Barrier(2)

        def bootstrap(ids):
            barrier.wait()
            self.store.bootstrap_employee_access(ids, ADMIN)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(bootstrap, ((101, 102), (201, 202))))
        self.assertIn(self.store.employee_ids(), (frozenset({101, 102}), frozenset({201, 202})))

    def test_invalid_ids_and_pages_are_rejected_without_mutation(self):
        self.bootstrap(())
        invalid = (True, False, 0, -1, "101", "١٠١", 1.0, None, MAX_ACCESS_ID + 1, 2**63)
        for value in invalid:
            for operation in (lambda: self.store.add_employee(value, ADMIN),
                              lambda: self.store.add_employee(101, value),
                              lambda: self.store.has_employee_access(value)):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    operation()
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_employees(**kwargs)
        self.assertEqual(self.store.count_employees(), 0)
        self.assertTrue(self.store.add_employee(MAX_ACCESS_ID, ADMIN))

    def test_invalid_seed_aborts_before_settings_or_employee_writes(self):
        for initial in (None, 123, (101, True), (101, "102"), (101, 2**53 + 1)):
            with self.subTest(initial=initial), self.assertRaises(ValueError):
                self.store.bootstrap_employee_access(initial, ADMIN)
        with self.assertRaises(ValueError):
            self.store.bootstrap_employee_access((101,), True)
        self.assertEqual(self.store.employee_ids(), frozenset())
        self.bootstrap((102,))
        self.assertEqual(self.store.employee_ids(), frozenset({102}))

    def test_failed_seed_rolls_back_settings_and_all_employee_inserts(self):
        with self.store._connection() as connection, connection:
            connection.execute("""
                CREATE TRIGGER reject_test_employee BEFORE INSERT ON employee_access
                WHEN NEW.user_id = 999 BEGIN SELECT RAISE(ABORT, 'test failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.bootstrap((101, 999))
        self.assertEqual(self.store.employee_ids(), frozenset())
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM access_settings").fetchone()[0], 0)
        self.bootstrap((102,))
        self.assertEqual(self.store.employee_ids(), frozenset({102}))

    def previous_schema(self):
        guest = self.store.add_guest(name="Старая карточка", phone="79991234567", comment="Комментарий",
                                     photo=b"preserved-photo", photo_file_id="preserved-file-id", actor_id=ADMIN,
                                     entry_status="closed", entry_reason="Причина")
        visit = self.store.start_visit(guest.id, guest.version, ADMIN)
        self.store.remember_staff(ADMIN, "Администратор", "owner")
        self.store.mark_reminder_sent(visit.id, ADMIN)
        with self.store._connection() as connection, connection:
            old_tables = ("guests", "visits", "audit_events", "staff", "visit_reminders")
            before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                      for table in old_tables}
            connection.execute("DROP TABLE employee_access")
            connection.execute("DROP TABLE access_settings")
        return before

    def assert_previous_rows_preserved(self, path, before):
        with closing(sqlite3.connect(path)) as connection:
            for table, expected in before.items():
                self.assertEqual(connection.execute(f"SELECT * FROM {table}").fetchall(), expected)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_upgrade_backs_up_once_preserves_all_data_and_does_not_import_profiles(self):
        before = self.previous_schema()
        upgraded = Store(self.store.path)
        self.assert_previous_rows_preserved(upgraded.path, before)
        self.assertEqual(upgraded.count_employees(), 0)
        self.assertFalse(upgraded.has_employee_access(ADMIN))
        backups = list((upgraded.path.parent / "backups").glob("guests-before-access-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assert_previous_rows_preserved(backups[0], before)
        with closing(sqlite3.connect(backups[0])) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"employee_access", "access_settings"}.isdisjoint(names))
        Store(upgraded.path)
        self.assertEqual(list((upgraded.path.parent / "backups").glob("*.sqlite3")), backups)

    def test_backup_failure_leaves_previous_schema_intact(self):
        before = self.previous_schema()
        with patch.object(Store, "backup", side_effect=OSError("test backup failure")):
            with self.assertRaises(OSError):
                Store(self.store.path)
        self.assert_previous_rows_preserved(self.store.path, before)
        with self.store._connection() as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"employee_access", "access_settings"}.isdisjoint(names))

    def test_schema_failure_rolls_back_partial_access_migration(self):
        before = self.previous_schema()
        original_connection = Store._connection

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if "CREATE TABLE IF NOT EXISTS access_settings" in sql:
                    raise sqlite3.OperationalError("test final DDL failure")
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def instrumented(store):
            with original_connection(store) as connection:
                yield ConnectionProxy(connection)

        with patch.object(Store, "_connection", instrumented):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.store.path)
        self.assert_previous_rows_preserved(self.store.path, before)
        with self.store._connection() as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"employee_access", "access_settings"}.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
