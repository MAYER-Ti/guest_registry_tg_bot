"""Owner-only grants, numeric IDs and permission persistence."""
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock
from uuid import uuid4

from access import AccessPolicy, MAX_TELEGRAM_USER_ID, parse_employee_id
from config import Config
from storage import Store


ADMIN = 7011639945


class ParseEmployeeTests(unittest.TestCase):
    def test_numeric_ascii_ids_with_surrounding_whitespace(self):
        for text, expected in (("101", 101), (" 7011639945\n", ADMIN),
                               ("000101", 101), (str(MAX_TELEGRAM_USER_ID), MAX_TELEGRAM_USER_ID)):
            with self.subTest(text=text):
                self.assertEqual(parse_employee_id(text), expected)

    def test_non_numeric_zero_negative_phone_username_and_overflow_fail_safely(self):
        invalid = (None, 101, True, "", " ", "0", "0000", "-101", "+101", "1.0",
                   "1e3", "@worker_private_name", "+7 (999) 123-45-67", "101 102",
                   "١٠١", "１２３", "²", "101\x00", "1\n01", "9" * 10000,
                   str(MAX_TELEGRAM_USER_ID + 1))
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as raised:
                    parse_employee_id(value)
                self.assertNotIn("worker_private_name", str(raised.exception))


class AccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.addCleanup(shutil.rmtree, self.runtime)
        self.store = Store(self.runtime / "guests.sqlite3")
        self.config = Config(token="test", allowed_user_ids=frozenset({101, 102, ADMIN}),
                             db_path=self.store.path)
        self.access = AccessPolicy(self.config, self.store)

    def test_owner_is_authorized_but_not_an_employee_and_legacy_ids_survive(self):
        self.assertTrue(self.access.is_admin(ADMIN))
        self.assertTrue(self.access.allows(ADMIN))
        self.assertFalse(self.store.has_employee_access(ADMIN))
        self.assertEqual(self.access.allowed_ids(), frozenset({ADMIN, 101, 102}))
        self.assertFalse(self.access.is_admin(101))
        self.assertTrue(self.access.allows(101))
        self.assertFalse(self.access.allows(999))

    def test_only_owner_can_grant_and_employee_cannot_delegate(self):
        self.assertTrue(self.access.add_employee(ADMIN, 103))
        self.assertTrue(self.access.allows(103))
        self.assertFalse(self.access.is_admin(103))
        with self.assertRaises(PermissionError):
            self.access.add_employee(103, 104)
        self.assertFalse(self.access.allows(104))
        self.assertFalse(self.access.add_employee(ADMIN, 103))
        self.assertEqual(self.access.allowed_ids(), frozenset({ADMIN, 101, 102, 103}))

    def test_permissions_persist_across_restart_without_new_environment_members(self):
        self.access.add_employee(ADMIN, 103)
        reopened = Store(self.store.path)
        changed_config = Config(token="test", allowed_user_ids=frozenset({999}),
                                db_path=reopened.path)
        access = AccessPolicy(changed_config, reopened)
        self.assertTrue(access.allows(103))
        self.assertTrue(access.allows(101))
        self.assertFalse(access.allows(999))
        self.assertTrue(access.allows(ADMIN))

    def test_profile_names_do_not_grant_permissions(self):
        self.store.remember_staff(999, "Administrator", "admin")
        self.assertFalse(self.access.allows(999))
        self.assertFalse(self.access.is_admin(999))
        with self.assertRaises(PermissionError):
            self.access.add_employee(999, 104)

    def test_admin_cannot_be_added_as_employee_or_replaced_on_existing_database(self):
        with self.assertRaises(ValueError):
            self.access.add_employee(ADMIN, ADMIN)
        replaced = Config(token="test", allowed_user_ids=frozenset({101}),
                          db_path=self.store.path, admin_user_id=101)
        with self.assertRaises(ValueError):
            AccessPolicy(replaced, self.store)
        self.assertTrue(self.access.is_admin(ADMIN))
        self.assertFalse(self.access.is_admin(101))

    def test_allows_invalid_id_never_reads_database_and_cannot_equal_bool_admin(self):
        proxy = Mock(wraps=self.store)
        access = AccessPolicy(self.config, proxy)
        proxy.reset_mock()
        for value in (None, True, False, 1.0, "101", 0, -101, MAX_TELEGRAM_USER_ID + 1):
            with self.subTest(value=value):
                self.assertFalse(access.allows(value))
                self.assertFalse(access.is_admin(value))
        proxy.has_employee_access.assert_not_called()

    def test_unauthorized_actor_is_rejected_before_target_validation_or_db_write(self):
        proxy = Mock(wraps=self.store)
        access = AccessPolicy(self.config, proxy)
        proxy.reset_mock()
        for actor in (101, 999, str(ADMIN), True):
            with self.subTest(actor=actor):
                with self.assertRaises(PermissionError):
                    access.add_employee(actor, object())
        proxy.add_employee.assert_not_called()

    def test_invalid_target_never_reaches_store(self):
        proxy = Mock(wraps=self.store)
        access = AccessPolicy(self.config, proxy)
        proxy.reset_mock()
        for value in (None, True, "103", 0, -103, 1.5, MAX_TELEGRAM_USER_ID + 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    access.add_employee(ADMIN, value)
        proxy.add_employee.assert_not_called()

    def test_owner_always_authorized_without_database_lookup(self):
        proxy = Mock(wraps=self.store)
        access = AccessPolicy(self.config, proxy)
        proxy.reset_mock()
        self.assertTrue(access.allows(ADMIN))
        proxy.has_employee_access.assert_not_called()

    def test_allowed_ids_returns_a_fresh_snapshot_with_dynamic_employee(self):
        before = self.access.allowed_ids()
        self.access.add_employee(ADMIN, 103)
        self.assertNotIn(103, before)
        self.assertIn(103, self.access.allowed_ids())


if __name__ == "__main__":
    unittest.main()
