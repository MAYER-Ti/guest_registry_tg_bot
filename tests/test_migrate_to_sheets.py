"""An offline, explicit migration never mutates or silently truncates its source."""
from __future__ import annotations

import base64
from contextlib import closing, redirect_stderr, redirect_stdout
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from config import Config
from migrate_to_sheets import ADMIN_ID, MigrationError, main, migrate, read_source, verify_import
from sheets_store import SheetsStorageError
from storage import AuditEvent, EmployeeAccess, Store


NOW = datetime(2026, 11, 6, 12, 34, 56, 789123, tzinfo=timezone.utc)
OWNER = "owner@example.com"
SECRET = "fake-secret-for-offline-tests-" + "x" * 40


class FakeDestination:
    """The normal read API is independent of the source and import argument."""

    def __init__(self):
        self.state = None
        self.sequences = None
        self.imports = 0
        self.staged_photos = {}
        self.calls = []
        self.import_payload = None

    def stage_photo(self, photo, *, admin_id):
        if admin_id != ADMIN_ID or not isinstance(photo, bytes):
            raise AssertionError("Unexpected photo upload")
        reference = f"staged-photo-{len(self.staged_photos) + 1}"
        self.calls.append(("stage_photo", reference))
        self.staged_photos[reference] = bytes(photo)
        return {"photo_ref": reference, "bytes": len(photo)}

    def import_state(self, state, *, admin_id, sequences=None):
        if self.state is not None:
            raise SheetsStorageError("Destination is not empty")
        if admin_id != ADMIN_ID:
            raise AssertionError("Unexpected administrator")
        self.imports += 1
        self.calls.append(("import_state", None))
        self.import_payload = copy.deepcopy(state)
        self.state = copy.deepcopy(state)
        for row in self.state["guests"]:
            if "photo_ref" in row:
                reference = row.pop("photo_ref")
                row["photo"] = base64.b64encode(self.staged_photos[reference]).decode("ascii")
        self.sequences = copy.deepcopy(sequences)
        return {"imported": True, "guests": len(state["guests"]),
                "visits": len(state["visits"]), "employees": len(state["employee_access"]),
                "audit_events": len(state["audit_events"])}

    def snapshot(self, *, include_photos=False):
        if not include_photos:
            raise AssertionError("Verification must include original photographs")
        return {"generated_at": NOW.isoformat(), "guests": copy.deepcopy(self.state["guests"]),
                "visits": copy.deepcopy(self.state["visits"])}

    def list_audit(self, guest_id=None, limit=5, offset=0):
        if guest_id is not None:
            raise AssertionError("Deleted guest history must also be checked")
        rows = sorted(self.state["audit_events"], key=lambda row: row["id"], reverse=True)
        return [AuditEvent(**copy.deepcopy(row)) for row in rows[offset:offset + limit]]

    def count_audit(self):
        return len(self.state["audit_events"])

    def list_employees(self, limit=8, offset=0):
        rows = sorted(self.state["employee_access"], key=lambda row: row["user_id"])
        return [EmployeeAccess(**row) for row in rows[offset:offset + limit]]

    def count_employees(self):
        return len(self.state["employee_access"])

    def staff_label(self, actor_id):
        row = next(row for row in self.state["staff"] if row["actor_id"] == actor_id)
        return f'{row["name"]} (@{row["username"]})' if row["username"] else row["name"]

    def reminder_sent(self, visit_id, recipient_id):
        return any(row["visit_id"] == visit_id and row["recipient_id"] == recipient_id
                   for row in self.state["visit_reminders"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        base = (Path.cwd() / ".test-runtime").resolve()
        self.root = (base / ("migrate-sheets-" + uuid4().hex)).resolve()
        self.assertEqual(self.root.parent, base)
        self.root.mkdir(parents=True, mode=0o777)
        self.addCleanup(shutil.rmtree, self.root)
        self.path = self.root / "Копия гостей # 1 % ?.sqlite3".replace("?", "unicode")
        self.config = Config(token="123:" + "x" * 30, allowed_user_ids=frozenset({ADMIN_ID}),
                             db_path=self.root / "never-created.sqlite3", storage_backend="google_sheets",
                             sheets_endpoint="https://script.google.com/macros/s/fake-deployment/exec",
                             sheets_secret=SECRET, google_owner_email=OWNER)

    def add(self, store, suffix=1, **fields):
        return store.add_guest(name=f"Тестовый гость {suffix}", phone=f"+799900000{suffix:02d}",
                               comment="Секретный комментарий: не выводить в отчёт",
                               photo=b"original-photo-" + bytes([suffix]) + b"\x00\xff",
                               photo_file_id=f"telegram-file-{suffix}", actor_id=101, **fields)

    def source(self, *, empty=False):
        store = Store(self.path)
        store.bootstrap_employee_access({ADMIN_ID, 101, 102, 103}, ADMIN_ID)
        for actor, name, username in ((ADMIN_ID, "Администратор", "admin_test"),
                                      (101, "Первый сотрудник", "staff_test"),
                                      (102, "Второй сотрудник", "")):
            store.remember_staff(actor, name, username)
        if not empty:
            first = self.add(store, 1, entry_status="closed", entry_reason="Тестовая причина")
            first = store.update_guest(first.id, first.version, 102, comment="Обновлённый комментарий")
            visit = store.start_visit(first.id, first.version, 101, now=NOW)
            store.mark_reminder_sent(visit.id, 102, now=NOW + timedelta(days=1))
            store.stop_visit(first.id, visit.id, 102, now=NOW + timedelta(days=1, minutes=5))
            second = self.add(store, 2)
            store.start_visit(second.id, second.version, 102, now=NOW + timedelta(hours=1))
            deleted = self.add(store, 3)
            store.delete_guest(deleted.id, deleted.version, 101)
            # Deleted identities must never be reused for old inline buttons.
            with closing(sqlite3.connect(self.path)) as db, db:
                for table, sequence in (("guests", 99), ("visits", 88), ("audit_events", 77)):
                    db.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (sequence, table))
        # This creates an offline copy, as the production procedure requires.
        copy_path = self.root / "offline.sqlite3"
        store.backup(copy_path)
        self.path = copy_path
        return store

    def fingerprint(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def assert_source_unchanged(self, before, names):
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual({path.name for path in self.root.iterdir()}, names)
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())

    def test_complete_import_preserves_photos_versions_every_section_and_allocated_ids(self):
        original = self.source()
        before = self.fingerprint()
        names = {path.name for path in self.root.iterdir()}
        destination = FakeDestination()
        factory = Mock(return_value=destination)
        result = migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_called_once_with(self.config.sheets_endpoint, SECRET,
                                        expected_owner_email=OWNER, expected_admin_id=ADMIN_ID)
        self.assertEqual(result["mode"], "execute")
        self.assertTrue(result["verified"])
        self.assertEqual(result["counts"], {"guests": 2, "visits": 2, "audit_events": 8,
                                            "staff": 3, "employee_access": 3, "visit_reminders": 1})
        self.assertEqual(result["source_sha256"], before)
        self.assertEqual(destination.imports, 1)
        self.assertEqual([call[0] for call in destination.calls], ["stage_photo", "stage_photo", "import_state"])
        self.assertTrue(all("photo" not in row and "photo_ref" in row for row in destination.import_payload["guests"]))
        self.assertEqual(destination.sequences, {"guests": 99, "visits": 88, "audit_events": 77})
        for row in destination.state["guests"]:
            guest = original.get_guest(row["id"])
            expected = asdict(guest)
            expected["photo"] = base64.b64encode(guest.photo).decode("ascii")
            self.assertEqual(row, expected)
        self.assertTrue(any(row["guest_id"] == 3 for row in destination.state["audit_events"]))
        self.assertEqual(destination.state["visits"][0]["started_at"], NOW.isoformat())
        self.assertEqual(destination.state["visit_reminders"][0]["sent_at"], (NOW + timedelta(days=1)).isoformat())
        self.assert_source_unchanged(before, names)

    def test_staging_failure_does_not_import_any_metadata_or_change_source(self):
        self.source()
        before = self.fingerprint()
        destination = FakeDestination()
        original_stage = destination.stage_photo

        def second_photo_fails(photo, *, admin_id):
            if destination.staged_photos:
                raise SheetsStorageError("Upload failed")
            return original_stage(photo, admin_id=admin_id)

        destination.stage_photo = Mock(side_effect=second_photo_fails)
        destination.import_state = Mock(wraps=destination.import_state)
        with self.assertRaises(SheetsStorageError):
            migrate(self.path, 2, execute=True, config=self.config,
                    store_factory=Mock(return_value=destination))
        destination.import_state.assert_not_called()
        self.assertEqual(destination.stage_photo.call_count, 2)
        self.assertEqual(len(destination.staged_photos), 1)
        self.assertIsNone(destination.state)
        self.assertEqual(self.fingerprint(), before)

    def test_aggregate_photo_size_can_exceed_one_rpc_without_combining_originals(self):
        self.source()
        with closing(sqlite3.connect(self.path)) as db, db:
            for guest_id in (1, 2):
                db.execute("UPDATE guests SET photo=? WHERE id=?", (bytes([guest_id]) * (7 * 1024 * 1024), guest_id))
        before = self.fingerprint()
        destination = FakeDestination()
        result = migrate(self.path, 2, execute=True, config=self.config,
                         store_factory=Mock(return_value=destination))
        self.assertTrue(result["verified"])
        self.assertEqual([len(photo) for photo in destination.staged_photos.values()], [7 * 1024 * 1024] * 2)
        self.assertGreater(sum(len(row["photo"]) for row in destination.state["guests"]), 16 * 1024 * 1024)
        self.assertLess(len(json.dumps(destination.import_payload).encode("utf-8")), 16 * 1024 * 1024)
        self.assertEqual([action for action, _ in destination.calls], ["stage_photo", "stage_photo", "import_state"])
        self.assertEqual(self.fingerprint(), before)

    def test_staging_keeps_original_snapshot_for_strict_photo_verification(self):
        self.source()
        snapshot = read_source(self.path, 2)
        original = copy.deepcopy(snapshot.state)
        destination = FakeDestination()
        with patch("migrate_to_sheets.read_source", return_value=snapshot):
            migrate(self.path, 2, execute=True, config=self.config,
                    store_factory=Mock(return_value=destination))
        self.assertEqual(snapshot.state, original)
        self.assertEqual(destination.state, original)
        self.assertTrue(all("photo_ref" not in row for row in snapshot.state["guests"]))

    def test_metadata_request_bound_is_checked_before_first_remote_call(self):
        self.source()
        before = self.fingerprint()
        factory = Mock()
        with patch("migrate_to_sheets.MAX_IMPORT_REQUEST_BYTES", 100):
            with self.assertRaisesRegex(MigrationError, "Метаданные"):
                migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.fingerprint(), before)

    def test_oversized_sheet_cell_is_rejected_before_staging_any_photo(self):
        self.source()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE audit_events SET changes=? WHERE id=1",
                       (json.dumps({"legacy_field": {"before": None, "after": "x" * 49000}}),))
        factory = Mock()
        with self.assertRaisesRegex(MigrationError, "ячейки"):
            migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()

    def test_dry_run_is_local_without_even_loading_telegram_or_google_config(self):
        self.source()
        before = self.fingerprint()
        names = {path.name for path in self.root.iterdir()}
        with patch("migrate_to_sheets.load_config", side_effect=AssertionError("Must remain offline")), \
                patch("migrate_to_sheets.SheetsStore", side_effect=AssertionError("No remote calls")):
            report = migrate(self.path, 2)
        self.assertEqual(report["mode"], "dry_run")
        self.assertNotIn("verified", report)
        self.assertEqual(report["counts"]["employee_access"], 3)
        output = json.dumps(report, ensure_ascii=False)
        for private in (str(self.path), "Тестовый гость", "799900000", "комментарий", "telegram-file", SECRET, OWNER):
            self.assertNotIn(private, output)
        self.assert_source_unchanged(before, names)

    def test_explicit_zero_guests_keeps_all_three_employee_grants_and_profiles(self):
        self.source(empty=True)
        destination = FakeDestination()
        report = migrate(self.path, 0, execute=True, config=self.config,
                         store_factory=Mock(return_value=destination))
        self.assertEqual(report["counts"]["guests"], 0)
        self.assertEqual({row["user_id"] for row in destination.state["employee_access"]}, {101, 102, 103})
        self.assertEqual({row["actor_id"] for row in destination.state["staff"]}, {ADMIN_ID, 101, 102})
        self.assertTrue(report["verified"])

    def test_guest_count_mismatch_blocks_all_remote_access_and_preserves_source(self):
        self.source()
        before = self.fingerprint()
        factory = Mock()
        with self.assertRaisesRegex(MigrationError, "ожидалось 0, найдено 2"):
            migrate(self.path, 0, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.fingerprint(), before)

    def test_mismatched_source_administrator_blocks_import(self):
        self.source()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE access_settings SET admin_id=999")
        before = self.fingerprint()
        factory = Mock()
        with self.assertRaisesRegex(MigrationError, "Администратор"):
            migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.fingerprint(), before)

    def test_nonempty_destination_conflict_is_not_overwritten_or_retried(self):
        self.source()
        before = self.fingerprint()
        destination = FakeDestination()
        destination.state = {"keep": "existing destination data"}
        with self.assertRaises(SheetsStorageError):
            migrate(self.path, 2, execute=True, config=self.config,
                    store_factory=Mock(return_value=destination))
        self.assertEqual(destination.state, {"keep": "existing destination data"})
        self.assertEqual(destination.imports, 0)
        self.assertEqual(self.fingerprint(), before)

    def test_orphan_visit_rejects_entire_import_without_remote_calls(self):
        self.source()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE visits SET guest_id=999 WHERE id=1")
        before = self.fingerprint()
        factory = Mock()
        with self.assertRaises(MigrationError):
            migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.fingerprint(), before)

    def test_orphan_reminder_is_rejected_even_without_declared_foreign_keys(self):
        self.source()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE visit_reminders RENAME TO old_reminders")
            db.execute("CREATE TABLE visit_reminders (visit_id INTEGER, recipient_id INTEGER, sent_at TEXT)")
            db.execute("INSERT INTO visit_reminders VALUES (999, 101, ?)", (NOW.isoformat(),))
        with self.assertRaises(MigrationError):
            read_source(self.path, 2)

    def test_source_with_pending_wal_requires_completed_backup(self):
        self.source()
        before = self.fingerprint()
        pending = Path(str(self.path) + "-wal")
        pending.write_bytes(b"uncheckpointed-records")
        with self.assertRaisesRegex(MigrationError, "WAL"):
            read_source(self.path, 2)
        self.assertEqual(pending.read_bytes(), b"uncheckpointed-records")
        self.assertEqual(self.fingerprint(), before)

    def test_changed_source_is_rejected_without_touching_google(self):
        self.source()
        factory = Mock()
        with patch("migrate_to_sheets._hash_file", side_effect=["old", "new"]):
            with self.assertRaisesRegex(MigrationError, "изменилась"):
                migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()

    def test_size_bound_fails_complete_import_before_any_rpc(self):
        self.source()
        before = self.fingerprint()
        factory = Mock()
        with patch("migrate_to_sheets.MAX_STATE_BYTES", 100):
            with self.assertRaisesRegex(MigrationError, "не обрезаны"):
                migrate(self.path, 2, execute=True, config=self.config, store_factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.fingerprint(), before)

    def test_legacy_guest_columns_default_without_upgrading_the_database(self):
        self.source(empty=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE guests")
            db.execute("CREATE TABLE guests (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, phone_key TEXT, "
                       "comment TEXT, photo BLOB, photo_file_id TEXT, created_by INTEGER, updated_by INTEGER, "
                       "created_at TEXT, updated_at TEXT, version INTEGER)")
            db.execute("INSERT INTO guests VALUES (42, 'Старый гость', '+79990000001', '79990000001', "
                       "'Сохранить', ?, 'old-file', 101, 102, ?, ?, 7)",
                       (b"original-old-photo", NOW.isoformat(), NOW.isoformat()))
            for table in ("audit_events", "visit_reminders", "visits", "employee_access", "access_settings", "staff"):
                db.execute(f"DROP TABLE {table}")
        before = self.fingerprint()
        state = read_source(self.path, 1).state
        self.assertEqual(state["guests"][0]["entry_status"], "open")
        self.assertEqual(state["guests"][0]["entry_reason"], "")
        self.assertEqual(state["guests"][0]["id"], 42)
        self.assertEqual(state["guests"][0]["version"], 7)
        self.assertTrue(all(not rows for section, rows in state.items() if section != "guests"))
        self.assertEqual(self.fingerprint(), before)
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(guests)")}
            self.assertNotIn("entry_status", columns)

    def test_missing_or_invalid_source_creates_nothing_and_hides_its_path(self):
        missing = self.root / "private-sensitive-directory" / "unknown.sqlite3"
        with self.assertRaises(MigrationError) as caught:
            read_source(missing, 0)
        self.assertNotIn("private-sensitive-directory", str(caught.exception))
        self.assertFalse(missing.parent.exists())
        self.path.write_bytes(b"not SQLite and should not be changed")
        before = self.fingerprint()
        with self.assertRaises(MigrationError):
            read_source(self.path, 0)
        self.assertEqual(self.fingerprint(), before)

    def test_execute_requires_google_configured_owner_and_correct_admin(self):
        self.source()
        for settings in ({"storage_backend": "sqlite"}, {"google_owner_email": ""}, {"admin_user_id": 999}):
            with self.subTest(settings=settings):
                factory = Mock()
                with self.assertRaises(MigrationError):
                    migrate(self.path, 2, execute=True, config=replace(self.config, **settings), store_factory=factory)
                factory.assert_not_called()

    def test_execute_flag_must_be_an_actual_boolean(self):
        for value in (None, 1, "yes"):
            with self.subTest(value=value):
                with self.assertRaises(MigrationError):
                    migrate(self.path, 0, execute=value)
        self.assertFalse(self.path.exists())

    def test_post_import_verification_detects_each_incomplete_section(self):
        self.source()
        source = read_source(self.path, 2)
        for section in ("guests", "visits", "audit_events", "employee_access", "staff", "visit_reminders"):
            with self.subTest(section=section):
                destination = FakeDestination()
                destination.import_state(source.state, admin_id=ADMIN_ID)
                if section == "staff":
                    destination.staff_label = Mock(return_value="wrong staff profile")
                else:
                    destination.state[section].pop()
                with self.assertRaises(MigrationError):
                    verify_import(destination, source)

    def test_verification_uses_all_pages_and_includes_deleted_guest_history(self):
        self.source()
        source = read_source(self.path, 2)
        state = copy.deepcopy(source.state)
        template = copy.deepcopy(state["audit_events"][0])
        state["audit_events"] = [{**template, "id": index, "guest_id": 777} for index in range(1, 206)]
        state["employee_access"] = [{"user_id": index, "added_by": ADMIN_ID, "added_at": NOW.isoformat()}
                                    for index in range(1, 206)]
        source = replace(source, state=state)
        destination = FakeDestination()
        destination.import_state(state, admin_id=ADMIN_ID)
        destination.list_audit = Mock(wraps=destination.list_audit)
        destination.list_employees = Mock(wraps=destination.list_employees)
        verify_import(destination, source)
        self.assertEqual([call.args for call in destination.list_audit.call_args_list],
                         [(None, 100, 0), (None, 100, 100), (None, 100, 200)])
        self.assertEqual([call.args for call in destination.list_employees.call_args_list],
                         [(100, 0), (100, 100), (100, 200)])

    def test_cli_requires_explicit_expected_guest_count(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--source", str(self.path)])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_defaults_to_safe_count_report_and_requires_execute_for_writes(self):
        self.source()
        out = StringIO()
        with redirect_stdout(out), patch("migrate_to_sheets.SheetsStore") as factory:
            self.assertEqual(main(["--source", str(self.path), "--expected-guests", "2"]), 0)
        factory.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["mode"], "dry_run")
        destination = FakeDestination()
        with redirect_stdout(StringIO()), patch("migrate_to_sheets.load_config", return_value=self.config), \
                patch("migrate_to_sheets.SheetsStore", return_value=destination) as factory:
            self.assertEqual(main(["--source", str(self.path), "--expected-guests", "2", "--execute"]), 0)
        factory.assert_called_once()
        self.assertEqual(destination.imports, 1)

    def test_cli_error_contains_no_source_path_credentials_or_guest_values(self):
        self.source()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                patch("migrate_to_sheets.load_config", side_effect=RuntimeError(f"{self.path} {SECRET} {OWNER}")):
            self.assertEqual(main(["--source", str(self.path), "--expected-guests", "2", "--execute"]), 1)
        self.assertEqual(out.getvalue(), "")
        for private in (str(self.path), SECRET, OWNER, "Тестовый гость", "799900000"):
            self.assertNotIn(private, err.getvalue())


if __name__ == "__main__":
    unittest.main()
