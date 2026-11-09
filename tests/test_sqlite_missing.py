"""A lost or invalid production database must never become an empty registry."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from storage import Store


class MissingDatabaseTests(unittest.TestCase):
    def setUp(self):
        base = (Path.cwd() / ".test-runtime").resolve()
        self.root = (base / ("sqlite-missing-" + uuid4().hex)).resolve()
        self.assertEqual(self.root.parent, base)
        self.root.mkdir(parents=True, mode=0o777)
        self.addCleanup(shutil.rmtree, self.root)

    def add(self, store):
        return store.add_guest(name="Тестовый гость", phone="+79991234567", comment="Комментарий",
                               photo=b"saved-photo", photo_file_id="test-file-id", actor_id=101)

    def test_default_creation_remains_explicitly_available(self):
        path = self.root / "new directory" / "guests.sqlite3"
        store = Store(path)
        self.assertTrue(path.is_file())
        guest = self.add(store)
        self.assertEqual(store.get_guest(guest.id), guest)

    def test_missing_existing_only_database_creates_neither_file_nor_parent(self):
        path = self.root / "private-token-folder" / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError) as caught:
            Store(path, create=False)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertNotIn("private-token-folder", str(caught.exception))
        self.assertNotIn(str(path), str(caught.exception))
        self.assertIsNone(caught.exception.filename)

    def test_missing_file_in_existing_parent_is_not_created(self):
        path = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            Store(path, create=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_registry_reopens_with_unicode_spaces_and_uri_characters(self):
        path = self.root / "Гости # 1 % данные.sqlite3"
        original = Store(path)
        guest = self.add(original)
        original.remember_staff(101, "Сотрудник", "test_staff")
        original.bootstrap_employee_access({101}, 7011639945)
        visit = original.start_visit(guest.id, guest.version, 101)
        expected_guest = original.get_guest(guest.id)
        expected_audit = original.list_audit()
        reopened = Store(path, create=False)
        self.assertEqual(reopened.get_guest(guest.id), expected_guest)
        self.assertEqual(reopened.list_visits(guest.id), [visit])
        self.assertEqual(reopened.list_audit(), expected_audit)
        self.assertEqual(reopened.employee_ids(), frozenset({101}))
        self.assertEqual(reopened.staff_label(101), "Сотрудник (@test_staff)")

    def test_startup_race_does_not_create_a_missing_file(self):
        path = self.root / "guests.sqlite3"
        Store(path)
        real_connect = sqlite3.connect
        modes = []

        def removed_between_check_and_connect(database, *args, **kwargs):
            modes.append(database)
            path.unlink()
            return real_connect(database, *args, **kwargs)

        with patch("storage.sqlite3.connect", side_effect=removed_between_check_and_connect):
            with self.assertRaises(FileNotFoundError):
                Store(path, create=False)
        self.assertTrue(modes[0].endswith("?mode=rw"))
        self.assertFalse(path.exists())

    def test_all_later_operations_refuse_to_recreate_a_deleted_database(self):
        for creation in (True, False):
            for operation in ("read", "write", "backup"):
                with self.subTest(create=creation, operation=operation):
                    folder = self.root / f"{creation}-{operation}"
                    folder.mkdir()
                    path = folder / "guests.sqlite3"
                    Store(path)
                    store = Store(path, create=creation)
                    guest = self.add(store)
                    path.unlink()
                    with self.assertRaises(FileNotFoundError):
                        if operation == "read":
                            store.get_guest(guest.id)
                        elif operation == "write":
                            store.update_guest(guest.id, guest.version, 101, comment="Не сохранять")
                        else:
                            store.backup(folder / "copy.sqlite3")
                    self.assertFalse(path.exists())
                    self.assertFalse((folder / "copy.sqlite3").exists())

    def test_empty_file_is_not_initialized_or_changed(self):
        path = self.root / "empty.sqlite3"
        path.write_bytes(b"")
        with self.assertRaises(sqlite3.DatabaseError):
            Store(path, create=False)
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual({file.name for file in self.root.iterdir()}, {path.name})

    def test_non_sqlite_file_is_not_initialized_or_changed(self):
        path = self.root / "private-token-format.sqlite3"
        content = b"This is not a SQLite database.\x00\xff"
        path.write_bytes(content)
        with self.assertRaises(sqlite3.DatabaseError) as caught:
            Store(path, create=False)
        self.assertNotIn("private-token-format", str(caught.exception))
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual({file.name for file in self.root.iterdir()}, {path.name})

    def test_unrelated_database_receives_no_registry_schema(self):
        path = self.root / "unrelated.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES ('preserve')")
        before = path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            Store(path, create=False)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [("unrelated",)])

    def test_existing_legacy_registry_migration_backup_failure_leaves_data_intact(self):
        path = self.root / "legacy.sqlite3"
        original = Store(path)
        guest = self.add(original)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE visits")
        with patch.object(Store, "backup", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                Store(path, create=False)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT photo, version FROM guests WHERE id=?", (guest.id,)).fetchone(),
                             (guest.photo, guest.version))
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='visits'").fetchone())

    def test_create_flag_requires_an_actual_boolean(self):
        for flag in (None, 0, 1, "false"):
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError):
                    Store(self.root / "must-not-exist.sqlite3", create=flag)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
