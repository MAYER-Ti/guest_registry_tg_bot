"""A migration requires explicit writing, an empty target and full readback."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import migrate_to_postgres as migration
from postgres_store import PostgresStorageError, STATE_KEYS


ADMIN = 7011639945
REGISTRY = "private-registry-0123456789"
DSN = "postgresql://operator:NEVER_PRINT_PASSWORD@example.invalid/postgres"
STAMP = "2026-11-09T12:30:01.123456+03:00"


def source_payload():
    state = {key: [] for key in STATE_KEYS}
    state["guests"] = [dict(id=7, name="Гость с фото", phone="+79991234567", phone_key="79991234567",
        comment="Частный комментарий", photo=base64.b64encode(b"original-photo-bytes").decode("ascii"),
        photo_file_id="telegram-file", created_by=ADMIN, updated_by=1234, created_at=STAMP,
        updated_at=STAMP, version=5, entry_status="closed", entry_reason="Частная причина"),
        dict(id=9, name="Фото пока потеряно", phone="+79991234568", phone_key="79991234568",
        comment="", photo="", photo_file_id="", created_by=ADMIN, updated_by=ADMIN,
        created_at=STAMP, updated_at=STAMP, version=1, entry_status="open", entry_reason="")]
    state["visits"] = [dict(id=11, guest_id=7, started_at=STAMP, stopped_at=None, started_by=1234, stopped_by=None)]
    state["audit_events"] = [dict(id=21, guest_id=99, guest_name="Удалённый гость", actor_id=1234,
        action="delete", changes={"name": {"before": "Удалённый гость", "after": None}}, created_at=STAMP)]
    state["staff"] = [dict(actor_id=1234, name="Сотрудник", username="staff_test", updated_at=STAMP)]
    state["employee_access"] = [dict(user_id=1234, added_by=ADMIN, added_at=STAMP)]
    state["visit_reminders"] = [dict(visit_id=11, recipient_id=1234, sent_at=STAMP)]
    return dict(format="guest-registry-reconstructed-state", version=1, admin_id=ADMIN, state=state,
        sequences=dict(guests=100, visits=200, audit_events=300),
        recovered_totals=[dict(guest_id=7, completed_count=3, completed_seconds=7200,
                              observed_at=STAMP, source_message_id="private-chat:42")],
        provenance={"private": "Sensitive provenance must never be printed"})


class FakeDestination:
    def __init__(self):
        self.payload = dict(registry_id=REGISTRY, admin_id=ADMIN, schema_version=1,
                            state={key: [] for key in STATE_KEYS},
                            sequences=dict(guests=0, visits=0, audit_events=0), recovered_totals=[])
        self.import_calls = 0
        self.export_calls = 0
        self.after_import = None

    def export_state(self):
        self.export_calls += 1
        return deepcopy(self.payload)

    def import_state(self, state, *, admin_id, sequences, recovered_totals):
        self.import_calls += 1
        if any(self.payload["state"].values()):
            raise ValueError("Nonempty destination")
        self.payload.update(state=migration._wire(deepcopy(state)), sequences=deepcopy(sequences),
                            recovered_totals=deepcopy(recovered_totals))
        if self.after_import:
            self.after_import(self.payload)
        return dict(imported=True)


class PostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.path = self.runtime / "private-source.json"
        self.payload = source_payload()
        self.write(self.payload)
        self.remote = FakeDestination()
        self.factory = Mock(return_value=self.remote)
        self.initializer = Mock()
        self.environ = dict(POSTGRES_MIGRATION_URL=DSN, REGISTRY_ID=REGISTRY)

    def tearDown(self):
        shutil.rmtree(self.runtime)

    def write(self, payload):
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def apply(self, **kwargs):
        return migration.migrate(self.path, 2, apply=True, environ=self.environ,
                                 store_factory=self.factory, initializer=self.initializer, **kwargs)

    def test_default_is_local_dry_run_with_counts_hashes_and_unchanged_source(self):
        before = self.path.read_bytes()
        with patch("postgres_store._load_driver", side_effect=AssertionError("No driver or network")):
            report = migration.migrate(self.path, store_factory=self.factory, initializer=self.initializer)
        self.factory.assert_not_called()
        self.initializer.assert_not_called()
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["counts"]["guests"], 2)
        self.assertEqual(report["counts"]["photos"], 1)
        self.assertEqual(report["counts"]["missing_photos"], 1)
        self.assertEqual(report["counts"]["recovered_totals"], 1)
        self.assertEqual(len(report["source_sha256"]), 64)
        self.assertEqual(len(report["content_sha256"]), 64)
        self.assertEqual(self.path.read_bytes(), before)
        text = json.dumps(report)
        for secret in ("Частный", "private-chat", "telegram-file", "Sensitive provenance", str(self.path)):
            self.assertNotIn(secret, text)

    def test_apply_imports_once_and_reads_back_every_section_photos_baselines_and_deleted_id_counters(self):
        before = self.path.read_bytes()
        report = self.apply()
        self.assertTrue(report["verified"])
        self.assertEqual(self.remote.import_calls, 1)
        self.assertEqual(self.remote.export_calls, 2)
        self.initializer.assert_not_called()
        self.assertEqual(self.remote.payload["sequences"], self.payload["sequences"])
        self.assertEqual(self.remote.payload["recovered_totals"], self.payload["recovered_totals"])
        self.assertEqual(self.remote.payload["state"]["guests"][0]["photo"], self.payload["state"]["guests"][0]["photo"])
        self.assertEqual(self.path.read_bytes(), before)
        self.factory.assert_called_once_with(DSN, expected_registry_id=REGISTRY,
                                            expected_admin_id=ADMIN, sslmode="require", sslrootcert=None)

    def test_initialize_requires_apply_and_only_forwards_explicit_role_and_verified_tls(self):
        with self.assertRaises(migration.MigrationError):
            migration.migrate(self.path, initialize=True, initializer=self.initializer)
        self.initializer.assert_not_called()
        self.environ.update(POSTGRES_RUNTIME_ROLE="guest_bot", DB_SSLMODE="verify-full", DB_SSLROOTCERT="system")
        self.apply(initialize=True)
        self.initializer.assert_called_once_with(DSN, registry_id=REGISTRY, admin_id=ADMIN,
                                                sslmode="verify-full", sslrootcert="system", runtime_role="guest_bot")

    def test_expected_count_is_mandatory_for_apply_and_count_mismatch_precedes_all_database_calls(self):
        for expected in (None, 1, True, -1):
            with self.subTest(expected=expected), self.assertRaises(migration.MigrationError):
                migration.migrate(self.path, expected, apply=True, initialize=True, environ=self.environ,
                                  store_factory=self.factory, initializer=self.initializer)
        self.factory.assert_not_called()
        self.initializer.assert_not_called()

    def test_operator_url_has_no_runtime_fallback_and_weak_tls_is_rejected_before_connecting(self):
        for changes in ({"POSTGRES_MIGRATION_URL": "", "DATABASE_URL": DSN}, {"DB_SSLMODE": "disable"},
                        {"POSTGRES_MIGRATION_URL": DSN + "?sslmode=disable", "DB_SSLMODE": "require"},
                        {"DB_SSLMODE": "verify-full"}, {"REGISTRY_ID": ""}, {"POSTGRES_RUNTIME_ROLE": "anon"}):
            with self.subTest(changes=changes), self.assertRaises(migration.MigrationError) as caught:
                migration.migrate(self.path, 2, apply=True, environ={**self.environ, **changes}, store_factory=self.factory)
            self.assertNotIn("NEVER_PRINT_PASSWORD", str(caught.exception))
        self.factory.assert_not_called()

    def test_invalid_state_counter_baseline_and_administrator_fail_before_initialization(self):
        invalid = []
        for mutate in (lambda p: p["state"]["guests"][0].update(phone_key="wrong"),
                       lambda p: p["sequences"].update(guests=1),
                       lambda p: p["recovered_totals"][0].update(guest_id=999),
                       lambda p: p.update(recovered_totals=None),
                       lambda p: p.update(admin_id=1234),
                       lambda p: p.pop("sequences"), lambda p: p.update(version=2)):
            value = deepcopy(self.payload)
            mutate(value)
            invalid.append(value)
        for payload in invalid:
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(migration.MigrationError):
                    self.apply(initialize=True)
        self.initializer.assert_not_called()
        self.factory.assert_not_called()

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        for text in ('{"state":{},"state":{},"sequences":{}}', '{"state":NaN,"sequences":{}}'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(migration.MigrationError):
                migration.read_source(self.path)

    def test_older_wrapper_without_baselines_and_complete_postgres_backup_are_supported(self):
        older = deepcopy(self.payload)
        older.pop("recovered_totals")
        self.write(older)
        self.assertEqual(migration.read_source(self.path).recovered_totals, [])
        backup = {**self.payload, "format": "guest-registry-postgres-backup", "schema_version": 1,
                  "registry_id": "original-registry-123456789", "created_at": STAMP}
        self.write(backup)
        self.assertEqual(migration.read_source(self.path).recovered_totals, self.payload["recovered_totals"])

    def test_nonempty_destination_never_imports_or_replaces_data(self):
        self.remote.payload["state"]["staff"] = deepcopy(self.payload["state"]["staff"])
        before = deepcopy(self.remote.payload)
        with self.assertRaises(migration.MigrationError):
            self.apply()
        self.assertEqual(self.remote.import_calls, 0)
        self.assertEqual(self.remote.payload, before)

    def test_every_section_photo_baseline_sequence_and_identity_is_checked_after_import(self):
        mutations = [lambda p, section=section: p["state"][section].pop() for section in STATE_KEYS]
        mutations.extend((lambda p: p["state"]["guests"][0].update(photo=base64.b64encode(b"different-photo").decode("ascii")),
                          lambda p: p["recovered_totals"][0].update(completed_seconds=0),
                          lambda p: p["sequences"].update(guests=101), lambda p: p.update(registry_id="wrong-destination")))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.remote = FakeDestination()
                self.remote.after_import = mutate
                self.factory.return_value = self.remote
                with self.assertRaises(migration.MigrationError):
                    self.apply()
                self.assertEqual(self.remote.import_calls, 1)

    def test_row_order_does_not_change_readback_identity(self):
        self.remote.after_import = lambda p: p["state"]["guests"].reverse()
        self.assertTrue(self.apply()["verified"])

    def test_source_change_before_import_is_detected_without_importing(self):
        def mutate_source():
            self.path.write_text("changed by another process", encoding="utf-8")
            return deepcopy(self.remote.payload)
        self.remote.export_state = mutate_source
        with self.assertRaises(migration.MigrationError):
            self.apply()
        self.assertEqual(self.remote.import_calls, 0)

    def test_cli_errors_never_echo_credentials_paths_guest_data_or_raw_driver_errors(self):
        out, err = StringIO(), StringIO()
        with patch.object(migration, "migrate", side_effect=PostgresStorageError(DSN + str(self.path))), \
                redirect_stdout(out), redirect_stderr(err):
            status = migration.main(["--source", str(self.path), "--apply", "--expected-guests", "2"])
        self.assertEqual(status, 1)
        self.assertFalse(out.getvalue())
        for secret in (DSN, "NEVER_PRINT_PASSWORD", str(self.path), "Частный"):
            self.assertNotIn(secret, err.getvalue())


if __name__ == "__main__":
    unittest.main()
