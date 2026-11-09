"""Environment configuration. Secrets are never included in validation errors."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class ConfigError(ValueError):
    """The bot cannot start safely with the supplied configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    db_path: Path
    extra_db_paths: tuple[Path, ...] = field(default=(), repr=False)
    admin_user_id: int = 7011639945
    storage_backend: str = "sqlite"
    allow_new_database: bool = False
    database_url: str = field(default="", repr=False)
    registry_id: str = field(default="", repr=False)
    db_sslmode: str = "require"
    db_sslrootcert: str = field(default="", repr=False)
    postgres_backup_dir: Path = field(default=Path("data/backups"), repr=False)


def _storage_settings() -> dict:
    backend = os.environ.get("STORAGE_BACKEND", "sqlite").strip()
    if backend not in {"sqlite", "postgres"}:
        raise ConfigError("STORAGE_BACKEND должен быть sqlite или postgres.")
    allow_new = os.environ.get("ALLOW_NEW_DATABASE", "0").strip()
    if allow_new not in {"0", "1"}:
        raise ConfigError("ALLOW_NEW_DATABASE должен быть 0 или 1.")
    settings = {"storage_backend": backend, "allow_new_database": allow_new == "1"}
    if backend == "postgres":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        registry_id = os.environ.get("REGISTRY_ID", "").strip()
        try:
            parsed = urlsplit(database_url)
            query = parse_qs(parsed.query, strict_parsing=True)
            valid = (parsed.scheme in {"postgres", "postgresql"} and parsed.hostname
                     and parsed.username and parsed.path not in {"", "/"}
                     and not parsed.fragment and parsed.port != 0
                     and not any(char in database_url for char in "\r\n\x00")
                     and all(len(values) == 1 for values in query.values()))
        except (ValueError, TypeError):
            valid = False
            query = {}
        if not valid:
            raise ConfigError("Задайте DATABASE_URL — строку подключения к PostgreSQL из Supabase.")
        sslmode = os.environ.get("DB_SSLMODE", query.get("sslmode", ["require"])[0]).strip()
        if sslmode not in {"require", "verify-full"} or query.get("sslmode", [sslmode])[0] not in {"require", "verify-full"}:
            raise ConfigError("Подключение к базе должно использовать TLS: DB_SSLMODE=require или verify-full.")
        rootcert = os.environ.get("DB_SSLROOTCERT", query.get("sslrootcert", [""])[0]).strip()
        if sslmode == "verify-full" and not rootcert:
            raise ConfigError("Для проверки сертификата укажите DB_SSLROOTCERT — сертификат базы или system.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,127}", registry_id):
            raise ConfigError("Задайте REGISTRY_ID — идентификатор заранее подготовленной картотеки.")
        backup_directory = os.environ.get("POSTGRES_BACKUP_DIR", "data/backups").strip()
        if not backup_directory or "\x00" in backup_directory or backup_directory == ":memory:":
            raise ConfigError("POSTGRES_BACKUP_DIR должен указывать на папку постоянных резервных копий.")
        settings.update(database_url=database_url, registry_id=registry_id,
                        db_sslmode=sslmode, db_sslrootcert=rootcert,
                        postgres_backup_dir=Path(backup_directory).expanduser())
    return settings


def get_db_path() -> Path:
    """Resolve storage without needing Telegram credentials (also used by backups)."""
    raw_path = os.environ.get("DB_PATH")
    if raw_path is None:
        raw_dir = os.environ.get("DATA_DIR", "data").strip()
        if not raw_dir:
            raise ConfigError("DATA_DIR не должен быть пустым.")
        return Path(raw_dir) / "guests.sqlite3"
    raw_path = raw_path.strip()
    if not raw_path or raw_path == ":memory:":
        raise ConfigError("DB_PATH должен указывать на файл постоянной базы данных.")
    return Path(raw_path)


def get_extra_db_paths() -> tuple[Path, ...]:
    """Only explicitly connected databases participate in reports.

    JSON avoids ambiguous separators in Windows paths and filenames. Missing
    and invalid sources are handled by the reporting layer without creating
    files or upgrading their schemas.
    """
    raw_paths = os.environ.get("EXTRA_DB_PATHS")
    if raw_paths is None:
        return ()
    try:
        values = json.loads(raw_paths)
    except (ValueError, TypeError):
        raise ConfigError("EXTRA_DB_PATHS должен быть JSON-массивом путей к базам данных.") from None
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip()
        or value.strip() == ":memory:" or "\x00" in value
        for value in values
    ):
        raise ConfigError("EXTRA_DB_PATHS должен быть JSON-массивом непустых путей к файлам баз данных.")
    return tuple(Path(value.strip()) for value in values)


def load_config() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token):
        raise ConfigError("Задайте корректный BOT_TOKEN, полученный у @BotFather.")

    raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
    parts = [part.strip() for part in raw_ids.split(",")]
    if not parts or any(not re.fullmatch(r"[1-9][0-9]*", part) for part in parts):
        raise ConfigError(
            "ALLOWED_USER_IDS обязателен: укажите числовые Telegram ID сотрудников "
            "через запятую (положительные целые числа)."
        )
    return Config(
        token=token,
        allowed_user_ids=frozenset(int(part) for part in parts),
        db_path=get_db_path(),
        extra_db_paths=get_extra_db_paths(),
        **_storage_settings(),
    )
