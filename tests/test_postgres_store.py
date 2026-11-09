"""Offline contract checks. Real SQL/concurrency checks are explicitly opt-in."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import postgres_store as pg
from storage import DuplicatePhoneError, Guest, StaleGuestError, VisitStateError


ADMIN = 7011639945
REGISTRY = "test-registry-d18d"
SECRET_DSN = "postgresql://private_user:NEVER_PRINT_PASSWORD@example.invalid/postgres"
STAMP = "2026-11-09T10:20:30.123456+03:00"


def card(**changes):
    row = dict(id=7, name="Алёна Иванова", phone="8 999 123-45-67", phone_key="79991234567",
               comment="Комментарий", photo=b"real-photo", photo_file_id="telegram-id",
               created_by=ADMIN, updated_by=ADMIN, created_at=STAMP, updated_at=STAMP,
               version=9, entry_status="closed", entry_reason="Причина")
    row.update(changes)
    return row


def empty_state():
    return {key: [] for key in pg.STATE_KEYS}


def recovered(**changes):
    row = dict(guest_id=7, completed_count=3, completed_seconds=7200,
               observed_at=STAMP, source_message_id="message115")
    row.update(changes)
    return row


class DriverError(Exception):
    def __init__(self, *, sqlstate=None, constraint=None):
        super().__init__(SECRET_DSN)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint)


class Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Connection:
    def __init__(self, driver):
        self.driver = driver
        self.commands = []
        self.committed = self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.committed = kind is None
        self.rolled_back = kind is not None

    def execute(self, query, params=None, **kwargs):
        self.commands.append((query, params, kwargs))
        if self.driver.handler:
            answer = self.driver.handler(query, params)
            if answer is not None:
                return answer
        if "FROM guest_registry.registry_meta" in query:
            return Result([dict(singleton=1, schema_version=1, registry_id=self.driver.registry_id,
                                admin_id=ADMIN, access_seeded=self.driver.seeded)])
        if "FROM information_schema.columns" in query:
            return Result(dict(table_name=table, column_name=field)
                          for table, fields in pg._TABLE_FIELDS.items() for field in fields)
        if "FROM pg_catalog.pg_class" in query:
            return Result(dict(relname=table, relrowsecurity=True) for table in pg._TABLE_FIELDS)
        if "FROM pg_catalog.pg_index" in query:
            return Result([
                dict(table_name="guests", index_name="guests_phone_key_key", indisunique=True,
                     indisvalid=True, indnkeyatts=1, indnatts=1, column_name="phone_key", predicate=None),
                dict(table_name="visits", index_name="visits_one_active_per_guest", indisunique=True,
                     indisvalid=True, indnkeyatts=1, indnatts=1, column_name="guest_id", predicate="(stopped_at IS NULL)"),
            ])
        if "FROM pg_catalog.pg_namespace" in query:
            return Result([{"exists": 1}] if self.driver.namespace_exists else [])
        return Result()


class Driver:
    Error = DriverError

    def __init__(self):
        self.connections = []
        self.connect_calls = []
        self.handler = None
        self.registry_id = REGISTRY
        self.seeded = False
        self.namespace_exists = False

    def connect(self, dsn, **kwargs):
        self.connect_calls.append((dsn, kwargs))
        connection = Connection(self)
        self.connections.append(connection)
        return connection


class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        self.driver = Driver()
        self.load = patch.object(pg, "_load_driver", return_value=(self.driver, "dict_row"))
        self.load.start()
        self.addCleanup(self.load.stop)

    def store(self, **kwargs):
        return pg.PostgresStore(SECRET_DSN, expected_registry_id=REGISTRY, **kwargs)

    def test_startup_only_reads_existing_schema_and_never_exposes_credentials(self):
        store = self.store()
        self.assertEqual(repr(store), "PostgresStore(backend='postgres')")
        self.assertFalse(hasattr(store, "path"))
        self.assertEqual(store.backend, "postgres")
        statements = [query for query, _, _ in self.driver.connections[0].commands]
        self.assertTrue(statements[0].startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        self.assertFalse(any(query.startswith(("CREATE", "INSERT", "ALTER")) for query in statements))
        options = self.driver.connect_calls[0][1]
        self.assertEqual(options["sslmode"], "require")
        self.assertIsNone(options["prepare_threshold"])
        self.assertEqual(options["connect_timeout"], 15)

    def test_ssl_overrides_untrusted_connection_options_and_accepts_system_ca(self):
        self.store(sslmode="verify-full", sslrootcert="system")
        options = self.driver.connect_calls[0][1]
        self.assertEqual(options["sslmode"], "verify-full")
        self.assertEqual(options["sslrootcert"], "system")
        for mode in ("disable", "allow", "prefer", None, []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.store(sslmode=mode)

    def test_identity_checked_again_after_startup_without_reinitializing(self):
        store = self.store()
        self.driver.registry_id = "different-instance"
        with self.assertRaises(pg.PostgresStorageError) as caught:
            store.get_guest(7)
        self.assertNotIn("NEVER_PRINT", str(caught.exception))
        self.assertTrue(self.driver.connections[-1].rolled_back)
        self.assertFalse(any(query.startswith("CREATE") for query, _, _ in self.driver.connections[-1].commands))

    def test_missing_or_unavailable_database_is_sanitized_without_retries(self):
        store = self.store()
        def handler(query, params):
            if "FROM guest_registry.registry_meta" in query:
                raise DriverError(sqlstate="42P01")
        self.driver.handler = handler
        before = len(self.driver.connections)
        with self.assertRaises(pg.PostgresStorageError) as caught:
            store.employee_ids()
        self.assertEqual(len(self.driver.connections), before + 1)
        self.assertNotIn("NEVER_PRINT", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_only_named_business_constraints_map_to_existing_errors(self):
        store = self.store()
        for constraint, kind in (("guests_phone_key_key", DuplicatePhoneError),
                                 ("visits_one_active_per_guest", VisitStateError),
                                 ("unrelated_private_constraint", pg.PostgresStorageError)):
            def handler(query, params):
                if query.startswith("INSERT"):
                    raise DriverError(sqlstate="23505", constraint=constraint)
            self.driver.handler = handler
            with self.subTest(constraint=constraint), self.assertRaises(kind) as caught:
                store.add_guest(name="Гость", phone="79990000001", comment="", photo=b"photo",
                                photo_file_id="file", actor_id=ADMIN)
            self.assertNotIn("NEVER_PRINT", str(caught.exception))
            self.assertTrue(self.driver.connections[-1].rolled_back)

    def test_new_cards_require_complete_photos_before_connecting(self):
        store = self.store()
        before = len(self.driver.connections)
        with self.assertRaises(ValueError):
            store.add_guest(name="Гость", phone="79990000001", comment="", photo=b"",
                            photo_file_id="", actor_id=ADMIN)
        self.assertEqual(len(self.driver.connections), before)

    def test_stale_edit_does_not_write_and_valid_edit_preserves_recovered_photo(self):
        store = self.store()
        old = card(photo=b"", photo_file_id="")
        def handler(query, params):
            if query.startswith("SELECT * FROM guest_registry.guests"):
                return Result([old])
            if query.startswith("UPDATE guest_registry.guests"):
                return Result([{**old, "version": old["version"] + 1, "comment": "Новое"}])
        self.driver.handler = handler
        with self.assertRaises(StaleGuestError):
            store.update_guest(7, 8, ADMIN, comment="Новое")
        self.assertFalse(any(query.startswith("UPDATE") for query, _, _ in self.driver.connections[-1].commands))
        updated = store.update_guest(7, 9, ADMIN, comment="Новое")
        self.assertEqual(updated.photo, b"")
        self.assertEqual(updated.photo_file_id, "")
        statements = [query for query, _, _ in self.driver.connections[-1].commands]
        self.assertIn("FOR UPDATE", next(query for query in statements if "FROM guest_registry.guests" in query))
        self.assertTrue(any("INSERT INTO guest_registry.audit_events" in query for query in statements))
        with self.assertRaises(ValueError):
            store.update_guest(7, 9, ADMIN, photo=b"")

    def test_snapshot_can_omit_photos_or_transform_complete_photos_without_changing_timestamps(self):
        store = self.store()
        def handler(query, params):
            if "FROM guest_registry.guests ORDER BY id" in query:
                return Result([card(photo=b"" if "''::bytea" in query else b"real-photo")])
            if "FROM guest_registry.visits ORDER BY id" in query:
                return Result()
        self.driver.handler = handler
        now = datetime(2026, 11, 9, 0, 0, 0, 987654, tzinfo=timezone.utc)
        wire = store.snapshot(now=now)
        guest = pg.decode_guest(wire["guests"][0])
        self.assertEqual(guest.photo, b"")
        self.assertEqual(guest.created_at, STAMP)
        self.assertEqual(wire["generated_at"], now.isoformat(timespec="microseconds"))
        wire = store.snapshot(include_photos=True, now=now, photo_transform=lambda raw: b"thumb")
        self.assertEqual(pg.decode_guest(wire["guests"][0]).photo, b"thumb")
        wire = store.snapshot(include_photos=True, now=now, photo_transform=lambda raw: b"")
        self.assertEqual(pg.decode_guest(wire["guests"][0]).photo, b"")
        with self.assertRaises(pg.PostgresStorageError):
            store.snapshot(include_photos=True, photo_transform=lambda _: (_ for _ in ()).throw(RuntimeError(SECRET_DSN)))

    def test_explicit_initializer_refuses_even_an_empty_existing_namespace(self):
        self.driver.namespace_exists = True
        with self.assertRaises(pg.PostgresStorageError):
            pg.initialize_schema(SECRET_DSN, registry_id=REGISTRY)
        self.assertTrue(self.driver.connections[-1].rolled_back)
        self.assertFalse(any(query.startswith("CREATE") for query, _, _ in self.driver.connections[-1].commands))

    def test_import_preserves_staff_access_with_zero_guests_and_seals_bootstrap(self):
        store = self.store()
        state = empty_state()
        state["employee_access"] = [dict(user_id=1234, added_by=ADMIN, added_at=STAMP)]
        state["staff"] = [dict(actor_id=1234, name="Сотрудник", username="employee", updated_at=STAMP)]
        source = deepcopy(state)
        result = store.import_state(state, sequences={"guests": 93, "visits": 65, "audit_events": 101})
        self.assertEqual(state, source)
        self.assertEqual(result, dict(imported=True, guests=0, visits=0, employees=1, audit_events=0))
        connection = self.driver.connections[-1]
        queries = [query for query, _, _ in connection.commands]
        lock_index = next(index for index, query in enumerate(queries) if query.startswith("LOCK TABLE"))
        insert_index = next(index for index, query in enumerate(queries) if query.startswith("INSERT"))
        self.assertLess(lock_index, insert_index)
        self.assertIn("ALTER SEQUENCE guest_registry.guests_id_seq RESTART WITH 94", queries)
        self.assertIn("ALTER SEQUENCE guest_registry.visits_id_seq RESTART WITH 66", queries)
        self.assertIn("ALTER SEQUENCE guest_registry.audit_events_id_seq RESTART WITH 102", queries)
        self.assertIn("UPDATE guest_registry.registry_meta SET access_seeded=true WHERE singleton=1", queries)
        self.assertTrue(connection.committed)
        self.assertFalse(any("setval" in query for query in queries))

    def test_import_nonempty_destination_in_any_section_refuses_every_write(self):
        store = self.store()
        def handler(query, params):
            if query == "SELECT 1 FROM guest_registry.employee_access LIMIT 1":
                return Result([{"exists": 1}])
        self.driver.handler = handler
        with self.assertRaises(ValueError):
            store.import_state(empty_state())
        connection = self.driver.connections[-1]
        self.assertTrue(connection.rolled_back)
        self.assertFalse(any(query.startswith(("INSERT", "ALTER SEQUENCE", "UPDATE")) for query, _, _ in connection.commands))

    def test_failed_import_rolls_back_records_and_transactional_sequences(self):
        store = self.store()
        def handler(query, params):
            if query.startswith("ALTER SEQUENCE guest_registry.visits"):
                raise DriverError(sqlstate="42501")
        self.driver.handler = handler
        with self.assertRaises(pg.PostgresStorageError):
            store.import_state(empty_state(), sequences=dict(guests=88, visits=99, audit_events=10))
        self.assertTrue(self.driver.connections[-1].rolled_back)
        self.assertFalse(self.driver.connections[-1].committed)

    def test_bootstrap_does_not_regrant_after_initialization(self):
        store = self.store()
        self.driver.seeded = True
        store.bootstrap_employee_access([1234], ADMIN)
        self.assertFalse(any(query.startswith("INSERT") for query, _, _ in self.driver.connections[-1].commands))

    def test_summary_adds_only_residual_baseline_and_exact_visits(self):
        store = self.store()
        now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        visits = [dict(id=1, guest_id=7, started_at="2026-11-09T06:00:00+00:00",
                       stopped_at="2026-11-09T06:30:00+00:00", started_by=ADMIN, stopped_by=ADMIN),
                  dict(id=2, guest_id=7, started_at="2026-11-09T10:00:00+00:00",
                       stopped_at="2026-11-09T10:15:00+00:00", started_by=ADMIN, stopped_by=ADMIN),
                  dict(id=3, guest_id=7, started_at="2026-11-09T11:50:00+00:00",
                       stopped_at=None, started_by=ADMIN, stopped_by=None)]
        for baseline, count, seconds in ((None, 2, 2700),
                                         (recovered(completed_count=0, completed_seconds=0), 2, 2700),
                                         (recovered(), 5, 9900)):
            def handler(query, params):
                if query.startswith("SELECT * FROM guest_registry.visits WHERE guest_id="):
                    return Result(visits)
                if query.startswith("SELECT * FROM guest_registry.recovered_totals WHERE guest_id="):
                    return Result([baseline] if baseline else [])
            self.driver.handler = handler
            with self.subTest(baseline=baseline):
                summary = store.get_visit_summary(7, now=now)
                self.assertEqual(summary.completed_count, count)
                self.assertEqual(summary.completed_seconds, seconds)
                self.assertEqual(summary.current_seconds, 600)
                self.assertEqual(summary.total_seconds, seconds + 600)
                self.assertEqual(store.get_recovered_totals(7), baseline)
        # Both exact visits, including the one already subtracted from the
        # source observation, are counted once alongside the residual baseline.
        self.assertEqual(store.get_visit_summary(7, now=now).completed_count, 5)

    def test_recovered_baseline_import_is_separate_immutable_and_atomic(self):
        store = self.store()
        state = empty_state()
        state["guests"] = [card()]
        baselines = [recovered()]
        original = deepcopy(baselines)
        store.import_state(state, recovered_totals=baselines)
        self.assertEqual(baselines, original)
        commands = self.driver.connections[-1].commands
        self.assertTrue(any(query.startswith("INSERT INTO guest_registry.recovered_totals") for query, _, _ in commands))
        self.assertTrue(any(query == "SELECT 1 FROM guest_registry.recovered_totals LIMIT 1" for query, _, _ in commands))
        self.assertFalse(any(query.startswith("INSERT INTO guest_registry.visits") for query, _, _ in commands))
        def handler(query, params):
            if query.startswith("ALTER SEQUENCE"):
                raise DriverError(sqlstate="42501")
        self.driver.handler = handler
        with self.assertRaises(pg.PostgresStorageError):
            store.import_state(state, recovered_totals=baselines)
        self.assertTrue(self.driver.connections[-1].rolled_back)

    def test_snapshot_and_complete_json_backup_preserve_baseline_provenance(self):
        store = self.store()
        baseline = recovered()
        def handler(query, params):
            if "FROM guest_registry.recovered_totals ORDER BY guest_id" in query:
                return Result([baseline])
            if query.startswith("SELECT last_value, is_called FROM guest_registry."):
                return Result([dict(last_value=1, is_called=False)])
        self.driver.handler = handler
        self.assertEqual(store.snapshot()["recovered_totals"], [baseline])
        self.assertEqual(store.export_state()["recovered_totals"], [baseline])
        root = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("postgres-baseline-backup-" + uuid4().hex + ".json")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        store.backup(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["recovered_totals"], [baseline])
        self.assertEqual(set(payload["state"]), set(pg.STATE_KEYS))


class ImportValidationTests(unittest.TestCase):
    def validated(self, state, sequences=None):
        return pg.PostgresStore._validated_state(state, sequences, ADMIN)

    def test_preserves_microseconds_source_offsets_recovered_photos_and_all_sections(self):
        state = empty_state()
        state["guests"] = [card(photo=b"", photo_file_id="")]
        checked, sequences = self.validated(state)
        self.assertEqual(checked["guests"][0]["created_at"], STAMP)
        self.assertEqual(checked["guests"][0]["updated_at"], STAMP)
        self.assertEqual(checked["guests"][0]["photo"], b"")
        self.assertEqual(sequences, dict(guests=7, visits=0, audit_events=0))
        self.assertEqual(checked["guests"][0]["name_key"], "алена иванова")

    def test_import_rejects_metadata_only_photos_but_report_decode_allows_them(self):
        row = card(photo="")
        self.assertEqual(pg.decode_guest(row).photo, b"")
        state = empty_state()
        state["guests"] = [row]
        with self.assertRaises(ValueError):
            self.validated(state)

    def test_rejects_duplicate_phone_guest_and_invalid_high_water_marks(self):
        state = empty_state()
        for rows, seq in (([card(), card(id=8)], None),
                          ([card(), card()], None),
                          ([card()], dict(guests=6, visits=0, audit_events=0)),
                          ([card()], dict(guests=True, visits=0, audit_events=0))):
            state["guests"] = rows
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                self.validated(state, seq)

    def test_visits_require_existing_guests_unique_active_visit_and_aware_times(self):
        state = empty_state()
        state["guests"] = [card()]
        visit = dict(id=1, guest_id=7, started_at=STAMP, stopped_at=None, started_by=ADMIN, stopped_by=None)
        for rows in ([{**visit, "guest_id": 8}], [visit, {**visit, "id": 2}],
                     [{**visit, "started_at": "2026-11-09T10:20:30.123456"}],
                     [{**visit, "stopped_by": ADMIN}],
                     [{**visit, "stopped_at": "2026-11-09T01:00:00+03:00", "stopped_by": ADMIN}]):
            state["visits"] = rows
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.validated(state)

    def test_deleted_guest_audit_survives_without_foreign_key_but_invalid_audit_is_rejected(self):
        state = empty_state()
        event = dict(id=28, guest_id=900, guest_name="Удалённый гость", actor_id=ADMIN,
                     action="delete", changes={"name": {"before": "Удалённый гость", "after": None}}, created_at=STAMP)
        state["audit_events"] = [event]
        checked, sequences = self.validated(state)
        self.assertEqual(checked["audit_events"], [event])
        self.assertEqual(sequences["audit_events"], 28)
        for changes in ({"name": {"before": [], "after": None}},
                        {"name": {"before": float("nan"), "after": None}},
                        {"name": {"before": "nul\0", "after": None}}):
            state["audit_events"] = [{**event, "changes": changes}]
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validated(state)

    def test_admin_identity_and_access_are_validated_even_with_no_cards(self):
        state = empty_state()
        for grant in (dict(user_id=1234, added_by=5678, added_at=STAMP),
                      dict(user_id=ADMIN, added_by=ADMIN, added_at=STAMP),
                      dict(user_id=True, added_by=ADMIN, added_at=STAMP)):
            state["employee_access"] = [grant]
            with self.subTest(grant=grant), self.assertRaises(ValueError):
                self.validated(state)

    def test_reminders_cannot_reference_missing_visits(self):
        state = empty_state()
        state["visit_reminders"] = [dict(visit_id=99, recipient_id=1234, sent_at=STAMP)]
        with self.assertRaises(ValueError):
            self.validated(state)

    def test_recovered_totals_require_valid_nonnegative_residuals_and_provenance(self):
        validate = pg.PostgresStore._validated_recovered_totals
        self.assertEqual(validate(None, {7}), [])
        self.assertEqual(validate([], {7}), [])
        self.assertEqual(validate([recovered()], {7}), [recovered()])
        self.assertEqual(validate([recovered(completed_count=0, completed_seconds=0)], {7})[0]["completed_count"], 0)
        invalid = ([recovered(guest_id=8)], [recovered(), recovered()],
                   [recovered(completed_count=-1)], [recovered(completed_seconds=-1)],
                   [recovered(completed_count=True)], [recovered(completed_seconds=1.5)],
                   [recovered(observed_at="2026-11-09T10:20:30")],
                   [recovered(source_message_id="")], [recovered(source_message_id=115)])
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate(rows, {7})


if __name__ == "__main__":
    unittest.main()
