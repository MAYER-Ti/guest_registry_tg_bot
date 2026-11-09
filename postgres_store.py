"""Private PostgreSQL registry, compatible with Supabase's two pooler modes.

Normal startup only verifies an existing registry. Use initialize_schema() once,
with an operator connection, before starting the bot. No SQL errors, connection
strings or record contents are included in public exceptions or repr().
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Iterable, Iterator

from storage import (
    AuditEvent, DuplicatePhoneError, EmployeeAccess, Guest, MAX_ACCESS_ID,
    MAX_PHOTO_BYTES, StaleGuestError, Visit, VisitStateError, VisitSummary,
    _EDITABLE_FIELDS, _card_changes, _restrict_permissions, _search_filter,
    _timestamp, _utc_datetime, _validate_access_id, _validate_actor,
    _validate_fields, _validate_identifier, _validate_page, normalize_name,
    normalize_phone,
)

SCHEMA_VERSION = 1
STATE_KEYS = ("guests", "visits", "audit_events", "staff", "employee_access", "visit_reminders")
DATA_TABLES = (*STATE_KEYS, "recovered_totals")
SEQUENCE_KEYS = ("guests", "visits", "audit_events")
_TABLE_FIELDS = {
    "registry_meta": ("singleton", "schema_version", "registry_id", "admin_id", "access_seeded"),
    "guests": (*Guest.__dataclass_fields__, "name_key"),
    "visits": tuple(Visit.__dataclass_fields__),
    "audit_events": tuple(AuditEvent.__dataclass_fields__),
    "staff": ("actor_id", "name", "username", "updated_at"),
    "employee_access": tuple(EmployeeAccess.__dataclass_fields__),
    "visit_reminders": ("visit_id", "recipient_id", "sent_at"),
    "recovered_totals": ("guest_id", "completed_count", "completed_seconds", "observed_at", "source_message_id"),
}
_UNAVAILABLE = "Не удалось обратиться к PostgreSQL. Данные не заменены пустой базой. Проверь подключение и попробуй позже."
_IDENTITY_ERROR = "Не удалось подтвердить владельца и структуру PostgreSQL. Запуск остановлен."
_STALE = "Карточка уже изменена или удалена. Откройте её заново."
_VISIT = "Этот отсчёт уже завершён или удалён. Откройте карточку заново."


class PostgresStorageError(RuntimeError):
    """A sanitized, fail-closed database error suitable for chat and logs."""


def _load_driver():
    try:
        driver = importlib.import_module("psycopg")
        return driver, importlib.import_module("psycopg.rows").dict_row
    except ImportError:
        raise PostgresStorageError("Для PostgreSQL установите зависимость psycopg[binary].") from None


def _registry_identity(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 200
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Укажите постоянный идентификатор картотеки PostgreSQL.")
    return value.strip()


def _options(database_url, sslmode, connect_timeout, sslrootcert=None):
    if not isinstance(database_url, str) or not database_url.strip() or "\0" in database_url:
        raise ValueError("Укажите строку подключения PostgreSQL.")
    if not isinstance(sslmode, str) or sslmode not in {"require", "verify-full"}:
        raise ValueError("PostgreSQL требует SSL: require или verify-full.")
    if type(connect_timeout) is not int or not 1 <= connect_timeout <= 60:
        raise ValueError("Время подключения должно быть от 1 до 60 секунд.")
    if sslrootcert is not None and (not isinstance(sslrootcert, str)
            or not sslrootcert.strip() or "\0" in sslrootcert or len(sslrootcert) > 4096):
        raise ValueError("Укажите файл корневого сертификата PostgreSQL или system.")
    options = dict(sslmode=sslmode, connect_timeout=connect_timeout,
                   prepare_threshold=None, application_name="guest-registry-bot")
    if sslrootcert is not None:
        options["sslrootcert"] = sslrootcert
    return options


def _int(value: object, *, minimum=1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_ACCESS_ID:
        raise ValueError("Некорректный числовой идентификатор.")
    return value


def _time(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Некорректное время.")
    _utc_datetime(datetime.fromisoformat(value))
    return value


def _object(value: object, fields: Iterable[str]) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Некорректная структура экспорта.")
    return value


def _photo(value: object) -> bytes:
    if isinstance(value, bytes):
        result = value
    elif isinstance(value, str) and len(value) <= ((MAX_PHOTO_BYTES + 2) // 3) * 4:
        try:
            result = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Некорректное фото.") from None
    else:
        raise ValueError("Некорректное фото.")
    if len(result) > MAX_PHOTO_BYTES:
        raise ValueError("Фото превышает 10 МБ.")
    return result


def _fields(*, allow_missing_photo=False, **values):
    missing = values.get("photo") == b"" and values.get("photo_file_id") == ""
    checked = _validate_fields(**{
        **values,
        "photo": b"\0" if allow_missing_photo and missing else values.get("photo"),
        "photo_file_id": "recovered-missing-photo" if allow_missing_photo and missing else values.get("photo_file_id"),
    })
    if any(isinstance(value, str) and "\0" in value for value in checked.values()):
        raise ValueError("Текст не должен содержать нулевой символ.")
    if allow_missing_photo and missing:
        checked.update(photo=b"", photo_file_id="")
    return checked


def decode_guest(value: object) -> Guest:
    """Validate a snapshot/import row, including an explicitly missing photo."""
    row = _object(value, Guest.__dataclass_fields__)
    photo = _photo(row["photo"])
    for field in ("id", "created_by", "updated_by", "version"):
        _int(row[field])
    for field in ("created_at", "updated_at"):
        _time(row[field])
    # A metadata-only report intentionally omits bytes but retains file_id.
    # Missing recovered photographs carry both an empty photo and file_id.
    checked = _fields(allow_missing_photo=True, **{
        field: (photo or (b"\0" if row["photo_file_id"] else b"")) if field == "photo" else row[field]
        for field in _EDITABLE_FIELDS
    })
    if row["phone_key"] != checked["phone_key"]:
        raise ValueError("Некорректный телефон карточки.")
    return Guest(**{**row, "photo": photo})


def decode_visit(value: object) -> Visit:
    row = _object(value, Visit.__dataclass_fields__)
    for field in ("id", "guest_id", "started_by"):
        _int(row[field])
    _time(row["started_at"])
    if (row["stopped_at"] is None) != (row["stopped_by"] is None):
        raise ValueError("Некорректный визит.")
    if row["stopped_at"] is not None:
        _time(row["stopped_at"])
        _int(row["stopped_by"])
        if datetime.fromisoformat(row["stopped_at"]) < datetime.fromisoformat(row["started_at"]):
            raise ValueError("Визит завершён раньше начала.")
    return Visit(**row)


def decode_recovered_totals(value: object) -> dict:
    """A residual historical baseline, never an invented set of dated visits.

    The importer must have subtracted every supplied exact visit completed at
    or before observed_at from the trustworthy source summary. Readers add
    this residual to all exact completed visit rows, including later visits.
    """
    row = dict(_object(value, _TABLE_FIELDS["recovered_totals"]))
    _int(row["guest_id"])
    _int(row["completed_count"], minimum=0)
    _int(row["completed_seconds"], minimum=0)
    _time(row["observed_at"])
    source = row["source_message_id"]
    if not isinstance(source, str) or not source.strip() or len(source) > 200 or "\0" in source:
        raise ValueError("Укажите исходное сообщение восстановленной сводки.")
    return row


def _guest(row) -> Guest | None:
    return None if row is None else Guest(**{key: bytes(row[key]) if key == "photo" else row[key]
                                           for key in Guest.__dataclass_fields__})


def _visit(row) -> Visit | None:
    return None if row is None else Visit(**{key: row[key] for key in Visit.__dataclass_fields__})


def _audit(row) -> AuditEvent | None:
    return None if row is None else AuditEvent(**{key: row[key] for key in AuditEvent.__dataclass_fields__})


def _wire(row):
    return {key: base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else value
            for key, value in row.items()}


def initialize_schema(database_url: str, *, registry_id: str, admin_id: int = 7011639945,
                      sslmode: str = "require", sslrootcert: str | None = None, connect_timeout: int = 15,
                      runtime_role: str | None = None) -> None:
    """Operator-only, atomic creation; refuse any pre-existing namespace.

    runtime_role must already exist. It receives data privileges and explicit
    RLS policies, without schema creation, role administration or BYPASSRLS.
    Create its login/password separately in the provider's secure console.
    """
    registry_id = _registry_identity(registry_id)
    _validate_access_id(admin_id)
    options = _options(database_url, sslmode, connect_timeout, sslrootcert)
    if runtime_role is not None and (not isinstance(runtime_role, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role)
            or runtime_role in {"anon", "authenticated", "public", "service_role"}):
        raise ValueError("Укажите отдельную роль PostgreSQL для бота.")
    driver, row_factory = _load_driver()
    try:
        with driver.connect(database_url, row_factory=row_factory, **options) as connection:
            # Transaction-scoped advisory locking works with both poolers.
            connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(164731, 1)")
            exists = connection.execute(
                "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'guest_registry'"
            ).fetchone()
            if exists is not None:
                raise PostgresStorageError("Схема guest_registry уже существует. Автоматическая замена запрещена.")
            if runtime_role is not None:
                role = connection.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (runtime_role,),
                ).fetchone()
                if role is None or role["rolsuper"] or role["rolbypassrls"]:
                    raise ValueError("Роль бота должна существовать без superuser и BYPASSRLS.")
            # No parameters: psycopg sends this fixed migration without prepare.
            connection.execute(Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8"), prepare=False)
            connection.execute("""INSERT INTO guest_registry.registry_meta
                (singleton, schema_version, registry_id, admin_id) VALUES (1, %s, %s, %s)""",
                (SCHEMA_VERSION, registry_id, admin_id))
            if runtime_role is not None:
                sql = importlib.import_module("psycopg.sql")
                role_name = sql.Identifier(runtime_role)
                connection.execute(sql.SQL("GRANT USAGE ON SCHEMA guest_registry TO {}").format(role_name))
                for table in STATE_KEYS:
                    connection.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON guest_registry.{} TO {}").format(sql.Identifier(table), role_name))
                    connection.execute(sql.SQL("CREATE POLICY bot_runtime ON guest_registry.{} TO {} USING (true) WITH CHECK (true)").format(sql.Identifier(table), role_name))
                connection.execute(sql.SQL("GRANT SELECT ON guest_registry.recovered_totals TO {}").format(role_name))
                connection.execute(sql.SQL("CREATE POLICY bot_runtime ON guest_registry.recovered_totals FOR SELECT TO {} USING (true)").format(role_name))
                connection.execute(sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA guest_registry TO {}").format(role_name))
                connection.execute(sql.SQL("GRANT SELECT ON guest_registry.registry_meta TO {}").format(role_name))
                connection.execute(sql.SQL("GRANT UPDATE (access_seeded) ON guest_registry.registry_meta TO {}").format(role_name))
                connection.execute(sql.SQL("CREATE POLICY bot_runtime ON guest_registry.registry_meta TO {} USING (true) WITH CHECK (true)").format(role_name))
    except driver.Error:
        raise PostgresStorageError(_UNAVAILABLE) from None


class PostgresStore:
    """Synchronous Store facade. Call from async code with asyncio.to_thread."""
    backend = "postgres"

    def __init__(self, database_url: str, *, expected_registry_id: str,
                 expected_admin_id: int = 7011639945, sslmode: str = "require", sslrootcert: str | None = None,
                 connect_timeout: int = 15):
        self._options = _options(database_url, sslmode, connect_timeout, sslrootcert)
        self._registry_id = _registry_identity(expected_registry_id)
        _validate_access_id(expected_admin_id)
        self._admin_id = expected_admin_id
        self._database_url = database_url
        self._driver, self._row_factory = _load_driver()
        with self._connection(read_only=True) as connection:
            columns = connection.execute("""SELECT table_name, column_name
                FROM information_schema.columns WHERE table_schema = 'guest_registry'""").fetchall()
            actual = {table: set() for table in _TABLE_FIELDS}
            for row in columns:
                if row["table_name"] in actual:
                    actual[row["table_name"]].add(row["column_name"])
            if any(actual[table] != set(fields) for table, fields in _TABLE_FIELDS.items()):
                raise PostgresStorageError(_IDENTITY_ERROR)
            secured = connection.execute("""SELECT c.relname, c.relrowsecurity
                FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='guest_registry' AND c.relkind='r'""").fetchall()
            if ({row["relname"] for row in secured} != set(_TABLE_FIELDS)
                    or not all(row["relrowsecurity"] for row in secured)):
                raise PostgresStorageError(_IDENTITY_ERROR)
            # These constraints are part of concurrency correctness, rather
            # than optional performance indexes. Reject a damaged migration.
            indexes = connection.execute("""SELECT t.relname AS table_name,
                x.relname AS index_name, i.indisunique, i.indisvalid,
                i.indnkeyatts, i.indnatts, a.attname AS column_name,
                pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS predicate
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
                JOIN pg_catalog.pg_class t ON t.oid=i.indrelid
                JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
                JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[0]
                WHERE n.nspname='guest_registry'
                AND x.relname IN ('guests_phone_key_key', 'visits_one_active_per_guest')""").fetchall()
            expected = {"guests_phone_key_key": ("guests", "phone_key", ""),
                        "visits_one_active_per_guest": ("visits", "guest_id", "stopped_atisnull")}
            if len(indexes) != len(expected):
                raise PostgresStorageError(_IDENTITY_ERROR)
            for row in indexes:
                predicate = re.sub(r"[()\s]", "", row["predicate"] or "").casefold()
                if (not row["indisunique"] or not row["indisvalid"]
                        or row["indnkeyatts"] != 1 or row["indnatts"] != 1
                        or (row["table_name"], row["column_name"], predicate) != expected[row["index_name"]]):
                    raise PostgresStorageError(_IDENTITY_ERROR)

    def __repr__(self):
        return "PostgresStore(backend='postgres')"

    @contextmanager
    def _connection(self, *, read_only=False) -> Iterator:
        try:
            with self._driver.connect(self._database_url, row_factory=self._row_factory,
                                      **self._options) as connection:
                if read_only:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                else:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
                connection.execute("SET LOCAL lock_timeout = '15s'")
                connection.execute("SET LOCAL statement_timeout = '60s'")
                connection.execute("SET LOCAL standard_conforming_strings = on")
                row = connection.execute("""SELECT schema_version, registry_id, admin_id
                    FROM guest_registry.registry_meta WHERE singleton=1""").fetchone()
                if (row is None or row["schema_version"] != SCHEMA_VERSION
                        or row["registry_id"] != self._registry_id or row["admin_id"] != self._admin_id):
                    raise PostgresStorageError(_IDENTITY_ERROR)
                yield connection
        except self._driver.Error as exc:
            constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
            if getattr(exc, "sqlstate", None) == "23505" and constraint == "guests_phone_key_key":
                raise DuplicatePhoneError("Гость с таким телефоном уже есть.") from None
            if getattr(exc, "sqlstate", None) == "23505" and constraint == "visits_one_active_per_guest":
                raise VisitStateError("Отсчёт времени для этого гостя уже запущен.") from None
            raise PostgresStorageError(_UNAVAILABLE) from None

    @staticmethod
    def _record_audit(connection, guest_id, guest_name, actor_id, action, changes, created_at):
        # Allocate history IDs in commit order even for unrelated guests. This
        # lock is acquired last and released at commit, with no later card locks.
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(164731, 2)")
        connection.execute("""INSERT INTO guest_registry.audit_events
            (guest_id, guest_name, actor_id, action, changes, created_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)""",
            (guest_id, guest_name, actor_id, action,
             json.dumps(changes, ensure_ascii=False, separators=(",", ":"), allow_nan=False), created_at))

    def add_guest(self, *, name, phone, comment, photo, photo_file_id, actor_id,
                  entry_status="open", entry_reason="") -> Guest:
        _validate_actor(actor_id)
        values = _fields(name=name, phone=phone, comment=comment, photo=photo,
                         photo_file_id=photo_file_id, entry_status=entry_status, entry_reason=entry_reason)
        moment = _timestamp()
        values.update(created_by=actor_id, updated_by=actor_id, created_at=moment, updated_at=moment)
        columns = tuple(values)
        with self._connection() as connection:
            result = _guest(connection.execute("INSERT INTO guest_registry.guests (" + ", ".join(columns)
                + ") VALUES (" + ", ".join(["%s"] * len(columns)) + ") RETURNING *",
                tuple(values[key] for key in columns)).fetchone())
            self._record_audit(connection, result.id, result.name, actor_id, "add", _card_changes(None, result), moment)
        return result

    def get_guest(self, id: int) -> Guest | None:
        _validate_identifier(id)
        with self._connection(read_only=True) as connection:
            return _guest(connection.execute("SELECT * FROM guest_registry.guests WHERE id=%s", (id,)).fetchone())

    def find_phone(self, phone: str) -> Guest | None:
        key = normalize_phone(phone)
        with self._connection(read_only=True) as connection:
            return _guest(connection.execute("SELECT * FROM guest_registry.guests WHERE phone_key=%s", (key,)).fetchone())

    def search_guests(self, query: str, limit=8, offset=0) -> list[Guest]:
        _validate_page(limit, offset)
        where, params = _search_filter(query)
        where = "FALSE" if where == "0" else where.replace("?", "%s")
        columns = ", ".join("''::bytea AS photo" if field == "photo" else field for field in Guest.__dataclass_fields__)
        with self._connection(read_only=True) as connection:
            rows = connection.execute(f"SELECT {columns} FROM guest_registry.guests WHERE {where} ORDER BY name_key, id LIMIT %s OFFSET %s",
                                      [*params, limit, offset]).fetchall()
        return [_guest(row) for row in rows]

    def count_search(self, query: str) -> int:
        where, params = _search_filter(query)
        where = "FALSE" if where == "0" else where.replace("?", "%s")
        with self._connection(read_only=True) as connection:
            return connection.execute(f"SELECT COUNT(*) AS n FROM guest_registry.guests WHERE {where}", params).fetchone()["n"]

    def update_guest(self, id: int, expected_version: int, actor_id: int, **fields) -> Guest:
        for value in (id, expected_version):
            _validate_identifier(value)
        _validate_actor(actor_id)
        if not fields or fields.keys() - _EDITABLE_FIELDS:
            raise ValueError("Укажите только изменяемые поля карточки.")
        with self._connection() as connection:
            current = _guest(connection.execute("SELECT * FROM guest_registry.guests WHERE id=%s FOR UPDATE", (id,)).fetchone())
            if current is None or current.version != expected_version:
                raise StaleGuestError(_STALE)
            values = {field: fields.get(field, getattr(current, field)) for field in _EDITABLE_FIELDS}
            # Ordinary edits keep a recovered missing photo. Uploading/replacing
            # a photo still requires both complete, nonempty photo fields.
            values = _fields(allow_missing_photo=not ({"photo", "photo_file_id"} & fields.keys()), **values)
            values.update(updated_by=actor_id, updated_at=_timestamp())
            assignments = ", ".join(f"{field}=%s" for field in values)
            result = _guest(connection.execute(f"UPDATE guest_registry.guests SET {assignments}, version=version+1 WHERE id=%s RETURNING *",
                                               [*values.values(), id]).fetchone())
            self._record_audit(connection, id, result.name, actor_id, "update", _card_changes(current, result), result.updated_at)
        return result

    def delete_guest(self, id: int, expected_version: int, actor_id: int | None = None) -> bool:
        for value in (id, expected_version):
            _validate_identifier(value)
        if actor_id is not None:
            _validate_actor(actor_id)
        with self._connection() as connection:
            current = _guest(connection.execute("SELECT * FROM guest_registry.guests WHERE id=%s FOR UPDATE", (id,)).fetchone())
            if current is None or current.version != expected_version:
                raise StaleGuestError(_STALE)
            connection.execute("DELETE FROM guest_registry.guests WHERE id=%s", (id,))
            self._record_audit(connection, id, current.name, actor_id, "delete", _card_changes(current, None), _timestamp())
        return True

    def start_visit(self, guest_id: int, expected_version: int, actor_id: int, *, now=None) -> Visit:
        for value in (guest_id, expected_version):
            _validate_identifier(value)
        _validate_actor(actor_id)
        moment = _timestamp(now)
        with self._connection() as connection:
            guest = connection.execute("SELECT version, name FROM guest_registry.guests WHERE id=%s FOR UPDATE", (guest_id,)).fetchone()
            if guest is None or guest["version"] != expected_version:
                raise StaleGuestError(_STALE)
            if connection.execute("SELECT 1 FROM guest_registry.visits WHERE guest_id=%s AND stopped_at IS NULL", (guest_id,)).fetchone():
                raise VisitStateError("Отсчёт времени для этого гостя уже запущен.")
            result = _visit(connection.execute("INSERT INTO guest_registry.visits (guest_id, started_at, started_by) VALUES (%s, %s, %s) RETURNING *", (guest_id, moment, actor_id)).fetchone())
            connection.execute("UPDATE guest_registry.guests SET version=version+1, updated_at=%s, updated_by=%s WHERE id=%s", (moment, actor_id, guest_id))
            self._record_audit(connection, guest_id, guest["name"], actor_id, "start", {
                "visit_id": {"before": None, "after": result.id}, "started_at": {"before": None, "after": moment}}, moment)
        return result

    def stop_visit(self, guest_id: int, visit_id: int, actor_id: int, *, now=None) -> Visit:
        for value in (guest_id, visit_id):
            _validate_identifier(value)
        _validate_actor(actor_id)
        moment = _utc_datetime(now)
        with self._connection() as connection:
            # Every mutation locks the guest first, then visit: deletion/start/
            # stop cannot deadlock or operate on a newer visit by accident.
            guest = connection.execute("SELECT name FROM guest_registry.guests WHERE id=%s FOR UPDATE", (guest_id,)).fetchone()
            if guest is None:
                raise VisitStateError(_VISIT)
            current = _visit(connection.execute("SELECT * FROM guest_registry.visits WHERE id=%s AND guest_id=%s AND stopped_at IS NULL FOR UPDATE", (visit_id, guest_id)).fetchone())
            if current is None:
                raise VisitStateError(_VISIT)
            if moment < datetime.fromisoformat(current.started_at):
                raise ValueError("Время завершения не может быть раньше начала визита.")
            stamp = _timestamp(moment)
            result = _visit(connection.execute("UPDATE guest_registry.visits SET stopped_at=%s, stopped_by=%s WHERE id=%s RETURNING *", (stamp, actor_id, visit_id)).fetchone())
            connection.execute("UPDATE guest_registry.guests SET version=version+1, updated_at=%s, updated_by=%s WHERE id=%s", (stamp, actor_id, guest_id))
            self._record_audit(connection, guest_id, guest["name"], actor_id, "stop", {
                "visit_id": {"before": visit_id, "after": visit_id}, "stopped_at": {"before": None, "after": stamp}}, stamp)
        return result

    def get_visit_summary(self, guest_id: int, *, now=None) -> VisitSummary:
        _validate_identifier(guest_id)
        moment = _utc_datetime(now)
        with self._connection(read_only=True) as connection:
            visits = [_visit(row) for row in connection.execute("SELECT * FROM guest_registry.visits WHERE guest_id=%s", (guest_id,)).fetchall()]
            baseline = connection.execute("SELECT * FROM guest_registry.recovered_totals WHERE guest_id=%s", (guest_id,)).fetchone()
        active = next((visit for visit in visits if visit.stopped_at is None), None)
        completed = [visit for visit in visits if visit.stopped_at is not None]
        return VisitSummary(active, len(completed) + (baseline["completed_count"] if baseline else 0),
                            sum(visit.duration_seconds(moment) for visit in completed) + (baseline["completed_seconds"] if baseline else 0),
                            active.duration_seconds(moment) if active else 0)

    def get_recovered_totals(self, guest_id: int) -> dict | None:
        """Return provenance for undated recovered history, if it was imported."""
        _validate_identifier(guest_id)
        with self._connection(read_only=True) as connection:
            row = connection.execute("SELECT * FROM guest_registry.recovered_totals WHERE guest_id=%s", (guest_id,)).fetchone()
            return dict(row) if row is not None else None

    def list_visits(self, guest_id: int, limit=5, offset=0) -> list[Visit]:
        _validate_identifier(guest_id)
        _validate_page(limit, offset)
        with self._connection(read_only=True) as connection:
            return [_visit(row) for row in connection.execute("SELECT * FROM guest_registry.visits WHERE guest_id=%s ORDER BY started_at::timestamptz DESC, id DESC LIMIT %s OFFSET %s", (guest_id, limit, offset)).fetchall()]

    def list_audit(self, guest_id=None, limit=5, offset=0) -> list[AuditEvent]:
        _validate_page(limit, offset)
        if guest_id is not None:
            _validate_identifier(guest_id)
        where, params = (" WHERE guest_id=%s", [guest_id]) if guest_id is not None else ("", [])
        with self._connection(read_only=True) as connection:
            return [_audit(row) for row in connection.execute("SELECT * FROM guest_registry.audit_events" + where + " ORDER BY id DESC LIMIT %s OFFSET %s", [*params, limit, offset]).fetchall()]

    def count_audit(self, guest_id=None) -> int:
        if guest_id is not None:
            _validate_identifier(guest_id)
        where, params = (" WHERE guest_id=%s", [guest_id]) if guest_id is not None else ("", [])
        with self._connection(read_only=True) as connection:
            return connection.execute("SELECT COUNT(*) AS n FROM guest_registry.audit_events" + where, params).fetchone()["n"]

    def get_audit(self, event_id: int) -> AuditEvent | None:
        _validate_identifier(event_id)
        with self._connection(read_only=True) as connection:
            return _audit(connection.execute("SELECT * FROM guest_registry.audit_events WHERE id=%s", (event_id,)).fetchone())

    @staticmethod
    def _staff_values(actor_id, name, username):
        _validate_actor(actor_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "\0" in name:
            raise ValueError("Некорректное имя сотрудника.")
        name = " ".join(name.split())
        if username is None:
            username = ""
        if not isinstance(username, str):
            raise ValueError("Некорректный Telegram username сотрудника.")
        username = username.strip().removeprefix("@")
        if username and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
            raise ValueError("Некорректный Telegram username сотрудника.")
        return name, username

    def remember_staff(self, actor_id, name, username=None) -> None:
        name, username = self._staff_values(actor_id, name, username)
        with self._connection() as connection:
            connection.execute("""INSERT INTO guest_registry.staff AS s (actor_id, name, username, updated_at)
                VALUES (%s, %s, %s, %s) ON CONFLICT (actor_id) DO UPDATE
                SET name=excluded.name, username=excluded.username, updated_at=excluded.updated_at
                WHERE s.name IS DISTINCT FROM excluded.name OR s.username IS DISTINCT FROM excluded.username""",
                (actor_id, name, username, _timestamp()))

    def staff_label(self, actor_id: int | None) -> str:
        if actor_id is None:
            return "Сотрудник не указан"
        _validate_actor(actor_id)
        with self._connection(read_only=True) as connection:
            row = connection.execute("SELECT name, username FROM guest_registry.staff WHERE actor_id=%s", (actor_id,)).fetchone()
        if row is None:
            return f"Telegram ID {actor_id}"
        return f'{row["name"]} (@{row["username"]})' if row["username"] else row["name"]

    def bootstrap_employee_access(self, initial_ids: Iterable[int], admin_id: int) -> None:
        _validate_access_id(admin_id)
        if admin_id != self._admin_id:
            raise ValueError("Администратор базы уже установлен; автоматическая смена запрещена.")
        try:
            supplied = tuple(initial_ids)
        except TypeError:
            raise ValueError("Укажите список числовых Telegram ID сотрудников.") from None
        seeds = set()
        for user_id in supplied:
            if type(user_id) is int and user_id == 2**53:
                continue
            _validate_access_id(user_id)
            if user_id != admin_id:
                seeds.add(user_id)
        with self._connection() as connection:
            meta = connection.execute("SELECT access_seeded FROM guest_registry.registry_meta WHERE singleton=1 FOR UPDATE").fetchone()
            if meta["access_seeded"]:
                return
            stamp = _timestamp()
            for user_id in sorted(seeds):
                connection.execute("INSERT INTO guest_registry.employee_access (user_id, added_by, added_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (user_id, admin_id, stamp))
            connection.execute("UPDATE guest_registry.registry_meta SET access_seeded=true WHERE singleton=1")

    def add_employee(self, user_id, actor_id) -> bool:
        _validate_access_id(user_id)
        _validate_access_id(actor_id)
        with self._connection() as connection:
            meta = connection.execute("SELECT admin_id FROM guest_registry.registry_meta WHERE singleton=1 FOR UPDATE").fetchone()
            if meta["admin_id"] != actor_id:
                raise PermissionError("Добавлять сотрудников может только администратор.")
            if user_id == actor_id:
                return False
            return connection.execute("INSERT INTO guest_registry.employee_access (user_id, added_by, added_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (user_id, actor_id, _timestamp())).rowcount == 1

    def has_employee_access(self, user_id) -> bool:
        _validate_access_id(user_id)
        with self._connection(read_only=True) as connection:
            return connection.execute("SELECT 1 FROM guest_registry.employee_access WHERE user_id=%s", (user_id,)).fetchone() is not None

    def employee_ids(self) -> frozenset[int]:
        with self._connection(read_only=True) as connection:
            return frozenset(row["user_id"] for row in connection.execute("SELECT user_id FROM guest_registry.employee_access").fetchall())

    def list_employees(self, limit=8, offset=0) -> list[EmployeeAccess]:
        _validate_page(limit, offset)
        with self._connection(read_only=True) as connection:
            return [EmployeeAccess(**row) for row in connection.execute("SELECT user_id, added_by, added_at FROM guest_registry.employee_access ORDER BY added_at::timestamptz, user_id LIMIT %s OFFSET %s", (limit, offset)).fetchall()]

    def count_employees(self) -> int:
        with self._connection(read_only=True) as connection:
            return connection.execute("SELECT COUNT(*) AS n FROM guest_registry.employee_access").fetchone()["n"]

    def overdue_visits(self, *, now=None, threshold_seconds=86400) -> list[tuple[Guest, Visit]]:
        if type(threshold_seconds) is not int or threshold_seconds <= 0:
            raise ValueError("Порог напоминания должен быть положительным числом секунд.")
        moment = _utc_datetime(now)
        columns = ", ".join("''::bytea AS guest_photo" if field == "photo" else f"g.{field} AS guest_{field}" for field in Guest.__dataclass_fields__)
        columns += ", " + ", ".join(f"v.{field} AS visit_{field}" for field in Visit.__dataclass_fields__)
        with self._connection(read_only=True) as connection:
            rows = connection.execute(f"SELECT {columns} FROM guest_registry.visits v JOIN guest_registry.guests g ON g.id=v.guest_id WHERE v.stopped_at IS NULL ORDER BY v.started_at::timestamptz, v.id").fetchall()
        result = []
        for row in rows:
            visit = _visit({field: row[f"visit_{field}"] for field in Visit.__dataclass_fields__})
            if visit.duration_seconds(moment) >= threshold_seconds:
                guest = _guest({field: row[f"guest_{field}"] for field in Guest.__dataclass_fields__})
                result.append((guest, visit))
        return result

    def reminder_sent(self, visit_id, recipient_id) -> bool:
        _validate_identifier(visit_id)
        _validate_actor(recipient_id)
        with self._connection(read_only=True) as connection:
            return connection.execute("SELECT 1 FROM guest_registry.visit_reminders WHERE visit_id=%s AND recipient_id=%s", (visit_id, recipient_id)).fetchone() is not None

    def mark_reminder_sent(self, visit_id, recipient_id, *, now=None) -> bool:
        _validate_identifier(visit_id)
        _validate_actor(recipient_id)
        stamp = _timestamp(now)
        with self._connection() as connection:
            # Locking this exact visit makes a racing stop wait until the
            # reminder marker commits, or return False after a committed stop.
            if connection.execute("SELECT id FROM guest_registry.visits WHERE id=%s AND stopped_at IS NULL FOR UPDATE", (visit_id,)).fetchone() is None:
                return False
            return connection.execute("INSERT INTO guest_registry.visit_reminders (visit_id, recipient_id, sent_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (visit_id, recipient_id, stamp)).rowcount == 1

    def snapshot(self, *, include_photos=False, now=None,
                 photo_transform: Callable[[bytes], bytes] | None = None) -> dict:
        if type(include_photos) is not bool or photo_transform is not None and not callable(photo_transform):
            raise ValueError("Некорректные параметры отчёта.")
        stamp = _timestamp(now)
        columns = ", ".join("''::bytea AS photo" if field == "photo" and not include_photos else field for field in Guest.__dataclass_fields__)
        guests = []
        with self._connection(read_only=True) as connection:
            for row in connection.execute(f"SELECT {columns} FROM guest_registry.guests ORDER BY id"):
                row = dict(row)
                row["photo"] = bytes(row["photo"])
                if include_photos and row["photo"] and photo_transform is not None:
                    try:
                        transformed = photo_transform(row["photo"])
                        if not isinstance(transformed, bytes) or len(transformed) > MAX_PHOTO_BYTES:
                            raise ValueError()
                        row["photo"] = transformed
                    except Exception:
                        raise PostgresStorageError("Не удалось подготовить фото для полного отчёта. Отчёт остановлен.") from None
                guests.append(_wire(row))
            visits = [dict(row) for row in connection.execute("SELECT * FROM guest_registry.visits ORDER BY id").fetchall()]
            recovered_totals = [dict(row) for row in connection.execute("SELECT * FROM guest_registry.recovered_totals ORDER BY guest_id").fetchall()]
        return {"generated_at": stamp, "guests": guests, "visits": visits, "recovered_totals": recovered_totals}

    @staticmethod
    def _validated_state(state, sequences, admin_id):
        _object(state, STATE_KEYS)
        if any(not isinstance(state[key], list) for key in STATE_KEYS):
            raise ValueError("Экспорт должен содержать все разделы базы данных.")
        result = {key: [] for key in STATE_KEYS}
        guest_ids, phones = set(), set()
        for row in state["guests"]:
            # A SQLite row may carry its derived name_key; wire snapshots omit
            # it. Both forms are checked, never trusted for search indexes.
            if isinstance(row, dict) and set(row) == {*Guest.__dataclass_fields__, "name_key"}:
                if row["name_key"] != normalize_name(row["name"]):
                    raise ValueError("Некорректный поисковый ключ карточки.")
                row = {key: value for key, value in row.items() if key != "name_key"}
            guest = decode_guest(row)
            if bool(guest.photo) != bool(guest.photo_file_id):
                raise ValueError("При восстановлении отсутствующее фото должно иметь пустой идентификатор.")
            if guest.id in guest_ids or guest.phone_key in phones:
                raise ValueError("Повтор карточки или телефона в экспорте.")
            guest_ids.add(guest.id)
            phones.add(guest.phone_key)
            result["guests"].append({**asdict(guest), "name_key": normalize_name(guest.name)})
        visit_ids, active = set(), set()
        for row in state["visits"]:
            visit = decode_visit(row)
            if visit.id in visit_ids or visit.guest_id not in guest_ids or visit.stopped_at is None and visit.guest_id in active:
                raise ValueError("Некорректная связь или повтор визита.")
            visit_ids.add(visit.id)
            if visit.stopped_at is None:
                active.add(visit.guest_id)
            result["visits"].append(asdict(visit))
        audit_ids = set()
        for original in state["audit_events"]:
            row = dict(_object(original, AuditEvent.__dataclass_fields__))
            _int(row["id"])
            _int(row["guest_id"])
            if row["actor_id"] is not None:
                _int(row["actor_id"])
            _time(row["created_at"])
            if isinstance(row["changes"], str):
                row["changes"] = json.loads(row["changes"])
            if (row["id"] in audit_ids or not isinstance(row["guest_name"], str)
                    or not row["guest_name"] or "\0" in row["guest_name"]
                    or row["action"] not in {"add", "update", "delete", "start", "stop"}
                    or not isinstance(row["changes"], dict)):
                raise ValueError("Некорректное событие истории.")
            for key, change in row["changes"].items():
                if not isinstance(key, str) or "\0" in key:
                    raise ValueError("Некорректное изменение в истории.")
                _object(change, ("before", "after"))
                if any(value is not None and type(value) not in {str, int, float, bool} for value in change.values()):
                    raise ValueError("Некорректное значение в истории.")
            # Reject NaN, Infinity and zero bytes before touching the destination.
            encoded = json.dumps(row["changes"], ensure_ascii=False, allow_nan=False)
            if "\\u0000" in encoded:
                raise ValueError("Некорректное изменение в истории.")
            audit_ids.add(row["id"])
            result["audit_events"].append(row)
        staff_ids = set()
        for original in state["staff"]:
            row = dict(_object(original, _TABLE_FIELDS["staff"]))
            _int(row["actor_id"])
            PostgresStore._staff_values(row["actor_id"], row["name"], row["username"])
            if (not isinstance(row["username"], str) or row["username"]
                    and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", row["username"])):
                raise ValueError("Некорректный Telegram username сотрудника.")
            _time(row["updated_at"])
            if row["actor_id"] in staff_ids:
                raise ValueError("Повтор профиля сотрудника.")
            staff_ids.add(row["actor_id"])
            result["staff"].append(row)
        employees = set()
        for original in state["employee_access"]:
            row = dict(_object(original, EmployeeAccess.__dataclass_fields__))
            _validate_access_id(row["user_id"])
            _validate_access_id(row["added_by"])
            _time(row["added_at"])
            if row["user_id"] in employees or row["user_id"] == admin_id or row["added_by"] != admin_id:
                raise ValueError("Некорректное разрешение сотрудника.")
            employees.add(row["user_id"])
            result["employee_access"].append(row)
        reminders = set()
        for original in state["visit_reminders"]:
            row = dict(_object(original, _TABLE_FIELDS["visit_reminders"]))
            key = (_int(row["visit_id"]), _int(row["recipient_id"]))
            _time(row["sent_at"])
            if key in reminders or key[0] not in visit_ids:
                raise ValueError("Некорректная связь напоминания.")
            reminders.add(key)
            result["visit_reminders"].append(row)
        maxima = {key: max((row["id"] for row in result[key]), default=0) for key in SEQUENCE_KEYS}
        if sequences is None:
            sequence_values = maxima
        else:
            _object(sequences, SEQUENCE_KEYS)
            sequence_values = {key: _int(value, minimum=0) for key, value in sequences.items()}
            if any(sequence_values[key] < maxima[key] for key in SEQUENCE_KEYS):
                raise ValueError("Счётчик идентификаторов меньше сохранённых данных.")
        return result, sequence_values

    def import_state(self, state: dict, *, admin_id=7011639945, sequences=None,
                     recovered_totals=None) -> dict:
        """Operator-only atomic import into a deliberately empty registry.

        Connect as the schema/sequence owner. Restoring sequence high-water
        marks transactionally requires ALTER SEQUENCE ownership; the limited
        runtime role intentionally lacks that privilege.

        recovered_totals contains residual counts/seconds after subtracting
        all supplied exact visits completed at/before each baseline's source
        observation. It is stored separately and never creates Visit rows.
        """
        _validate_access_id(admin_id)
        if admin_id != self._admin_id:
            raise PermissionError("Импортировать базу может только установленный администратор.")
        records, sequence_values = self._validated_state(state, sequences, admin_id)
        baselines = self._validated_recovered_totals(recovered_totals, {row["id"] for row in records["guests"]})
        import_records = {**records, "recovered_totals": baselines}
        with self._connection() as connection:
            connection.execute("SELECT singleton FROM guest_registry.registry_meta WHERE singleton=1 FOR UPDATE")
            # Table locks prevent an empty check racing add/start/profile/grant.
            # Only this explicit operator import needs these strong locks.
            connection.execute("LOCK TABLE " + ", ".join("guest_registry." + table for table in DATA_TABLES) + " IN EXCLUSIVE MODE")
            if any(connection.execute(f"SELECT 1 FROM guest_registry.{table} LIMIT 1").fetchone() is not None for table in DATA_TABLES):
                raise ValueError("Импорт разрешён только в полностью пустую базу. Данные не заменены.")
            for table in DATA_TABLES:
                for row in import_records[table]:
                    values = {**row}
                    if table == "audit_events":
                        values["changes"] = json.dumps(values["changes"], ensure_ascii=False, allow_nan=False)
                    fields = tuple(values)
                    placeholders = ", ".join("%s::jsonb" if table == "audit_events" and field == "changes" else "%s" for field in fields)
                    connection.execute(f"INSERT INTO guest_registry.{table} (" + ", ".join(fields) + ") VALUES (" + placeholders + ")", tuple(values.values()))
            for table, value in sequence_values.items():
                # ALTER ... RESTART is transactional. setval() is not and could
                # consume/reuse recovered IDs after a failed import.
                connection.execute(f"ALTER SEQUENCE guest_registry.{table}_id_seq RESTART WITH {value + 1}")
            connection.execute("UPDATE guest_registry.registry_meta SET access_seeded=true WHERE singleton=1")
        return {"imported": True, "guests": len(records["guests"]), "visits": len(records["visits"]),
                "employees": len(records["employee_access"]), "audit_events": len(records["audit_events"])}

    @staticmethod
    def _validated_recovered_totals(values, guest_ids):
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("Восстановленные итоги должны быть списком.")
        rows, seen = [], set()
        for value in values:
            row = decode_recovered_totals(value)
            if row["guest_id"] in seen or row["guest_id"] not in guest_ids:
                raise ValueError("Некорректная связь или повтор восстановленной сводки.")
            seen.add(row["guest_id"])
            rows.append(row)
        return rows

    def export_state(self) -> dict:
        """One complete repeatable snapshot, including photos and empty sections."""
        with self._connection(read_only=True) as connection:
            state = {}
            for table in STATE_KEYS:
                order = "visit_id, recipient_id" if table == "visit_reminders" else "actor_id" if table == "staff" else "user_id" if table == "employee_access" else "id"
                state[table] = [_wire(dict(row)) for row in connection.execute(f"SELECT * FROM guest_registry.{table} ORDER BY {order}").fetchall()]
            recovered_totals = [dict(row) for row in connection.execute("SELECT * FROM guest_registry.recovered_totals ORDER BY guest_id").fetchall()]
            sequences = {}
            for table in SEQUENCE_KEYS:
                row = connection.execute(f"SELECT last_value, is_called FROM guest_registry.{table}_id_seq").fetchone()
                sequences[table] = row["last_value"] if row["is_called"] else row["last_value"] - 1
        return {"format": "guest-registry-postgres-backup", "version": 1,
                "schema_version": SCHEMA_VERSION, "registry_id": self._registry_id, "admin_id": self._admin_id,
                "created_at": _timestamp(), "state": state, "sequences": sequences,
                "recovered_totals": recovered_totals}

    def backup(self, destination: Path | str) -> Path:
        """Atomic, complete JSON backup; contains private photos and guest data."""
        destination = Path(destination).expanduser().resolve()
        payload = self.export_state()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".guest-postgres-backup-", suffix=".json", dir=destination.parent)
        temporary_path = Path(temporary_name)
        try:
            _restrict_permissions(temporary_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
            _restrict_permissions(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination
