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

from storage import DuplicatePhoneError, StaleGuestError, Store, VisitStateError


UTC = timezone.utc


class ActivityStorageTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = base / ("guest-activity-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "data" / "guests.sqlite3")
        self.start = datetime(2026, 10, 9, 10, 0, 0, 100_000, tzinfo=UTC)

    def add(self, **overrides):
        fields = dict(name="Алёна Иванова", phone="89991234567", comment="Заметка",
                      photo=b"private-photo-bytes", photo_file_id="private-telegram-photo-id", actor_id=1001)
        fields.update(overrides)
        return self.store.add_guest(**fields)

    def begin(self, guest, *, now=None, actor_id=2002):
        return self.store.start_visit(guest.id, self.store.get_guest(guest.id).version, actor_id,
                                      now=now if now is not None else self.start)

    def test_all_committed_operations_capture_actual_changes_and_survive_deletion(self):
        self.store.remember_staff(1001, "Первый сотрудник", "staff_one")
        guest = self.add()
        added = self.store.list_audit()[0]
        self.assertEqual((added.action, added.actor_id, added.guest_id, added.guest_name),
                         ("add", 1001, guest.id, guest.name))
        self.assertEqual(added.created_at, guest.created_at)
        self.assertEqual(added.changes["name"], {"before": None, "after": guest.name})
        changed = self.store.update_guest(guest.id, guest.version, 2002, name="Алёна Петрова",
                                          entry_status="closed", entry_reason="Конфликт",
                                          photo=b"new-photo-bytes", photo_file_id="new-private-file-id")
        event = self.store.list_audit(guest.id)[0]
        self.assertEqual(event.action, "update")
        self.assertEqual(event.guest_name, changed.name)
        self.assertEqual(event.created_at, changed.updated_at)
        self.assertEqual(set(event.changes), {"name", "entry_status", "entry_reason", "photo"})
        self.assertEqual(event.changes["entry_status"], {"before": "open", "after": "closed"})
        self.assertEqual(event.changes["entry_reason"], {"before": "", "after": "Конфликт"})
        visit = self.begin(changed)
        started = self.store.list_audit()[0]
        self.assertEqual((started.action, started.created_at), ("start", visit.started_at))
        self.assertEqual(started.changes["visit_id"]["after"], visit.id)
        stopped = self.store.stop_visit(guest.id, visit.id, 3003, now=self.start + timedelta(hours=1))
        stop_event = self.store.list_audit()[0]
        self.assertEqual((stop_event.action, stop_event.created_at), ("stop", stopped.stopped_at))
        self.assertEqual(stop_event.changes["stopped_at"]["after"], stopped.stopped_at)
        self.store.delete_guest(guest.id, self.store.get_guest(guest.id).version, actor_id=4004)
        reopened = Store(self.store.path)
        events = reopened.list_audit(guest.id, limit=100)
        self.assertEqual([event.action for event in events], ["delete", "stop", "start", "update", "add"])
        self.assertEqual(events[0].actor_id, 4004)
        self.assertEqual(events[0].changes["name"], {"before": changed.name, "after": None})
        self.assertEqual(reopened.count_audit(), 5)
        self.assertEqual(reopened.get_audit(added.id), added)
        self.assertIsNone(reopened.get_audit(9999))
        self.assertIsNone(reopened.get_guest(guest.id))
        self.assertEqual(reopened.list_visits(guest.id), [])
        with reopened._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            raw = " ".join(row[0] for row in connection.execute("SELECT changes FROM audit_events"))
        for secret in ("private-photo-bytes", "new-photo-bytes", "private-telegram-photo-id", "new-private-file-id"):
            self.assertNotIn(secret, raw)
        self.assertNotIn("photo_file_id", raw)

    def test_failed_and_stale_operations_create_no_events(self):
        guest = self.add()
        other = self.add(name="Другой", phone="79990000002")
        count = self.store.count_audit()
        with self.assertRaises(DuplicatePhoneError):
            self.add(actor_id=2002)
        with self.assertRaises(DuplicatePhoneError):
            self.store.update_guest(other.id, other.version, 2002, phone=guest.phone, comment="Не сохранится")
        for mutation in (
            lambda: self.store.update_guest(guest.id, 999, 2002, comment="Не сохранится"),
            lambda: self.store.delete_guest(guest.id, 999, 2002),
            lambda: self.store.start_visit(guest.id, 999, 2002, now=self.start),
        ):
            with self.assertRaises(StaleGuestError):
                mutation()
        self.assertEqual(self.store.count_audit(), count)
        self.assertEqual(self.store.get_guest(guest.id), guest)
        visit = self.begin(guest)
        count += 1
        with self.assertRaises(VisitStateError):
            self.begin(guest)
        with self.assertRaises(ValueError):
            self.store.stop_visit(guest.id, visit.id, 3003, now=self.start - timedelta(seconds=1))
        with self.assertRaises(VisitStateError):
            self.store.stop_visit(other.id, visit.id, 3003, now=self.start)
        self.assertEqual(self.store.count_audit(), count)
        self.assertIsNone(self.store.list_visits(guest.id)[0].stopped_at)

    def test_audit_write_failure_rolls_back_each_mutation_and_related_visits(self):
        guest = self.add()
        with patch.object(self.store, "_record_audit", side_effect=sqlite3.OperationalError("audit unavailable")):
            for mutation in (
                lambda: self.add(phone="79990000002"),
                lambda: self.store.update_guest(guest.id, guest.version, 2002, entry_status="closed"),
                lambda: self.store.delete_guest(guest.id, guest.version, 2002),
                lambda: self.begin(guest),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    mutation()
                self.assertEqual(self.store.get_guest(guest.id), guest)
                self.assertEqual(self.store.count_audit(), 1)
                self.assertEqual(self.store.list_visits(guest.id), [])
        visit = self.begin(guest)
        before_stop = self.store.get_guest(guest.id)
        with patch.object(self.store, "_record_audit", side_effect=sqlite3.OperationalError("audit unavailable")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.stop_visit(guest.id, visit.id, 3003, now=self.start + timedelta(minutes=10))
            with self.assertRaises(sqlite3.OperationalError):
                self.store.delete_guest(guest.id, before_stop.version, 4004)
        self.assertEqual(self.store.get_guest(guest.id), before_stop)
        self.assertEqual(self.store.list_visits(guest.id), [visit])
        self.assertEqual(self.store.count_audit(), 2)

    def test_audit_is_paginated_by_commit_id_with_independent_guest_filters(self):
        first = self.add()
        second = self.add(name="Второй", phone="79990000002")
        # Supplied clocks can be historical; new commits must still come first.
        for index in range(4):
            guest = first if index % 2 == 0 else second
            visit = self.begin(guest, now=self.start - timedelta(days=index))
            self.store.stop_visit(guest.id, visit.id, 3003, now=datetime.fromisoformat(visit.started_at))
        all_events = self.store.list_audit(limit=100)
        self.assertEqual(self.store.list_audit(limit=3), all_events[:3])
        self.assertEqual(self.store.list_audit(limit=3, offset=3), all_events[3:6])
        expected = [event for event in all_events if event.guest_id == first.id]
        self.assertEqual(self.store.count_audit(first.id), len(expected))
        self.assertEqual(self.store.list_audit(first.id, limit=2, offset=2), expected[2:4])
        self.assertEqual([event.id for event in all_events], sorted((event.id for event in all_events), reverse=True))

    def test_racing_edits_commit_one_change_and_one_audit(self):
        guest = self.add()
        barrier = Barrier(2)

        def edit(actor):
            barrier.wait()
            try:
                return self.store.update_guest(guest.id, guest.version, actor, entry_status="closed",
                                               entry_reason=f"Причина {actor}")
            except StaleGuestError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, [2002, 3003]))
        winner = next(result for result in results if result is not None)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.count_audit(guest.id), 2)
        event = self.store.list_audit(guest.id)[0]
        self.assertEqual(event.actor_id, winner.updated_by)
        self.assertEqual(event.changes["entry_reason"]["after"], winner.entry_reason)

    def test_legacy_delete_api_records_unknown_actor_and_noop_edit_is_explicit(self):
        guest = self.add()
        unchanged = self.store.update_guest(guest.id, guest.version, 2002, name="  Алёна Иванова ")
        self.assertEqual(self.store.list_audit()[0].changes, {})
        self.store.delete_guest(guest.id, unchanged.version)
        self.assertIsNone(self.store.list_audit()[0].actor_id)
        self.assertEqual(self.store.staff_label(None), "Сотрудник не указан")

    def test_staff_labels_persist_update_and_do_not_add_audit_events(self):
        self.assertEqual(self.store.staff_label(1001), "Telegram ID 1001")
        self.store.remember_staff(1001, "  Алёна\nИванова ", "@mihun")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.staff_label(1001), "Алёна Иванова (@mihun)")
        self.assertEqual(reopened.count_audit(), 0)
        reopened.remember_staff(1001, "Новое имя", None)
        self.assertEqual(self.store.staff_label(1001), "Новое имя")
        with self.store._connection() as connection:
            timestamp = connection.execute("SELECT updated_at FROM staff").fetchone()[0]
        reopened.remember_staff(1001, "Новое имя", None)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT updated_at FROM staff").fetchone()[0], timestamp)

    def test_overdue_visits_exact_24_hour_boundary_active_only_and_no_photo_bytes(self):
        old = self.add()
        old_visit = self.begin(old)
        fresh = self.add(name="Недавний", phone="79990000002")
        self.begin(fresh, now=self.start + timedelta(hours=23))
        stopped_guest = self.add(name="Ушёл", phone="79990000003")
        stopped_visit = self.begin(stopped_guest)
        self.store.stop_visit(stopped_guest.id, stopped_visit.id, 3003, now=self.start + timedelta(hours=1))
        self.assertEqual(self.store.overdue_visits(now=self.start + timedelta(days=1) - timedelta(microseconds=1)), [])
        due = self.store.overdue_visits(now=self.start + timedelta(days=1))
        self.assertEqual([(guest.id, visit.id) for guest, visit in due], [(old.id, old_visit.id)])
        self.assertEqual(due[0][0].photo, b"")
        self.assertEqual(due[0][0].name, old.name)
        self.assertEqual(self.store.get_guest(old.id).photo, old.photo)
        self.assertEqual(len(self.store.overdue_visits(now=self.start + timedelta(days=1), threshold_seconds=3600)), 2)
        self.assertEqual(self.store.overdue_visits(now=self.start - timedelta(hours=1)), [])

    def test_reminders_are_per_visit_recipient_durable_and_never_touch_guest_or_audit(self):
        guest = self.add()
        visit = self.begin(guest)
        card = self.store.get_guest(guest.id)
        events = self.store.list_audit()
        self.assertFalse(self.store.reminder_sent(visit.id, 1001))
        self.assertTrue(self.store.mark_reminder_sent(visit.id, 1001, now=self.start + timedelta(days=1)))
        self.assertFalse(self.store.mark_reminder_sent(visit.id, 1001, now=self.start + timedelta(days=2)))
        self.assertFalse(self.store.reminder_sent(visit.id, 2002))
        reopened = Store(self.store.path)
        self.assertTrue(reopened.reminder_sent(visit.id, 1001))
        self.assertTrue(reopened.mark_reminder_sent(visit.id, 2002, now=self.start + timedelta(days=1)))
        self.assertEqual(self.store.get_guest(guest.id), card)
        self.assertEqual(self.store.list_audit(), events)
        self.assertEqual(len(self.store.overdue_visits(now=self.start + timedelta(days=1))), 1)
        self.store.stop_visit(guest.id, visit.id, 3003, now=self.start + timedelta(days=2))
        self.assertFalse(self.store.mark_reminder_sent(visit.id, 3003))
        self.assertFalse(self.store.mark_reminder_sent(9999, 1001))
        next_visit = self.begin(guest, now=self.start + timedelta(days=3))
        self.assertFalse(self.store.reminder_sent(next_visit.id, 1001))
        self.assertTrue(self.store.mark_reminder_sent(next_visit.id, 1001))
        self.store.delete_guest(guest.id, self.store.get_guest(guest.id).version, 4004)
        self.assertFalse(self.store.reminder_sent(visit.id, 1001))
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visit_reminders").fetchone()[0], 0)

    def test_racing_reminder_marks_only_once(self):
        guest = self.add()
        visit = self.begin(guest)
        barrier = Barrier(2)

        def mark(_):
            barrier.wait()
            return self.store.mark_reminder_sent(visit.id, 1001, now=self.start + timedelta(days=1))

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(mark, [1, 2])), [False, True])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visit_reminders").fetchone()[0], 1)

    def previous_schema(self, filename="previous.sqlite3", *, partial=False):
        path = self.root / filename
        source = Store(path)
        guest = source.add_guest(name="Старая карточка", phone="79990000099", comment="Данные",
                                 photo=b"legacy-photo", photo_file_id="legacy-file-id", actor_id=1001,
                                 entry_status="closed", entry_reason="Старая причина")
        visit = source.start_visit(guest.id, guest.version, 2002, now=self.start)
        source.remember_staff(2002, "Старый сотрудник", "old_staff")
        source.mark_reminder_sent(visit.id, 2002)
        with closing(sqlite3.connect(path)) as connection, connection:
            before_guests = connection.execute("SELECT * FROM guests").fetchall()
            before_visits = connection.execute("SELECT * FROM visits").fetchall()
            connection.execute("DROP TABLE visit_reminders")
            if not partial:
                connection.execute("DROP TABLE staff")
                connection.execute("DROP TABLE audit_events")
        return path, before_guests, before_visits

    def assert_unchanged(self, path, guests, visits):
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM guests").fetchall(), guests)
            self.assertEqual(connection.execute("SELECT * FROM visits").fetchall(), visits)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_activity_upgrade_backs_up_once_and_keeps_all_previous_card_visit_fields(self):
        path, guests, visits = self.previous_schema()
        upgraded = Store(path)
        self.assert_unchanged(path, guests, visits)
        self.assertEqual(upgraded.count_audit(), 0)
        backups = list((self.root / "backups").glob("guests-before-activity-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assert_unchanged(backups[0], guests, visits)
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events'").fetchone())
        Store(path)
        self.assertEqual(list((self.root / "backups").glob("*.sqlite3")), backups)
        self.assertFalse((self.store.path.parent / "backups").exists())

    def test_partial_activity_upgrade_preserves_existing_history_and_staff(self):
        path, guests, visits = self.previous_schema(partial=True)
        upgraded = Store(path)
        self.assert_unchanged(path, guests, visits)
        self.assertEqual(upgraded.count_audit(), 2)
        self.assertEqual(upgraded.staff_label(2002), "Старый сотрудник (@old_staff)")
        self.assertFalse(upgraded.reminder_sent(1, 2002))
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 1)

    def test_backup_failure_aborts_activity_migration_without_schema_changes(self):
        path, guests, visits = self.previous_schema()
        with patch.object(Store, "backup", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                Store(path)
        self.assert_unchanged(path, guests, visits)
        with closing(sqlite3.connect(path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"audit_events", "staff", "visit_reminders"}.isdisjoint(names))

    def test_activity_schema_creation_failure_rolls_back_whole_migration(self):
        path, guests, visits = self.previous_schema()
        original_connection = Store._connection

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if "CREATE TABLE IF NOT EXISTS visit_reminders" in sql:
                    raise sqlite3.OperationalError("simulated final CREATE failure")
                return self.connection.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def instrumented(store):
            with original_connection(store) as connection:
                yield ConnectionProxy(connection)

        with patch.object(Store, "_connection", instrumented):
            with self.assertRaises(sqlite3.OperationalError):
                Store(path)
        self.assert_unchanged(path, guests, visits)
        with closing(sqlite3.connect(path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"audit_events", "staff", "visit_reminders"}.isdisjoint(names))

    def test_new_apis_reject_invalid_identifiers_pages_staff_and_clocks(self):
        for value in (True, 0, -1, "1"):
            for read in (lambda: self.store.list_audit(value), lambda: self.store.count_audit(value),
                         lambda: self.store.get_audit(value), lambda: self.store.staff_label(value),
                         lambda: self.store.reminder_sent(value, 1001),
                         lambda: self.store.mark_reminder_sent(1, value)):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    read()
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_audit(**kwargs)
        for name, username in (("", None), ("a" * 201, None), ("x\x00y", None),
                               ("Имя", "bad name"), ("Имя", 123), ("Имя", "x" * 65)):
            with self.subTest(name=name, username=username), self.assertRaises(ValueError):
                self.store.remember_staff(1001, name, username)
        for threshold in (0, -1, True, "86400"):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.store.overdue_visits(threshold_seconds=threshold)
        for read in (lambda: self.store.overdue_visits(now=datetime(2026, 10, 9)),
                     lambda: self.store.mark_reminder_sent(1, 1001, now=datetime(2026, 10, 9))):
            with self.assertRaises(ValueError):
                read()
        guest = self.add()
        with self.assertRaises(ValueError):
            self.store.delete_guest(guest.id, guest.version, True)
        self.assertEqual(self.store.get_guest(guest.id), guest)


if __name__ == "__main__":
    unittest.main()
