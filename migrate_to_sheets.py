"""Explicit offline migration from a preserved SQLite copy to Google Sheets.

The default invocation is a local dry run. Writes require --execute and the
operator's explicit --expected-guests count. Use a completed SQLite online
backup, not a live file copied without its WAL. The source is opened immutable
and read-only, never passed through Store or upgraded in place.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

from config import Config, load_config
from sheets_store import SheetsStorageError, SheetsStore, decode_guest, decode_visit
from storage import AuditEvent, EmployeeAccess, Guest, Visit, MAX_ACCESS_ID, MAX_PHOTO_BYTES


ADMIN_ID = 7011639945
MAX_STATE_BYTES = 50 * 1024 * 1024
MAX_IMPORT_REQUEST_BYTES = 16 * 1024 * 1024
STATE_KEYS = ("guests", "visits", "audit_events", "staff", "employee_access", "visit_reminders")
STAFF_FIELDS = ("actor_id", "name", "username", "updated_at")
REMINDER_FIELDS = ("visit_id", "recipient_id", "sent_at")
_SOURCE_ERROR = "Исходная копия базы недоступна, повреждена или содержит несовместимые данные."


class MigrationError(RuntimeError):
    """A safe migration prerequisite or verification failed."""


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    state: dict = field(repr=False)
    sequences: dict
    counts: dict
    source_sha256: str


def _integer(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_ACCESS_ID:
        raise ValueError("Invalid integer")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Timezone required")
    return value


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _no_pending_wal(path):
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise MigrationError("Нужна завершённая резервная копия SQLite: рядом есть неперенесённые записи WAL.")


def _rows(connection, table, fields, *, optional=False, defaults=None):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        if optional:
            return
        raise ValueError("Missing table")
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    defaults = defaults or {}
    if set(fields) - columns - defaults.keys():
        raise ValueError("Missing columns")
    existing = [name for name in fields if name in columns]
    # Bound raw values in SQLite before Python loads a possibly enormous BLOB
    # or legacy text field. The exact JSON budget is checked again per row.
    lengths = "+".join(f"COALESCE(length(CAST({name} AS BLOB)),0)" for name in existing)
    total = connection.execute(f"SELECT COALESCE(SUM({lengths}),0) FROM {table}").fetchone()[0]
    if total > MAX_STATE_BYTES:
        raise MigrationError("Полный перенос превышает безопасный размер 50 МиБ. Нужен отдельный план переноса; данные не обрезаны.")
    for row in connection.execute("SELECT " + ",".join(existing) + f" FROM {table} ORDER BY rowid"):
        values = dict(row)
        yield {name: values[name] if name in values else defaults[name] for name in fields}


def _valid_changes(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid audit changes")
    for key, change in value.items():
        if not isinstance(key, str) or not isinstance(change, dict) or set(change) != {"before", "after"}:
            raise ValueError("Invalid audit change")
        for item in change.values():
            if item is not None and type(item) not in (str, int, float, bool):
                raise ValueError("Invalid audit value")
            if type(item) is float and not math.isfinite(item):
                raise ValueError("Nonfinite audit value")


def read_source(source: Path | str, expected_guests: int) -> SourceSnapshot:
    """Read and validate one immutable snapshot without creating any files."""
    _integer(expected_guests, minimum=0)
    path = Path(source).expanduser().resolve()
    try:
        if not path.is_file():
            raise MigrationError(_SOURCE_ERROR)
        _no_pending_wal(path)
        fingerprint = _hash_file(path)
        uri = path.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if [tuple(row) for row in connection.execute("PRAGMA integrity_check")] != [("ok",)]:
                raise ValueError("Integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Foreign keys failed")
            state = {key: [] for key in STATE_KEYS}
            # Canonical JSON's measured size bounds the complete import, not
            # just photographs. No section is clipped to fit a quota.
            size = len(json.dumps(state, separators=(",", ":")).encode("utf-8"))

            def append(section, row):
                nonlocal size
                size += len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) + 1
                if size > MAX_STATE_BYTES:
                    raise MigrationError("Полный перенос превышает безопасный размер 50 МиБ. Нужен отдельный план переноса; данные не обрезаны.")
                state[section].append(row)

            actual_guests = connection.execute("SELECT COUNT(*) FROM guests").fetchone()[0]
            if actual_guests != expected_guests:
                raise MigrationError(f"Число карточек не совпало: ожидалось {expected_guests}, найдено {actual_guests}. Перенос остановлен.")
            guest_ids, phone_keys = set(), set()
            for row in _rows(connection, "guests", tuple(Guest.__dataclass_fields__),
                             defaults={"entry_status": "open", "entry_reason": ""}):
                if not isinstance(row["photo"], bytes) or not 0 < len(row["photo"]) <= MAX_PHOTO_BYTES:
                    raise ValueError("Invalid photo")
                wire = {**row, "photo": base64.b64encode(row["photo"]).decode("ascii")}
                guest = decode_guest(wire, require_photo=True)
                if guest.id in guest_ids or guest.phone_key in phone_keys:
                    raise ValueError("Duplicate guest identity")
                guest_ids.add(guest.id)
                phone_keys.add(guest.phone_key)
                append("guests", wire)

            visit_ids, active_guests = set(), set()
            for row in _rows(connection, "visits", tuple(Visit.__dataclass_fields__), optional=True):
                visit = decode_visit(row)
                if visit.id in visit_ids or visit.guest_id not in guest_ids:
                    raise ValueError("Invalid visit reference")
                if visit.stopped_at is None:
                    if visit.guest_id in active_guests:
                        raise ValueError("Multiple active visits")
                    active_guests.add(visit.guest_id)
                visit_ids.add(visit.id)
                append("visits", row)

            audit_ids = set()
            for row in _rows(connection, "audit_events", tuple(AuditEvent.__dataclass_fields__), optional=True):
                for key in ("id", "guest_id"):
                    _integer(row[key])
                if row["actor_id"] is not None:
                    _integer(row["actor_id"])
                _timestamp(row["created_at"])
                if row["id"] in audit_ids or not isinstance(row["guest_name"], str) or not row["guest_name"]:
                    raise ValueError("Invalid audit identity")
                if row["action"] not in {"add", "update", "delete", "start", "stop"}:
                    raise ValueError("Invalid audit action")
                row["changes"] = json.loads(row["changes"])
                _valid_changes(row["changes"])
                audit_ids.add(row["id"])
                append("audit_events", row)

            staff_ids = set()
            for row in _rows(connection, "staff", STAFF_FIELDS, optional=True):
                actor = _integer(row["actor_id"])
                _timestamp(row["updated_at"])
                if (actor in staff_ids or not isinstance(row["name"], str) or not row["name"].strip()
                        or len(row["name"]) > 200 or "\0" in row["name"]
                        or not isinstance(row["username"], str)
                        or row["username"] and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", row["username"])):
                    raise ValueError("Invalid staff profile")
                staff_ids.add(actor)
                append("staff", row)

            settings = []
            for row in _rows(connection, "access_settings", ("singleton", "admin_id"), optional=True):
                settings.append(row)
                if len(settings) > 1:
                    raise ValueError("Multiple access settings")
            if settings and (len(settings) != 1 or settings[0] != {"singleton": 1, "admin_id": ADMIN_ID}):
                raise MigrationError("Администратор исходной базы не совпадает с администратором нового хранилища.")
            employee_ids = set()
            for row in _rows(connection, "employee_access", tuple(EmployeeAccess.__dataclass_fields__), optional=True):
                target = _integer(row["user_id"])
                _integer(row["added_by"])
                _timestamp(row["added_at"])
                if target == ADMIN_ID or target in employee_ids or row["added_by"] != ADMIN_ID or not settings:
                    raise ValueError("Invalid access grant")
                employee_ids.add(target)
                append("employee_access", row)

            reminder_ids = set()
            for row in _rows(connection, "visit_reminders", REMINDER_FIELDS, optional=True):
                key = (_integer(row["visit_id"]), _integer(row["recipient_id"]))
                _timestamp(row["sent_at"])
                if key in reminder_ids or key[0] not in visit_ids:
                    raise ValueError("Invalid reminder reference")
                reminder_ids.add(key)
                append("visit_reminders", row)

            sequences = {key: 0 for key in ("guests", "visits", "audit_events")}
            for row in _rows(connection, "sqlite_sequence", ("name", "seq"), optional=True):
                if row["name"] in sequences:
                    sequences[row["name"]] = _integer(row["seq"], minimum=0)
            connection.rollback()
        _no_pending_wal(path)
        if _hash_file(path) != fingerprint:
            raise MigrationError("Исходная копия изменилась во время чтения. Нужна отдельная завершённая резервная копия.")
        return SourceSnapshot(state, sequences, {key: len(value) for key, value in state.items()}, fingerprint)
    except MigrationError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        raise MigrationError(_SOURCE_ERROR) from None


def _wire(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "__dataclass_fields__"):
        return _wire(asdict(value))
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def verify_import(remote, source: SourceSnapshot):
    """Verify complete cards/photos/visits, audit, employee IDs and old markers."""
    current = remote.snapshot(include_photos=True)
    for section in ("guests", "visits"):
        expected = sorted(source.state[section], key=lambda row: row["id"])
        actual = sorted(current[section], key=lambda row: row["id"])
        if actual != expected:
            raise MigrationError("Контрольная проверка переноса не пройдена. Не переключайте бота на новое хранилище.")
    events = []
    for offset in range(0, len(source.state["audit_events"]) + 1, 100):
        page = remote.list_audit(None, 100, offset)
        events.extend(_wire(page))
        if len(page) < 100:
            break
    if (remote.count_audit() != len(source.state["audit_events"])
            or sorted(events, key=lambda row: row["id"]) != sorted(source.state["audit_events"], key=lambda row: row["id"])):
        raise MigrationError("Контрольная проверка истории изменений не пройдена.")
    employees = []
    for offset in range(0, len(source.state["employee_access"]) + 1, 100):
        page = remote.list_employees(100, offset)
        employees.extend(_wire(page))
        if len(page) < 100:
            break
    if (remote.count_employees() != len(source.state["employee_access"])
            or sorted(employees, key=lambda row: row["user_id"]) != sorted(source.state["employee_access"], key=lambda row: row["user_id"])):
        raise MigrationError("Контрольная проверка прав сотрудников не пройдена.")
    for row in source.state["staff"]:
        expected = f'{row["name"]} (@{row["username"]})' if row["username"] else row["name"]
        if remote.staff_label(row["actor_id"]) != expected:
            raise MigrationError("Контрольная проверка сотрудников не пройдена.")
    for row in source.state["visit_reminders"]:
        if remote.reminder_sent(row["visit_id"], row["recipient_id"]) is not True:
            raise MigrationError("Контрольная проверка напоминаний не пройдена.")


def _import_metadata(source: SourceSnapshot) -> dict:
    """Preflight the bounded metadata request before uploading any photograph."""
    metadata = copy.deepcopy(source.state)
    for row in metadata["guests"]:
        del row["photo"]
        # The bridge accepts references up to 200 ASCII characters. Reserving
        # that maximum makes this bound independent of actual Drive IDs.
        row["photo_ref"] = "x" * 200
    for records in metadata.values():
        for row in records:
            for value in row.values():
                if value is None:
                    continue
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) if isinstance(value, dict) else str(value)
                # Google Sheets cells are limited separately from HTTP size.
                # Apps Script counts UTF-16 code units, including astral emoji.
                if len(text.encode("utf-16-le")) // 2 > 49000:
                    raise MigrationError("Запись превышает допустимый размер ячейки Google Таблицы. Перенос остановлен до загрузки фотографий.")
    payload = json.dumps({"version": 1, "operation_id": "0" * 32, "issued_at": MAX_ACCESS_ID,
                          "action": "import_state", "params": {"state": metadata, "admin_id": ADMIN_ID,
                                                                    "sequences": source.sequences}},
                         ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    envelope = json.dumps({"payload": payload, "signature": "0" * 64}, ensure_ascii=False, separators=(",", ":"))
    if len(envelope.encode("utf-8")) > MAX_IMPORT_REQUEST_BYTES:
        raise MigrationError("Метаданные превышают допустимый размер запроса Google. Нужен отдельный план переноса; данные не обрезаны.")
    return metadata


def migrate(source: Path | str, expected_guests: int, *, execute=False,
            config: Config | None = None, store_factory=None) -> dict:
    if type(execute) is not bool:
        raise MigrationError("Запись должна быть явно разрешена флагом --execute.")
    snapshot = read_source(source, expected_guests)
    report = {"mode": "execute" if execute else "dry_run", "counts": snapshot.counts,
              "source_sha256": snapshot.source_sha256, "source_unchanged": True}
    if not execute:
        return report
    config = config or load_config()
    owner_email = config.google_owner_email.strip()
    if (config.storage_backend != "google_sheets" or config.admin_user_id != ADMIN_ID
            or not owner_email):
        raise MigrationError("Для переноса требуется настроенное Google-хранилище выбранного владельца.")
    metadata = _import_metadata(snapshot)
    remote = (store_factory or SheetsStore)(config.sheets_endpoint, config.sheets_secret,
                                           expected_owner_email=owner_email, expected_admin_id=ADMIN_ID)
    # Each original fits an individual RPC. Metadata commits only after every
    # photograph has been uploaded successfully; the preserved source snapshot
    # remains untouched for the complete post-import comparison below.
    for original, prepared in zip(snapshot.state["guests"], metadata["guests"]):
        photo = base64.b64decode(original["photo"], validate=True)
        staged = remote.stage_photo(photo, admin_id=ADMIN_ID)
        prepared["photo_ref"] = staged["photo_ref"]
    remote.import_state(metadata, admin_id=ADMIN_ID, sequences=snapshot.sequences)
    verify_import(remote, snapshot)
    report["verified"] = True
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Перенос завершённой копии SQLite в пустую Google Таблицу.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-guests", type=int, required=True)
    parser.add_argument("--execute", action="store_true", help="Разрешить запись; без флага выполняется только локальная проверка.")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.source, args.expected_guests, execute=args.execute)
    except (MigrationError, SheetsStorageError, OSError, ValueError, RuntimeError):
        print("Перенос не выполнен или его результат не подтверждён. Источник не изменён. Проверьте копию, ожидаемое число гостей и доступ к пустому назначению.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
