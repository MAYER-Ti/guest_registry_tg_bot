"""Opt-in SQL tests, never activated by a production DATABASE_URL.

Use a dedicated *empty database* named guest_registry_test_<suffix>, then set:
  TEST_DATABASE_URL, TEST_DATABASE_DISPOSABLE=1
Alternatively, the one explicitly disposable, empty Supabase project is allowed
only with both exact TEST_EMPTY_SUPABASE_PROJECT_REF and
TEST_EMPTY_SUPABASE_PROJECT_ACK pins. Its pooler username or direct hostname
must bind the connection to that project, and a read-only preflight must find
no application objects and no guest_registry namespace before initialization.
Optional TEST_DATABASE_SSLMODE (require/verify-full), TEST_DATABASE_SSLROOTCERT.
The suite refuses a pre-existing guest_registry namespace. It only drops the
namespace it created after verifying its random identity pin during cleanup.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

from postgres_store import (
    DATA_TABLES, PostgresStore, PostgresStorageError, STATE_KEYS, _options, initialize_schema,
)
from storage import DuplicatePhoneError, StaleGuestError, VisitStateError


ADMIN = 7011639945
STAMP = "2026-11-09T15:16:17.987654+03:00"
DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
ENABLED = bool(DATABASE_URL) and os.environ.get("TEST_DATABASE_DISPOSABLE") == "1"
_EMPTY_SUPABASE_PROJECT_REF = "sluphajmvixtloasbajo"
_BUILTIN_SCHEMAS = (
    "information_schema", "auth", "storage", "realtime", "_realtime",
    "supabase_functions", "supabase_migrations", "extensions", "graphql",
    "graphql_public", "vault", "net", "cron", "pgsodium", "pgsodium_masks",
)


def _disposable_target(parameters, environ):
    """Return local/test-project mode only for an explicitly pinned target.

    The project exception is deliberately specific to the fresh project root
    authorized for initial verification. It cannot activate on another project
    merely by supplying that project's ref, and does not authorize later runs
    after real application tables have been imported.
    """
    if environ.get("TEST_DATABASE_DISPOSABLE") != "1":
        raise RuntimeError("Live tests require explicit disposable-database authorization.")
    if parameters.get("dbname", "").startswith("guest_registry_test_"):
        return "test_database"
    project_ref = environ.get("TEST_EMPTY_SUPABASE_PROJECT_REF")
    if (project_ref != _EMPTY_SUPABASE_PROJECT_REF
            or environ.get("TEST_EMPTY_SUPABASE_PROJECT_ACK") != _EMPTY_SUPABASE_PROJECT_REF
            or parameters.get("dbname") != "postgres" or parameters.get("hostaddr")):
        raise RuntimeError("Live tests require a dedicated test database or the explicitly pinned empty project.")
    host = parameters.get("host", "").casefold()
    user = parameters.get("user", "")
    direct = host == f"db.{project_ref}.supabase.co" and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", user))
    pooler = (re.fullmatch(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com", host)
              and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\." + re.escape(project_ref), user))
    if not (direct or pooler) or parameters.get("port", "5432") not in {"5432", "6543"}:
        raise RuntimeError("Disposable Supabase test connection does not match the authorized project.")
    return "empty_supabase_project"


def _assert_empty_project(connection):
    """No mutation: reject an existing registry or any application relation."""
    if connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname='guest_registry'").fetchone() is not None:
        raise RuntimeError("Disposable project already has a registry; live tests refused.")
    objects = connection.execute("""SELECT n.nspname, c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm', 'S')
        AND n.nspname !~ '^pg_'
        AND n.nspname <> ALL(%s::text[]) LIMIT 1""", (list(_BUILTIN_SCHEMAS),)).fetchone()
    if objects is not None:
        raise RuntimeError("Disposable project contains application objects; live tests refused.")


class DisposableGuardTests(unittest.TestCase):
    def setUp(self):
        self.parameters = dict(dbname="postgres", host="aws-1-eu-north-1.pooler.supabase.com",
                               user="guest_migration." + _EMPTY_SUPABASE_PROJECT_REF, port="6543")
        self.environ = dict(TEST_DATABASE_DISPOSABLE="1",
                            TEST_EMPTY_SUPABASE_PROJECT_REF=_EMPTY_SUPABASE_PROJECT_REF,
                            TEST_EMPTY_SUPABASE_PROJECT_ACK=_EMPTY_SUPABASE_PROJECT_REF)

    def test_existing_named_test_database_route_is_preserved(self):
        self.assertEqual(_disposable_target(dict(dbname="guest_registry_test_123"),
                                            dict(TEST_DATABASE_DISPOSABLE="1")), "test_database")

    def test_only_exact_ack_and_project_pins_allow_new_project(self):
        self.assertEqual(_disposable_target(self.parameters, self.environ), "empty_supabase_project")
        direct = {**self.parameters, "host": f"db.{_EMPTY_SUPABASE_PROJECT_REF}.supabase.co", "user": "guest_migration", "port": "5432"}
        self.assertEqual(_disposable_target(direct, self.environ), "empty_supabase_project")
        for field in ("TEST_DATABASE_DISPOSABLE", "TEST_EMPTY_SUPABASE_PROJECT_REF", "TEST_EMPTY_SUPABASE_PROJECT_ACK"):
            for value in (None, "", "another-project"):
                environment = {**self.environ, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                    _disposable_target(self.parameters, environment)

    def test_host_tenant_override_and_wrong_username_are_rejected(self):
        for field, value in (("host", "example.invalid"),
                             ("host", "aws-1-eu-north-1.pooler.supabase.com.example.invalid"),
                             ("user", "guest_migration.someotherproject"),
                             ("user", "guest_migration." + _EMPTY_SUPABASE_PROJECT_REF + "suffix"),
                             ("hostaddr", "127.0.0.1"), ("dbname", "production"), ("port", "9999")):
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                _disposable_target({**self.parameters, field: value}, self.environ)

    def test_preflight_refuses_existing_schema_or_application_objects(self):
        def connection(responses):
            iterator = iter(responses)
            return SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: next(iterator)))
        _assert_empty_project(connection([None, None]))
        with self.assertRaises(RuntimeError):
            _assert_empty_project(connection([{"exists": 1}]))
        with self.assertRaises(RuntimeError):
            _assert_empty_project(connection([None, {"nspname": "public", "relname": "real_customers"}]))


@unittest.skipUnless(ENABLED, "requires an explicitly authorized disposable TEST_DATABASE_URL")
class PostgresLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row

        parameters = conninfo_to_dict(DATABASE_URL)
        target_mode = _disposable_target(parameters, os.environ)
        cls.psycopg = psycopg
        cls.identity = "live-test-" + uuid4().hex
        cls.kwargs = dict(sslmode=os.environ.get("TEST_DATABASE_SSLMODE", "require"),
                          sslrootcert=os.environ.get("TEST_DATABASE_SSLROOTCERT") or None)
        cls.connect_kwargs = _options(DATABASE_URL, cls.kwargs["sslmode"], 15, cls.kwargs["sslrootcert"])
        cls.connect_kwargs["row_factory"] = dict_row
        if target_mode == "empty_supabase_project":
            with psycopg.connect(DATABASE_URL, **cls.connect_kwargs) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                _assert_empty_project(connection)
        # initialize_schema refuses existing namespaces before any data write.
        initialize_schema(DATABASE_URL, registry_id=cls.identity, **cls.kwargs)
        cls.created = True
        cls.addClassCleanup(cls.cleanup_schema)
        cls.store = PostgresStore(DATABASE_URL, expected_registry_id=cls.identity, **cls.kwargs)

    @classmethod
    def cleanup_schema(cls):
        if not getattr(cls, "created", False):
            return
        with cls.psycopg.connect(DATABASE_URL, **cls.connect_kwargs) as connection:
            row = connection.execute("SELECT registry_id FROM guest_registry.registry_meta WHERE singleton=1 FOR UPDATE").fetchone()
            if row is None or row["registry_id"] != cls.identity:
                raise RuntimeError("Test schema identity changed; cleanup refused.")
            connection.execute("DROP SCHEMA guest_registry CASCADE")

    def setUp(self):
        # Only operate on the namespace created and pinned by this suite.
        with self.psycopg.connect(DATABASE_URL, **self.connect_kwargs) as connection:
            row = connection.execute("SELECT registry_id FROM guest_registry.registry_meta WHERE singleton=1 FOR UPDATE").fetchone()
            self.assertEqual(row["registry_id"], self.identity)
            connection.execute("TRUNCATE " + ", ".join("guest_registry." + table for table in DATA_TABLES) + " RESTART IDENTITY CASCADE")
            connection.execute("UPDATE guest_registry.registry_meta SET access_seeded=false WHERE singleton=1")

    def add(self, **overrides):
        values = dict(name="АлЁна Иванова", phone="8 999 123-45-67", comment="Комментарий",
                      photo=b"atomic-private-photo", photo_file_id="telegram-file", actor_id=ADMIN)
        values.update(overrides)
        return self.store.add_guest(**values)

    def test_schema_privacy_identity_and_no_implicit_initialization(self):
        with self.psycopg.connect(DATABASE_URL, **self.connect_kwargs) as connection:
            rows = connection.execute("""SELECT c.relname, c.relrowsecurity FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='guest_registry' AND c.relkind='r'""").fetchall()
            self.assertTrue(all(row["relrowsecurity"] for row in rows))
            self.assertEqual(len(rows), 8)
            public = connection.execute("""SELECT EXISTS (
                SELECT 1 FROM pg_namespace n, LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
                WHERE n.nspname='guest_registry' AND a.grantee=0
            ) AS allowed""").fetchone()
            self.assertFalse(public["allowed"])
        with self.assertRaises(PostgresStorageError):
            PostgresStore(DATABASE_URL, expected_registry_id="wrong-pin", **self.kwargs)
        with self.assertRaises(PostgresStorageError):
            initialize_schema(DATABASE_URL, registry_id=self.identity, **self.kwargs)

    def test_card_crud_unique_phone_search_and_audit_are_atomic(self):
        first = self.add()
        with self.assertRaises(DuplicatePhoneError):
            self.add(phone="+7 999 123 45 67")
        second = self.add(name="Гость 50%_\\", phone="79990000002")
        with self.assertRaises(DuplicatePhoneError):
            self.store.update_guest(second.id, second.version, ADMIN, phone=first.phone, comment="Не сохранится")
        self.assertEqual(self.store.get_guest(second.id), second)
        self.assertEqual(self.store.count_audit(), 2)
        for query, expected in (("АЛЕНА", first), ("123-45", first), ("%", second), ("_", second), ("\\", second)):
            found = self.store.search_guests(query)
            self.assertEqual([guest.id for guest in found], [expected.id])
            self.assertEqual(found[0].photo, b"")
            self.assertEqual(self.store.count_search(query), 1)
        updated = self.store.update_guest(first.id, first.version, 1234, comment="Сохранено")
        self.assertEqual(updated.version, first.version + 1)
        self.assertEqual(updated.photo, first.photo)
        with self.assertRaises(StaleGuestError):
            self.store.delete_guest(first.id, first.version, ADMIN)
        self.assertTrue(self.store.delete_guest(first.id, updated.version, ADMIN))
        self.assertIsNone(self.store.get_guest(first.id))
        self.assertEqual([event.action for event in self.store.list_audit(first.id)], ["delete", "update", "add"])

    def test_simultaneous_start_and_stop_have_exactly_one_winner(self):
        guest = self.add()
        now = datetime(2026, 11, 9, 10, 0, 0, 123456, tzinfo=timezone.utc)
        def start(_):
            try:
                return self.store.start_visit(guest.id, guest.version, ADMIN, now=now)
            except (StaleGuestError, VisitStateError):
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            started = [value for value in pool.map(start, range(6)) if value is not None]
        self.assertEqual(len(started), 1)
        visit = started[0]
        self.assertEqual(visit.started_at, now.isoformat(timespec="microseconds"))
        def stop(_):
            try:
                return self.store.stop_visit(guest.id, visit.id, ADMIN, now=now + timedelta(hours=1))
            except VisitStateError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            stopped = [value for value in pool.map(stop, range(6)) if value is not None]
        self.assertEqual(len(stopped), 1)
        summary = self.store.get_visit_summary(guest.id, now=now + timedelta(hours=2))
        self.assertEqual(summary.completed_count, 1)
        self.assertEqual(summary.completed_seconds, 3600)
        self.assertIsNone(summary.active)
        self.assertEqual(self.store.get_guest(guest.id).version, guest.version + 2)

    def test_admin_staff_and_reminders_survive_reopening_and_are_not_regranted(self):
        self.store.bootstrap_employee_access([ADMIN, 1234, 2**53], ADMIN)
        self.store.remember_staff(1234, "Иван Сотрудник", "@ivan_test")
        self.assertEqual(self.store.staff_label(1234), "Иван Сотрудник (@ivan_test)")
        self.assertEqual(self.store.employee_ids(), frozenset({1234}))
        with self.assertRaises(PermissionError):
            self.store.add_employee(5678, 1234)
        self.assertTrue(self.store.add_employee(5678, ADMIN))
        self.assertFalse(self.store.add_employee(5678, ADMIN))
        self.store.bootstrap_employee_access([9999], ADMIN)
        self.assertFalse(self.store.has_employee_access(9999))
        guest = self.add()
        now = datetime.now(timezone.utc)
        visit = self.store.start_visit(guest.id, guest.version, ADMIN, now=now - timedelta(days=2))
        overdue = self.store.overdue_visits(now=now)
        self.assertEqual([(g.id, v.id) for g, v in overdue], [(guest.id, visit.id)])
        self.assertEqual(overdue[0][0].photo, b"")
        self.assertTrue(self.store.mark_reminder_sent(visit.id, 1234, now=now))
        self.assertFalse(self.store.mark_reminder_sent(visit.id, 1234, now=now))
        self.assertTrue(self.store.reminder_sent(visit.id, 1234))
        self.store.stop_visit(guest.id, visit.id, ADMIN, now=now)
        self.assertFalse(self.store.mark_reminder_sent(visit.id, 5678, now=now))
        reopened = PostgresStore(DATABASE_URL, expected_registry_id=self.identity, **self.kwargs)
        self.assertEqual(reopened.employee_ids(), frozenset({1234, 5678}))
        self.assertTrue(reopened.reminder_sent(visit.id, 1234))

    def test_import_preserves_every_section_offsets_missing_photos_and_deleted_ids(self):
        state = {key: [] for key in STATE_KEYS}
        state["guests"] = [dict(id=7, name="Гость", phone="79990000001", phone_key="79990000001",
            comment="Восстановлено", photo="", photo_file_id="", created_by=ADMIN, updated_by=ADMIN,
            created_at=STAMP, updated_at=STAMP, version=9, entry_status="closed", entry_reason="Причина")]
        state["visits"] = [dict(id=11, guest_id=7, started_at=STAMP, stopped_at=None, started_by=ADMIN, stopped_by=None)]
        state["audit_events"] = [dict(id=20, guest_id=101, guest_name="Удалённый", actor_id=ADMIN,
            action="delete", changes={"name": {"before": "Удалённый", "after": None}}, created_at=STAMP)]
        state["employee_access"] = [dict(user_id=1234, added_by=ADMIN, added_at=STAMP)]
        state["staff"] = [dict(actor_id=1234, name="Иван", username="ivan_test", updated_at=STAMP)]
        state["visit_reminders"] = [dict(visit_id=11, recipient_id=1234, sent_at=STAMP)]
        original = deepcopy(state)
        imported = self.store.import_state(state, sequences=dict(guests=60, visits=61, audit_events=62))
        self.assertEqual(state, original)
        self.assertTrue(imported["imported"])
        guest = self.store.get_guest(7)
        self.assertEqual(guest.photo, b"")
        self.assertEqual(guest.created_at, STAMP)
        self.assertEqual(guest.updated_at, STAMP)
        guest = self.store.update_guest(7, guest.version, ADMIN, comment="Редактирование без фото")
        self.assertEqual(guest.photo_file_id, "")
        with self.assertRaises(ValueError):
            self.store.import_state(state)
        next_guest = self.add(phone="79990000002")
        self.assertEqual(next_guest.id, 61)
        next_visit = self.store.start_visit(next_guest.id, next_guest.version, ADMIN)
        self.assertEqual(next_visit.id, 62)
        self.assertGreater(self.store.list_audit()[0].id, 62)
        export = self.store.export_state()
        self.assertEqual(len(export["state"]["staff"]), 1)
        self.assertEqual(export["state"]["employee_access"], state["employee_access"])
        self.assertEqual(export["state"]["visit_reminders"], state["visit_reminders"])
        self.assertEqual(export["state"]["guests"][0]["created_at"], STAMP)

    def test_import_access_without_guests_and_full_backup(self):
        state = {key: [] for key in STATE_KEYS}
        state["employee_access"] = [dict(user_id=1234, added_by=ADMIN, added_at=STAMP)]
        self.store.import_state(state)
        self.store.bootstrap_employee_access([9999], ADMIN)
        self.assertEqual(self.store.employee_ids(), frozenset({1234}))
        self.add()
        root = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        destination = root / ("postgres-live-backup-" + uuid4().hex + ".json")
        self.addCleanup(lambda: destination.unlink(missing_ok=True))
        self.store.backup(destination)
        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(payload["registry_id"], self.identity)
        self.assertEqual(payload["admin_id"], ADMIN)
        self.assertEqual(set(payload["state"]), set(STATE_KEYS))
        self.assertTrue(payload["state"]["guests"][0]["photo"])
        checked, sequences = self.store._validated_state(payload["state"], payload["sequences"], ADMIN)
        self.assertEqual(checked["guests"][0]["photo"], b"atomic-private-photo")

    def test_recovered_residual_totals_preserve_history_without_inventing_visits(self):
        state = {key: [] for key in STATE_KEYS}
        guest = dict(id=7, name="Гость", phone="79990000001", phone_key="79990000001",
            comment="Восстановлено", photo="", photo_file_id="", created_by=ADMIN, updated_by=ADMIN,
            created_at=STAMP, updated_at=STAMP, version=1, entry_status="open", entry_reason="")
        state["guests"] = [guest, {**guest, "id": 8, "phone": "79990000002", "phone_key": "79990000002"}]
        state["visits"] = [
            dict(id=1, guest_id=7, started_at="2026-11-08T10:00:00+00:00",
                 stopped_at="2026-11-08T10:05:00+00:00", started_by=ADMIN, stopped_by=ADMIN),
            dict(id=2, guest_id=7, started_at="2026-11-10T10:00:00+00:00",
                 stopped_at="2026-11-10T10:10:00+00:00", started_by=ADMIN, stopped_by=ADMIN),
        ]
        # The source observation had 3 visits / 3900 seconds. The known exact
        # pre-observation visit (1 / 300) was subtracted by recovery already.
        baselines = [dict(guest_id=7, completed_count=2, completed_seconds=3600,
                          observed_at=STAMP, source_message_id="message115"),
                     dict(guest_id=8, completed_count=0, completed_seconds=0,
                          observed_at=STAMP, source_message_id="message116")]
        self.store.import_state(state, recovered_totals=baselines)
        summary = self.store.get_visit_summary(7)
        self.assertEqual(summary.completed_count, 4)
        self.assertEqual(summary.completed_seconds, 4500)
        self.assertEqual(self.store.get_visit_summary(8).completed_count, 0)
        self.assertEqual(self.store.get_recovered_totals(7), baselines[0])
        self.assertEqual(len(self.store.list_visits(7)), 2)
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["visits"]), 2)
        self.assertEqual(snapshot["recovered_totals"], baselines)
        self.assertEqual(self.store.export_state()["recovered_totals"], baselines)
        root = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        destination = root / ("postgres-live-baseline-backup-" + uuid4().hex + ".json")
        self.addCleanup(lambda: destination.unlink(missing_ok=True))
        self.store.backup(destination)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["recovered_totals"], baselines)
        self.assertTrue(self.store.delete_guest(7, 1, ADMIN))
        self.assertIsNone(self.store.get_recovered_totals(7))
        self.assertEqual(self.store.get_visit_summary(7).completed_seconds, 0)
        self.assertEqual(self.store.list_visits(7), [])
        self.assertEqual(self.store.get_recovered_totals(8), baselines[1])


if __name__ == "__main__":
    unittest.main()
