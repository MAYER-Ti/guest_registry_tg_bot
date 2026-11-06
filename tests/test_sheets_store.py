"""Private Sheets RPC contract exercised without any real Google requests."""
from __future__ import annotations

import base64
import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
from urllib import error, request
from uuid import uuid4

from sheets_store import (
    SheetsStorageError, SheetsStore, _GoogleRedirects, _http_post, decode_guest,
    decode_visit,
)
from storage import DuplicatePhoneError, StaleGuestError, Store, VisitStateError


URL = "https://script.google.com/macros/s/test-deployment_123/exec"
SECRET = "only-a-fake-test-secret-" + "x" * 40
OWNER = "owner@example.com"
ADMIN = 7011639945
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
HEALTH = {"ready": True, "version": 1, "schema_version": 1, "owner_email": OWNER, "admin_id": ADMIN}


def wire(value):
    if is_dataclass(value):
        return wire(asdict(value))
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, frozenset)):
        return [wire(item) for item in value]
    return value


def response(value):
    return json.dumps({"ok": True, "result": wire(value)}, ensure_ascii=False).encode("utf-8")


class LocalTransport:
    """A signed protocol bridge over the existing real SQLite Store.

    It intentionally loses selected mutation responses after committing and
    serves the persisted operation result when the client retries unchanged.
    """
    MUTATIONS = {"add_guest", "update_guest", "delete_guest", "start_visit", "stop_visit",
                 "add_employee", "remember_staff", "bootstrap_employee_access", "mark_reminder_sent"}

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.ledger = {}
        self.lose_after_commit = set()
        self.photos = {}
        self.hooks = {}

    def __call__(self, url, body, timeout):
        envelope = json.loads(body)
        signature = hmac.new(SECRET.encode(), envelope["payload"].encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, envelope["signature"]):
            raise AssertionError("Bad HMAC")
        message = json.loads(envelope["payload"])
        expected_timeout = 300 if message["action"] in {"create_backup", "import_state", "restore_backup"} else 40
        if url != URL or timeout != expected_timeout:
            raise AssertionError("Unexpected transport arguments")
        if message["version"] != 1 or type(message["issued_at"]) is not int:
            raise AssertionError("Invalid protocol")
        self.calls.append((message, body))
        action, params = message["action"], message["params"]
        operation = message["operation_id"]
        if operation in self.ledger:
            return self.ledger[operation]
        if action in self.hooks:
            return self.hooks[action](message)
        if action == "health":
            return response(HEALTH)
        if action == "get_photo":
            return response(self.photos[params["photo_ref"]])
        if action == "create_backup":
            return response({"complete": True, "created_at": NOW.isoformat(), "folder_id": "private-backup-id",
                             "manifest_file_id": "private-manifest-id", "photo_count": 2,
                             "counts": {"guests": 2, "visits": 1, "employees": 2, "audit_events": 4}})
        if action == "snapshot":
            from reporting import report_snapshot
            moment = datetime.fromisoformat(params["now"]) if params["now"] else NOW
            snapshot = report_snapshot(self.store, now=moment)
            guests, visits = [], []
            for row in snapshot.rows:
                guest = self.store.get_guest(row.guest.id)
                ref = "photo-reference-" + str(guest.id) + "-" + str(guest.version)
                self.photos[ref] = guest.photo
                item = wire(row.guest)
                item["photo"] = ""
                item["photo_ref"] = ref
                guests.append(item)
                visits.extend(wire(list(row.visits)))
            return response({"generated_at": moment.isoformat(), "guests": guests, "visits": visits})
        params = dict(params)
        if "now" in params:
            params["now"] = datetime.fromisoformat(params["now"]) if params["now"] else None
        if action == "add_guest":
            params["photo"] = base64.b64decode(params["photo"])
        if action == "update_guest":
            fields = params.pop("fields")
            if "photo" in fields:
                fields["photo"] = base64.b64decode(fields["photo"])
            params.update(fields)
        try:
            result = getattr(self.store, action)(**params)
            if action == "overdue_visits":
                result = [{"guest": guest, "visit": visit} for guest, visit in result]
            encoded = response(result)
        except (DuplicatePhoneError, StaleGuestError, VisitStateError, PermissionError, ValueError) as exc:
            kind = {DuplicatePhoneError: "DUPLICATE_PHONE", StaleGuestError: "STALE_GUEST",
                    VisitStateError: "VISIT_STATE", PermissionError: "PERMISSION", ValueError: "VALIDATION"}[type(exc)]
            encoded = json.dumps({"ok": False, "error": {"code": kind, "message": "private server details " + SECRET}}).encode()
        if action in self.MUTATIONS:
            self.ledger[operation] = encoded
        if action in self.lose_after_commit:
            self.lose_after_commit.remove(action)
            raise error.URLError("Simulated response loss " + SECRET)
        return encoded


class SheetsStoreTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.local = Store(self.runtime / "guests.sqlite3")
        self.transport = LocalTransport(self.local)
        self.client = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=self.transport)
        self.client.bootstrap_employee_access([ADMIN, 101, 102], ADMIN)
        self.sleep_patch = patch("sheets_store.time.sleep")
        self.sleep = self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        shutil.rmtree(self.runtime)
        try:
            self.runtime.parent.rmdir()
        except OSError:
            pass

    def seed(self, index=1):
        return self.client.add_guest(name=f"Гость {index} 😀", phone=f"+7999123{index:04d}",
            comment="Текст с кавычками \" и строкой\n=SUM(1,2)", photo=b"fake-photo-" + bytes([index]),
            photo_file_id="telegram-photo", actor_id=101, entry_status="closed", entry_reason="Причина")

    def test_full_guest_roundtrip_and_unicode_hmac(self):
        guest = self.seed()
        self.assertEqual(guest, self.local.get_guest(guest.id))
        self.assertEqual(guest, self.client.get_guest(guest.id))
        self.assertEqual(guest, self.client.find_phone("8 (999) 123-00-01"))
        self.assertIsNone(self.client.get_guest(9999))
        self.assertIsNone(self.client.find_phone("+79990000000"))
        found = self.client.search_guests("Гость")
        self.assertEqual([guest.id], [item.id for item in found])
        self.assertEqual(b"", found[0].photo)
        self.assertEqual(1, self.client.count_search("😀"))
        updated = self.client.update_guest(guest.id, guest.version, 102, name="Новое имя",
                                          photo=b"new-photo", photo_file_id="new-file")
        self.assertEqual("Новое имя", updated.name)
        self.assertEqual(b"new-photo", updated.photo)
        self.assertNotIn(SECRET, repr(self.client))
        self.assertNotIn(URL, repr(self.client))
        self.assertFalse(hasattr(self.client, "path"))

    def test_duplicate_stale_and_permission_errors_are_typed_without_server_details(self):
        guest = self.seed()
        with self.assertRaises(DuplicatePhoneError) as duplicate:
            self.seed()
        changed = self.client.update_guest(guest.id, guest.version, 102, comment="Изменено")
        with self.assertRaises(StaleGuestError) as stale:
            self.client.update_guest(guest.id, guest.version, 101, comment="Потерянное изменение")
        with self.assertRaises(PermissionError) as forbidden:
            self.client.add_employee(104, 101)
        for failure in (duplicate, stale, forbidden):
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertNotIn("server details", str(failure.exception))
        self.assertEqual(changed, self.local.get_guest(guest.id))

    def test_ambiguous_mutation_retries_same_signed_operation_and_only_commits_once(self):
        self.transport.lose_after_commit.add("add_guest")
        guest = self.seed()
        calls = [(message, body) for message, body in self.transport.calls if message["action"] == "add_guest"]
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(1, self.local.count_search("Гость"))
        self.assertEqual(1, self.local.count_audit(guest.id))
        self.sleep.assert_called_once_with(1)
        self.client.get_guest(guest.id)
        self.client.get_guest(guest.id)
        reads = [message["operation_id"] for message, body in self.transport.calls if message["action"] == "get_guest"]
        self.assertEqual(2, len(set(reads)))

    def test_visits_summaries_overdue_and_durable_reminder_roundtrip(self):
        guest = self.seed()
        visit = self.client.start_visit(guest.id, guest.version, 101, now=NOW - timedelta(hours=25))
        active_card = self.client.get_guest(guest.id)
        with self.assertRaises(VisitStateError):
            self.client.start_visit(guest.id, active_card.version, 102, now=NOW)
        summary = self.client.get_visit_summary(guest.id, now=NOW)
        self.assertEqual(visit, summary.active)
        self.assertEqual(25 * 3600, summary.current_seconds)
        self.assertEqual([visit], self.client.list_visits(guest.id))
        overdue = self.client.overdue_visits(now=NOW)
        self.assertEqual(1, len(overdue))
        self.assertEqual(guest.id, overdue[0][0].id)
        self.assertEqual(b"", overdue[0][0].photo)
        self.assertFalse(self.client.reminder_sent(visit.id, 101))
        self.assertTrue(self.client.mark_reminder_sent(visit.id, 101, now=NOW))
        restarted = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=self.transport)
        self.assertTrue(restarted.reminder_sent(visit.id, 101))
        self.assertFalse(restarted.mark_reminder_sent(visit.id, 101, now=NOW))
        stopped = self.client.stop_visit(guest.id, visit.id, 102, now=NOW)
        self.assertEqual(NOW.isoformat(timespec="microseconds"), stopped.stopped_at)
        self.assertFalse(self.client.mark_reminder_sent(visit.id, 102, now=NOW))
        self.assertEqual([], self.client.overdue_visits(now=NOW + timedelta(days=1)))
        summary = self.client.get_visit_summary(guest.id, now=NOW)
        self.assertIsNone(summary.active)
        self.assertEqual(1, summary.completed_count)
        self.assertEqual(25 * 3600, summary.completed_seconds)
        self.assertEqual(0, summary.current_seconds)

    def test_access_profiles_do_not_grant_and_owner_is_persistent(self):
        self.client.remember_staff(104, "Работник", "worker")
        self.assertFalse(self.client.has_employee_access(104))
        self.assertIn("@worker", self.client.staff_label(104))
        self.assertTrue(self.client.add_employee(104, ADMIN))
        self.assertFalse(self.client.add_employee(104, ADMIN))
        self.assertTrue(self.client.has_employee_access(104))
        self.assertEqual(frozenset({101, 102, 104}), self.client.employee_ids())
        self.assertEqual(3, self.client.count_employees())
        self.assertEqual(1, len(self.client.list_employees(limit=1, offset=2)))
        self.assertIsNone(self.client.bootstrap_employee_access([ADMIN, 999], ADMIN))
        self.assertFalse(self.client.has_employee_access(999))
        self.client.remember_staff(104, "Работник", "")
        self.assertEqual("Сотрудник не указан", self.client.staff_label(None))

    def test_audit_and_deletion_roundtrip(self):
        guest = self.seed()
        self.client.update_guest(guest.id, guest.version, 102, entry_status="open", entry_reason="Вход разрешён")
        events = self.client.list_audit(guest.id)
        self.assertEqual(2, self.client.count_audit(guest.id))
        self.assertEqual(events[0], self.client.get_audit(events[0].id))
        self.assertEqual("closed", events[0].changes["entry_status"]["before"])
        self.assertIsNone(self.client.get_audit(9999))
        current = self.client.get_guest(guest.id)
        self.assertTrue(self.client.delete_guest(guest.id, current.version, 102))
        self.assertIsNone(self.client.get_guest(guest.id))
        self.assertEqual("delete", self.client.list_audit(guest.id)[0].action)
        self.assertEqual(3, self.client.count_audit())

    def test_metadata_snapshot_has_no_original_photos_and_photo_refs_stay_internal(self):
        guest = self.seed()
        self.client.start_visit(guest.id, guest.version, 101, now=NOW - timedelta(hours=1))
        result = self.client.snapshot(now=NOW)
        self.assertEqual(NOW, datetime.fromisoformat(result["generated_at"]))
        self.assertEqual("", result["guests"][0]["photo"])
        self.assertNotIn("photo_ref", result["guests"][0])
        self.assertEqual(1, len(result["visits"]))
        self.assertFalse(any(message["action"] == "get_photo" for message, body in self.transport.calls))
        self.assertFalse(next(message for message, body in reversed(self.transport.calls)
                              if message["action"] == "snapshot")["params"]["include_photos"])

    def test_photo_snapshot_fetches_and_transforms_one_original_at_a_time(self):
        first, second = self.seed(1), self.seed(2)
        timeline = []
        original_transport = self.transport
        def observed(url, body, timeout):
            action = json.loads(json.loads(body)["payload"])["action"]
            if action == "get_photo":
                timeline.append("fetch")
            return original_transport(url, body, timeout)
        self.client._transport = observed
        def thumbnail(data):
            timeline.append("transform")
            self.assertIn(data, (first.photo, second.photo))
            return b"thumb-" + data[-1:]
        result = self.client.snapshot(include_photos=True, now=NOW, photo_transform=thumbnail)
        self.assertEqual(["fetch", "transform", "fetch", "transform"], timeline)
        self.assertEqual([b"thumb-\x01", b"thumb-\x02"],
                         [base64.b64decode(row["photo"]) for row in result["guests"]])

    def test_missing_snapshot_photo_fails_whole_export(self):
        self.seed()
        self.transport.hooks["get_photo"] = lambda message: response("")
        with self.assertRaises(SheetsStorageError):
            self.client.snapshot(include_photos=True, now=NOW)

    def test_snapshot_pins_old_photo_even_when_card_changes_between_reads(self):
        guest = self.seed()
        original = self.transport
        changed = False
        def concurrent(url, body, timeout):
            nonlocal changed
            action = json.loads(json.loads(body)["payload"])["action"]
            if action == "get_photo" and not changed:
                self.local.update_guest(guest.id, guest.version, 102, photo=b"changed", photo_file_id="changed")
                changed = True
            return original(url, body, timeout)
        self.client._transport = concurrent
        result = self.client.snapshot(include_photos=True, now=NOW)
        self.assertEqual(guest.photo, base64.b64decode(result["guests"][0]["photo"]))
        self.assertEqual(b"changed", self.local.get_guest(guest.id).photo)

    def test_backup_metadata_is_returned_without_a_fake_local_database_path(self):
        result = self.client.create_backup()
        self.assertEqual("private-backup-id", result["folder_id"])
        self.assertFalse(hasattr(self.client, "path"))
        self.transport.hooks["create_backup"] = lambda message: response({"complete": False})
        with self.assertRaises(SheetsStorageError):
            self.client.create_backup()

    def test_stage_photo_and_restore_send_validated_private_rpc_parameters(self):
        captured = {}
        def staged(message):
            captured["stage"] = message["params"]
            return response({"photo_ref": "private-staged-photo-1", "bytes": len(b"photo")})
        self.transport.hooks["stage_photo"] = staged
        self.assertEqual("private-staged-photo-1", self.client.stage_photo(b"photo")["photo_ref"])
        self.assertEqual(base64.b64encode(b"photo").decode(), captured["stage"]["photo"])
        self.assertEqual(ADMIN, captured["stage"]["admin_id"])
        def restored(message):
            captured["restore"] = message["params"]
            return response({"restored": True, "guests": 2, "visits": 3, "employees": 3, "audit_events": 5})
        self.transport.hooks["restore_backup"] = restored
        self.assertTrue(self.client.restore_backup("private-manifest-file")["restored"])
        self.assertEqual({"admin_id": ADMIN, "manifest_file_id": "private-manifest-file"}, captured["restore"])
        self.client.create_backup(only_if_due=False)
        self.assertFalse(self.transport.calls[-1][0]["params"]["only_if_due"])
        with self.assertRaises(ValueError):
            self.client.stage_photo(b"")
        with self.assertRaises(ValueError):
            self.client.restore_backup("https://evil.example/manifest")
        with self.assertRaises(ValueError):
            self.client.create_backup(only_if_due=1)

    def test_incomplete_staging_and_restore_are_never_success(self):
        for staged in ({"photo_ref": "private-photo", "bytes": 100}, {"photo_ref": "https://evil.example", "bytes": 5}):
            self.transport.hooks["stage_photo"] = lambda message, staged=staged: response(staged)
            with self.subTest(staged=staged), self.assertRaises(SheetsStorageError):
                self.client.stage_photo(b"photo")
        for restored in ({"restored": False, "guests": 0, "visits": 0, "employees": 0, "audit_events": 0},
                         {"restored": True, "guests": -1, "visits": 0, "employees": 0, "audit_events": 0}):
            self.transport.hooks["restore_backup"] = lambda message, restored=restored: response(restored)
            with self.subTest(restored=restored), self.assertRaises(SheetsStorageError):
                self.client.restore_backup("private-manifest-file")

    def test_import_empty_destination_preserves_three_workers_and_original_source(self):
        destination = Store(self.runtime / "import-target.sqlite3")
        transport = LocalTransport(destination)
        client = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=transport)
        state = {"guests": [], "visits": [], "audit_events": [], "staff": [], "visit_reminders": [],
                 "employee_access": [{"user_id": actor, "added_by": ADMIN, "added_at": NOW.isoformat()}
                                     for actor in (201, 202, 203)]}
        original = copy.deepcopy(state)
        sequences = {"guests": 10, "visits": 9, "audit_events": 7}
        def imported(message):
            incoming = message["params"]["state"]
            self.assertEqual(ADMIN, message["params"]["admin_id"])
            self.assertEqual(sequences, message["params"]["sequences"])
            self.assertEqual(0, destination.count_employees())
            destination.bootstrap_employee_access([row["user_id"] for row in incoming["employee_access"]], ADMIN)
            return response({"imported": True, "guests": 0, "visits": 0, "employees": 3, "audit_events": 0})
        transport.hooks["import_state"] = imported
        result = client.import_state(state, sequences=sequences)
        self.assertEqual(3, result["employees"])
        self.assertEqual(frozenset({201, 202, 203}), client.employee_ids())
        self.assertEqual(original, state)
        self.assertEqual({"guests": 10, "visits": 9, "audit_events": 7}, sequences)
        self.assertFalse(client.has_employee_access(ADMIN))

    def test_import_conflict_and_count_mismatch_never_acknowledge_success(self):
        state = {"guests": [], "visits": [], "audit_events": [], "staff": [],
                 "employee_access": [], "visit_reminders": []}
        before = self.local.employee_ids()
        self.transport.hooks["import_state"] = lambda message: json.dumps({
            "ok": False, "error": {"code": "CONFLICT", "message": "Private source " + SECRET},
        }).encode()
        with self.assertRaises(SheetsStorageError) as failure:
            self.client.import_state(state)
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertEqual(before, self.local.employee_ids())
        for value in ({"imported": False, "guests": 0, "visits": 0, "employees": 0, "audit_events": 0},
                      {"imported": True, "guests": 1, "visits": 0, "employees": 0, "audit_events": 0},
                      {"imported": True, "guests": 0, "visits": 0, "employees": True, "audit_events": 0}):
            self.transport.hooks["import_state"] = lambda message, value=value: response(value)
            with self.subTest(value=value), self.assertRaises(SheetsStorageError):
                self.client.import_state(state)
        with self.assertRaises(ValueError):
            self.client.import_state({"guests": []})
        for sequence in ({"guests": 1}, {"guests": True, "visits": 0, "audit_events": 0},
                         {"guests": -1, "visits": 0, "audit_events": 0}):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                self.client.import_state(state, sequences=sequence)

    def test_snapshot_rejects_unknown_photos_duplicate_ids_and_orphan_visits(self):
        guest = wire(self.seed())
        guest["photo"] = ""
        guest["photo_ref"] = "private-photo-reference-1"
        valid = {"generated_at": NOW.isoformat(), "guests": [guest], "visits": []}
        orphan = {"id": 1, "guest_id": 999, "started_at": NOW.isoformat(),
                  "stopped_at": None, "started_by": 101, "stopped_by": None}
        bad_results = [
            {**valid, "guests": [guest, dict(guest)]},
            {**valid, "guests": [{**guest, "photo_ref": "https://evil.example/file"}]},
            {**valid, "guests": [{**guest, "photo": base64.b64encode(b"unexpected").decode()}]},
            {**valid, "visits": [orphan]},
            {**valid, "visits": [{**orphan, "guest_id": guest["id"]},
                                  {**orphan, "id": 2, "guest_id": guest["id"]}]},
            {**valid, "generated_at": (NOW - timedelta(days=1)).isoformat()},
        ]
        for value in bad_results:
            self.transport.hooks["snapshot"] = lambda message, value=value: response(value)
            with self.subTest(value_fields=list(value)), self.assertRaises(SheetsStorageError):
                self.client.snapshot(include_photos=True, now=NOW)
        self.assertFalse(any(message["action"] == "get_photo" for message, body in self.transport.calls))

    def test_snapshot_tolerates_only_javascript_millisecond_time_normalization(self):
        instant = NOW.replace(microsecond=123456)
        normalized = instant.replace(microsecond=123000)
        self.transport.hooks["snapshot"] = lambda message: response({
            "generated_at": normalized.isoformat().replace("+00:00", "Z"), "guests": [], "visits": [],
        })
        result = self.client.snapshot(now=instant)
        self.assertEqual(normalized, datetime.fromisoformat(result["generated_at"]))
        with self.assertRaises(SheetsStorageError):
            self.client.snapshot(now=instant + timedelta(milliseconds=1))

    def test_bad_inputs_fail_before_transport_and_never_validate_unicode_ids_as_integers(self):
        before = len(self.transport.calls)
        for value in (True, 0, -1, 1.5, "101", 2**53):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.client.has_employee_access(value)
        for kwargs in ({"limit": True}, {"limit": 101}, {"offset": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client.search_guests("name", **kwargs)
        for fields in ({"unknown": "value"}, {"photo": b""}, {"comment": "x" * 3001}):
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError):
                self.client.update_guest(1, 1, 101, **fields)
        with self.assertRaises(ValueError):
            self.client.bootstrap_employee_access([float(2**53)], ADMIN)
        with self.assertRaises(ValueError):
            self.client.get_visit_summary(1, now=datetime(2026, 11, 6))
        self.assertEqual(before, len(self.transport.calls))

    def test_wrong_entity_identity_fails_closed(self):
        guest = self.seed()
        self.transport.hooks["get_guest"] = lambda message: response(self.local.get_guest(guest.id))
        with self.assertRaises(SheetsStorageError):
            self.client.get_guest(guest.id + 100)
        self.transport.hooks["find_phone"] = lambda message: response(self.local.get_guest(guest.id))
        with self.assertRaises(SheetsStorageError):
            self.client.find_phone("+79990000000")

    def test_malformed_data_never_becomes_zero_empty_or_partial_results(self):
        for value in (False, -1, "0", None, 1.5, 2**53):
            self.transport.hooks["count_search"] = lambda message, result=value: response(result)
            with self.subTest(value=value), self.assertRaises(SheetsStorageError):
                self.client.count_search("name")
        self.transport.hooks["search_guests"] = lambda message: response([{}])
        with self.assertRaises(SheetsStorageError):
            self.client.search_guests("name")
        for raw in (b"not json", b"\xff", b"{}", b'{"ok":true}', b'{"ok":1,"result":0}',
                    b'{"ok":true,"result":NaN}', b'{"ok":false,"error":"private"}'):
            self.transport.hooks["count_search"] = lambda message, raw=raw: raw
            with self.subTest(raw=raw), self.assertRaises(SheetsStorageError):
                self.client.count_search("name")

    def test_guest_and_visit_decoders_reject_corrupt_timestamps_and_binary(self):
        guest = wire(self.seed())
        for field, value in (("photo", "not-base64"), ("created_at", "2026-11-06"),
                             ("id", True), ("phone_key", "wrong"), ("entry_status", "unknown")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_guest({**guest, field: value})
        visit = {"id": 1, "guest_id": 1, "started_at": NOW.isoformat(),
                 "stopped_at": None, "started_by": 101, "stopped_by": None}
        for field, value in (("started_at", "bad"), ("stopped_by", 102), ("id", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_visit({**visit, field: value})


class TransportSafetyTests(unittest.TestCase):
    def make_client(self, after_health):
        transport = Mock(side_effect=[response(HEALTH), *after_health])
        return SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=transport), transport

    def test_endpoint_and_secret_validation_never_call_untrusted_hosts(self):
        for url in ("http://script.google.com/macros/s/id/exec", "https://evil.example/macros/s/id/exec",
                    "https://script.google.com.evil.example/macros/s/id/exec", URL + "?secret=private",
                    URL + "#fragment", URL.replace("/exec", "/dev"), URL.replace("https://", "https://user:password@"),
                    URL.replace("script.google.com", "script.google.com:444")):
            transport = Mock()
            with self.subTest(url=url), self.assertRaises(ValueError):
                SheetsStore(url, SECRET, expected_owner_email=OWNER, transport=transport)
            transport.assert_not_called()
        with self.assertRaises(ValueError):
            SheetsStore(URL, "short", expected_owner_email=OWNER, transport=Mock())

    def test_health_checks_owner_admin_schema_and_readiness(self):
        for change in ({"ready": False}, {"version": True}, {"schema_version": 2},
                       {"owner_email": "other@example.com"}, {"owner_email": 123},
                       {"admin_id": 101}, {"admin_id": True}):
            with self.subTest(change=change), self.assertRaises(SheetsStorageError):
                SheetsStore(URL, SECRET, expected_owner_email=OWNER,
                            transport=Mock(return_value=response({**HEALTH, **change})))

    def test_owner_must_be_supplied_before_any_http_request(self):
        transport = Mock()
        with self.assertRaises(TypeError):
            SheetsStore(URL, SECRET, transport=transport)
        with self.assertRaises(ValueError):
            SheetsStore(URL, SECRET, expected_owner_email="", transport=transport)
        transport.assert_not_called()

    def test_network_exhaustion_is_bounded_and_never_echoes_private_exception(self):
        private = SECRET + " private guest phone +79991234567"
        client, transport = self.make_client([error.URLError(private)] * 3)
        with patch("sheets_store.time.sleep") as sleep, self.assertRaises(SheetsStorageError) as failure:
            client.count_employees()
        self.assertEqual(4, transport.call_count)
        self.assertEqual([1, 2], [item.args[0] for item in sleep.call_args_list])
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertNotIn("+79991234567", str(failure.exception))
        self.assertEqual(transport.call_args_list[1].args[1], transport.call_args_list[3].args[1])

    def test_long_operations_wait_for_script_and_do_not_repeat_ambiguous_timeout(self):
        state = {"guests": [], "visits": [], "audit_events": [], "staff": [],
                 "employee_access": [], "visit_reminders": []}
        for action in ("create_backup", "import_state", "restore_backup"):
            client, transport = self.make_client([TimeoutError("Private upstream " + SECRET)])
            with self.subTest(action=action), patch("sheets_store.time.sleep") as sleep, \
                    self.assertRaises(SheetsStorageError) as failure:
                if action == "import_state":
                    client.import_state(state)
                elif action == "restore_backup":
                    client.restore_backup("private-manifest-file")
                else:
                    client.create_backup()
            self.assertEqual(2, transport.call_count, "Exactly one health call and one long request")
            self.assertEqual(300.0, transport.call_args.args[2])
            self.assertNotIn(SECRET, str(failure.exception))
            sleep.assert_not_called()

    def test_http_transient_retry_and_permanent_no_retry(self):
        client, transport = self.make_client([error.HTTPError(URL, 503, "private", {}, None), response(3)])
        with patch("sheets_store.time.sleep") as sleep:
            self.assertEqual(3, client.count_employees())
        sleep.assert_called_once_with(1.0)
        self.assertEqual(3, transport.call_count)
        client, transport = self.make_client([error.HTTPError(URL, 403, "private", {}, None)])
        with patch("sheets_store.time.sleep") as sleep, self.assertRaises(SheetsStorageError):
            client.count_employees()
        sleep.assert_not_called()
        self.assertEqual(2, transport.call_count)

    def test_rate_limit_respects_retry_after_or_defers_without_premature_retry(self):
        limited = error.HTTPError(URL, 429, "private", {"Retry-After": "5"}, None)
        client, transport = self.make_client([limited, response(0)])
        with patch("sheets_store.time.sleep") as sleep:
            self.assertEqual(0, client.count_employees())
        sleep.assert_called_once_with(5.0)
        limited = error.HTTPError(URL, 429, "private", {"Retry-After": "300"}, None)
        client, transport = self.make_client([limited])
        with patch("sheets_store.time.sleep") as sleep, self.assertRaises(SheetsStorageError):
            client.count_employees()
        sleep.assert_not_called()
        self.assertEqual(2, transport.call_count)

    def test_redirect_only_allows_google_contentservice_get_without_post_body(self):
        handler = _GoogleRedirects()
        outgoing = request.Request(URL, data=b"private-payload", method="POST")
        target = "https://script.googleusercontent.com/macros/echo?user_content_key=temporary"
        redirected = handler.redirect_request(outgoing, None, 302, "Found", {}, target)
        self.assertEqual("GET", redirected.get_method())
        self.assertIsNone(redirected.data)
        for target in ("https://evil.example/", "http://script.googleusercontent.com/macros/echo",
                       "https://accounts.google.com/login", "https://script.googleusercontent.com/wrong",
                       "https://script.googleusercontent.com.evil.example/macros/echo"):
            with self.subTest(target=target), self.assertRaises(SheetsStorageError):
                handler.redirect_request(outgoing, None, 302, "Found", {}, target)
        with self.assertRaises(SheetsStorageError):
            handler.redirect_request(outgoing, None, 307, "Found", {},
                                     "https://script.googleusercontent.com/macros/echo")

    def test_http_transport_bounds_reads_and_rejects_wrong_final_url(self):
        reply = Mock()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        reply.geturl.return_value = URL
        reply.read.return_value = b"x" * 33
        opener = Mock()
        opener.open.return_value = reply
        with patch("sheets_store.request.build_opener", return_value=opener), \
                patch("sheets_store.MAX_RESPONSE_BYTES", 32), self.assertRaises(SheetsStorageError):
            _http_post(URL, b"private", 40)
        reply.read.assert_called_once_with(33)
        reply.reset_mock()
        reply.geturl.return_value = "https://evil.example/"
        with patch("sheets_store.request.build_opener", return_value=opener), self.assertRaises(SheetsStorageError):
            _http_post(URL, b"private", 40)
        reply.read.assert_not_called()

    def test_service_errors_unknown_or_auth_fail_closed(self):
        for code in ("AUTH", "NOT_READY", "LIMIT", "UNKNOWN"):
            raw = json.dumps({"ok": False, "error": {"code": code, "message": SECRET}}).encode()
            client, transport = self.make_client([raw])
            with self.subTest(code=code), self.assertRaises(SheetsStorageError) as failure:
                client.count_employees()
            self.assertNotIn(SECRET, str(failure.exception))


if __name__ == "__main__":
    unittest.main()
