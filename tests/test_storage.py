from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from storage import DuplicatePhoneError, StaleGuestError, Store, normalize_name, normalize_phone


class StorageTests(unittest.TestCase):
    def setUp(self):
        # Inherit Windows ACLs: Python 3.13 TemporaryDirectory applies a 0700
        # DACL that excludes some Windows sandbox tokens from their own files.
        temporary_base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = temporary_base / ("guest-storage-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "data" / "guests.sqlite3")

    def add(self, **overrides):
        fields = dict(name="Алёна Иванова", phone="8 (999) 123-45-67", comment="Постоянная гостья",
                      photo=b"test-photo-content", photo_file_id="telegram-photo-id", actor_id=1001)
        fields.update(overrides)
        return self.store.add_guest(**fields)

    def test_crud_survives_reopening_and_keeps_audit_fields(self):
        guest = self.add()
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_guest(guest.id), guest)
        self.assertEqual(guest.phone_key, "79991234567")
        changed = reopened.update_guest(guest.id, guest.version, 2002,
                                       name="Алёна Петрова", phone="+7 987 654 32 10",
                                       comment="Новый комментарий", photo=b"new-photo", photo_file_id="new-file")
        self.assertEqual(changed.version, 2)
        self.assertEqual(changed.created_by, 1001)
        self.assertEqual(changed.created_at, guest.created_at)
        self.assertEqual(changed.updated_by, 2002)
        self.assertGreaterEqual(changed.updated_at, guest.updated_at)
        self.assertEqual(reopened.find_phone("8-987-654-32-10"), changed)
        self.assertIsNone(reopened.find_phone(guest.phone))
        self.assertTrue(reopened.delete_guest(changed.id, changed.version))
        self.assertIsNone(Store(self.store.path).get_guest(guest.id))

    def test_normalization_and_cyrillic_partial_search(self):
        guest = self.add(name="  АлЁна\tИванова ")
        self.assertEqual(normalize_name("  АЛЁНА\nИВАНОВА  "), "алена иванова")
        self.assertEqual(normalize_phone("+7 (999) 123-45-67"), "79991234567")
        self.assertEqual(normalize_phone("８９９９１２３４５６７"), "79991234567")
        for query in ("АЛЕНА", "иван", "АлЁна   Ив", "123-45", "8 999 123 45 67"):
            with self.subTest(query=query):
                matches = self.store.search_guests(query)
                self.assertEqual([row.id for row in matches], [guest.id])
                self.assertEqual(matches[0].photo, b"")
                self.assertEqual(self.store.count_search(query), 1)
        self.assertEqual(self.store.search_guests("   "), [])
        self.assertEqual(self.store.count_search(""), 0)

    def test_wildcards_and_sql_are_literal(self):
        percent = self.add(name="Гость 50%", phone="79990000001")
        underscore = self.add(name="Гость_особый", phone="79990000002")
        backslash = self.add(name="Гость\\тест", phone="79990000003")
        self.add(name="Другой гость", phone="79990000004")
        for query, guest in (("%", percent), ("_", underscore), ("\\", backslash)):
            self.assertEqual([row.id for row in self.store.search_guests(query)], [guest.id])
            self.assertEqual(self.store.count_search(query), 1)
        self.assertEqual(self.store.search_guests("' OR 1=1 --"), [])

    def test_duplicate_phone_add_and_edit_are_atomic(self):
        first = self.add()
        with self.assertRaises(DuplicatePhoneError):
            self.add(phone="+7 999 123 45 67", actor_id=2002)
        second = self.add(name="Иван", phone="79990000002")
        with self.assertRaises(DuplicatePhoneError):
            self.store.update_guest(second.id, second.version, 2002, phone=first.phone, comment="Не сохранится")
        self.assertEqual(self.store.get_guest(second.id), second)
        self.assertEqual(self.store.count_search(""), 0)
        self.assertEqual(len(self.store.search_guests("999")), 2)

    def test_stale_edits_and_deletion_cannot_overwrite_other_staff(self):
        guest = self.add()
        changed = self.store.update_guest(guest.id, guest.version, 2002, comment="Внесено другим сотрудником")
        with self.assertRaises(StaleGuestError):
            self.store.update_guest(guest.id, guest.version, 3003, comment="Устаревшая карточка")
        with self.assertRaises(StaleGuestError):
            self.store.delete_guest(guest.id, guest.version)
        self.assertEqual(self.store.get_guest(guest.id), changed)
        self.store.delete_guest(changed.id, changed.version)
        with self.assertRaises(StaleGuestError):
            self.store.delete_guest(changed.id, changed.version)
        with self.assertRaises(StaleGuestError):
            self.store.update_guest(changed.id, changed.version, 3003, comment="После удаления")

    def test_concurrent_edit_only_one_staff_update_succeeds(self):
        guest = self.add()

        def edit(actor_id):
            try:
                return self.store.update_guest(guest.id, guest.version, actor_id, comment=str(actor_id))
            except StaleGuestError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(edit, [2002, 3003, 4004, 5005]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get_guest(guest.id).version, 2)

    def test_backup_contains_photos_and_remains_independent(self):
        guest = self.add()
        destination = self.root / "backups" / "guests.sqlite3"
        self.assertEqual(self.store.backup(destination), destination.resolve())
        restored = Store(destination)
        self.assertEqual(restored.get_guest(guest.id), guest)
        self.store.delete_guest(guest.id, guest.version)
        self.assertEqual(restored.get_guest(guest.id).photo, guest.photo)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(ValueError):
            self.store.backup(self.store.path)

    def test_pagination_has_stable_order_and_counts_all_matches(self):
        for index in range(12):
            self.add(name=f"Гость {index:02}", phone=f"799900000{index:02}")
        first = self.store.search_guests("ГОСТЬ", limit=8)
        second = self.store.search_guests("гость", limit=8, offset=8)
        self.assertEqual(self.store.count_search("гость"), 12)
        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 4)
        self.assertEqual([row.name for row in first + second], [f"Гость {i:02}" for i in range(12)])

    def test_invalid_values_do_not_create_or_modify_cards(self):
        for phone in ("123", "a79991234567", "7" * 16, ""):
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                self.add(phone=phone)
        for fields in ({"name": " "}, {"name": "а" * 101}, {"comment": "а" * 3001},
                       {"photo": b""}, {"photo": b"a" * (10 * 1024 * 1024 + 1)},
                       {"photo_file_id": ""}, {"actor_id": 0}):
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError):
                self.add(**fields)
        guest = self.add()
        with self.assertRaises(ValueError):
            self.store.update_guest(guest.id, guest.version, 1001, created_by=999)
        with self.assertRaises(ValueError):
            self.store.update_guest(guest.id, guest.version, 1001, name="")
        self.assertEqual(self.store.get_guest(guest.id), guest)
        with self.assertRaises(ValueError):
            self.store.search_guests("а" * 101)
        with self.assertRaises(ValueError):
            self.store.search_guests("guest", limit=0)


if __name__ == "__main__":
    unittest.main()
