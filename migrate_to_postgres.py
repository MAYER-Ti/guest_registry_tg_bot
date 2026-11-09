"""Validate a preserved private JSON wrapper; import only with explicit --apply.

The source is never written. Apply needs the operator/schema owner's private
POSTGRES_MIGRATION_URL, a destination REGISTRY_ID, and --expected-guests.
No connection is made during the default dry run. Stop the bot while importing
and checking a fresh destination; application startup seeds nonempty tables.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

from postgres_store import (
    PostgresStore, SCHEMA_VERSION, STATE_KEYS, _options, _registry_identity,
    initialize_schema,
)
from storage import MAX_ACCESS_ID


ADMIN_ID = 7011639945
MAX_SOURCE_BYTES = 512 * 1024 * 1024
_SOURCE_ERROR = "Исходный JSON недоступен, повреждён или содержит несовместимые данные."
_TARGET_ERROR = "Не удалось подтвердить перенос. Не переключайте бота; проверьте доступ и содержимое назначения."


class MigrationError(RuntimeError):
    """A sanitized prerequisite or verification error."""


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    state: dict = field(repr=False)
    sequences: dict
    recovered_totals: list = field(repr=False)
    counts: dict
    source_sha256: str
    content_sha256: str
    path: Path = field(repr=False)
    admin_id: int


def _valid_id(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_ACCESS_ID:
        raise MigrationError("Некорректное ожидаемое число гостей или Telegram ID администратора.")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _wire(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


def _canonical(state, sequences, recovered_totals):
    order = {"guests": ("id",), "visits": ("id",), "audit_events": ("id",),
             "staff": ("actor_id",), "employee_access": ("user_id",),
             "visit_reminders": ("visit_id", "recipient_id")}
    rows = {key: sorted(state[key], key=lambda row: tuple(row[field] for field in order[key]))
            for key in STATE_KEYS}
    return _wire(dict(state=rows, sequences=sequences,
                      recovered_totals=sorted(recovered_totals, key=lambda row: row["guest_id"])))


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_source_unchanged(source):
    try:
        unchanged = _file_digest(source.path) == source.source_sha256
    except OSError:
        unchanged = False
    if not unchanged:
        raise MigrationError("Исходный JSON изменился. Перенос остановлен; используйте отдельную сохранённую копию.")


def read_source(path: Path | str, expected_guests=None, *, admin_id=ADMIN_ID) -> SourceSnapshot:
    """Validate recovery/backup wrappers locally, including deleted-ID counters."""
    _valid_id(admin_id)
    if expected_guests is not None:
        _valid_id(expected_guests, minimum=0)
    try:
        path = Path(path).expanduser().resolve()
        with path.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise MigrationError("JSON превышает безопасный предел 512 МиБ. Данные не обрезаны; нужен отдельный план переноса.")
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if not isinstance(payload, dict) or "state" not in payload or "sequences" not in payload:
            raise ValueError("State wrapper and counters required")
        if payload.get("admin_id", admin_id) != admin_id or type(payload.get("admin_id", admin_id)) is not int:
            raise MigrationError("Администратор исходной копии не совпадает с выбранным администратором.")
        if (type(payload.get("schema_version", SCHEMA_VERSION)) is not int
                or payload.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION
                or type(payload.get("version", 1)) is not int or payload.get("version", 1) != 1):
            raise ValueError("Incompatible schema")
        if not isinstance(payload["sequences"], dict):
            raise ValueError("Explicit counters required")
        state, sequences = PostgresStore._validated_state(payload["state"], payload["sequences"], admin_id)
        if not isinstance(payload.get("recovered_totals", []), list):
            raise ValueError("Invalid recovered totals")
        baselines = PostgresStore._validated_recovered_totals(payload.get("recovered_totals", []),
                                                             {row["id"] for row in state["guests"]})
        if expected_guests is not None and len(state["guests"]) != expected_guests:
            raise MigrationError("Ожидаемое число карточек не совпало. Перенос остановлен.")
        counts = {key: len(state[key]) for key in STATE_KEYS}
        counts.update(recovered_totals=len(baselines), photos=sum(bool(row["photo"]) for row in state["guests"]),
                      missing_photos=sum(not row["photo"] for row in state["guests"]),
                      active_visits=sum(row["stopped_at"] is None for row in state["visits"]))
        result = SourceSnapshot(state, sequences, baselines, counts, hashlib.sha256(raw).hexdigest(),
                                _digest(_canonical(state, sequences, baselines)), path, admin_id)
        _assert_source_unchanged(result)
        return result
    except MigrationError:
        raise
    except Exception:
        raise MigrationError(_SOURCE_ERROR) from None


def _settings(environ):
    """Use a private operator URL; runtime DATABASE_URL is never a fallback."""
    try:
        url = environ.get("POSTGRES_MIGRATION_URL", "").strip()
        registry = _registry_identity(environ.get("REGISTRY_ID", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}", registry):
            raise ValueError("Invalid identity")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True)
        if (parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username
                or parsed.path in {"", "/"} or parsed.fragment or parsed.port == 0
                or any(char in url for char in "\r\n\t\0") or any(len(values) != 1 for values in query.values())):
            raise ValueError("Invalid URL")
        sslmode = environ.get("DB_SSLMODE", query.get("sslmode", ["require"])[0]).strip()
        if query.get("sslmode", [sslmode])[0] not in {"require", "verify-full"}:
            raise ValueError("TLS required")
        rootcert = environ.get("DB_SSLROOTCERT", query.get("sslrootcert", [""])[0]).strip() or None
        if sslmode == "verify-full" and rootcert is None:
            raise ValueError("Certificate required")
        _options(url, sslmode, 15, rootcert)
        runtime_role = environ.get("POSTGRES_RUNTIME_ROLE", "").strip() or None
        if runtime_role is not None and (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role)
                                          or runtime_role in {"public", "anon", "authenticated", "service_role"}):
            raise ValueError("Invalid runtime role")
        return url, registry, sslmode, rootcert, runtime_role
    except Exception:
        raise MigrationError("Задайте POSTGRES_MIGRATION_URL оператора, REGISTRY_ID и безопасные настройки TLS.") from None


def verify_import(remote, source: SourceSnapshot, *, registry_id: str):
    """Compare every section, original photo byte, baseline and high-water mark."""
    try:
        actual = remote.export_state()
        if actual["registry_id"] != registry_id or actual["admin_id"] != source.admin_id or actual["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Destination identity changed")
        state, sequences = PostgresStore._validated_state(actual["state"], actual["sequences"], source.admin_id)
        baselines = PostgresStore._validated_recovered_totals(actual["recovered_totals"],
                                                             {row["id"] for row in state["guests"]})
        if _canonical(state, sequences, baselines) != _canonical(source.state, source.sequences, source.recovered_totals):
            raise ValueError("Readback mismatch")
    except Exception:
        raise MigrationError(_TARGET_ERROR) from None


def migrate(path: Path | str, expected_guests=None, *, apply=False, initialize=False,
            admin_id=ADMIN_ID, environ=None, store_factory=None, initializer=None) -> dict:
    """Perform one import, with local preflight and complete post-commit readback."""
    if type(apply) is not bool or type(initialize) is not bool or initialize and not apply:
        raise MigrationError("Создание схемы требует совместного указания --apply и --initialize.")
    if apply and expected_guests is None:
        raise MigrationError("Для записи обязательно укажите --expected-guests.")
    source = read_source(path, expected_guests, admin_id=admin_id)
    report = dict(mode="apply" if apply else "dry_run", counts=source.counts,
                  source_sha256=source.source_sha256, content_sha256=source.content_sha256,
                  source_unchanged=True)
    if not apply:
        return report
    url, registry, sslmode, rootcert, runtime_role = _settings(os.environ if environ is None else environ)
    _assert_source_unchanged(source)
    try:
        if initialize:
            (initializer or initialize_schema)(url, registry_id=registry, admin_id=admin_id,
                                                sslmode=sslmode, sslrootcert=rootcert, runtime_role=runtime_role)
        remote = (store_factory or PostgresStore)(url, expected_registry_id=registry,
                                                  expected_admin_id=admin_id, sslmode=sslmode, sslrootcert=rootcert)
        before = remote.export_state()
        if (set(before["state"]) != set(STATE_KEYS) or any(before["state"].values())
                or before["recovered_totals"]):
            raise MigrationError("Назначение содержит данные. Импорт не выполнен; автоматическая замена запрещена.")
        _assert_source_unchanged(source)
        remote.import_state(deepcopy(source.state), admin_id=admin_id, sequences=deepcopy(source.sequences),
                            recovered_totals=deepcopy(source.recovered_totals))
        verify_import(remote, source, registry_id=registry)
        _assert_source_unchanged(source)
    except MigrationError:
        raise
    except Exception:
        raise MigrationError(_TARGET_ERROR) from None
    report["verified"] = True
    return report


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise MigrationError("Некорректные параметры. Укажите --source и проверьте флаги переноса.")


def main(argv=None) -> int:
    parser = _SafeParser(description="Проверка и явный перенос сохранённого JSON в пустую PostgreSQL-картотеку.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-guests", type=int)
    parser.add_argument("--admin-id", type=int, default=ADMIN_ID)
    parser.add_argument("--apply", action="store_true", help="Разрешить единственный импорт и полную проверку результата.")
    parser.add_argument("--initialize", action="store_true", help="Вместе с --apply создать только отсутствующую схему.")
    try:
        args = parser.parse_args(argv)
        result = migrate(args.source, args.expected_guests, apply=args.apply,
                         initialize=args.initialize, admin_id=args.admin_id)
    except Exception:
        print("Перенос не выполнен или не подтверждён. Скрипт не изменяет исходный файл. Проверьте копию, число карточек и пустое назначение.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
