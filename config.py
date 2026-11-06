"""Environment configuration. Secrets are never included in validation errors."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
    sheets_endpoint: str = field(default="", repr=False)
    sheets_secret: str = field(default="", repr=False)
    google_owner_email: str = field(default="", repr=False)
    allow_new_database: bool = False


def _storage_settings() -> dict:
    backend = os.environ.get("STORAGE_BACKEND", "sqlite").strip()
    if backend not in {"sqlite", "google_sheets"}:
        raise ConfigError("STORAGE_BACKEND должен быть sqlite или google_sheets.")
    allow_new = os.environ.get("ALLOW_NEW_DATABASE", "0").strip()
    if allow_new not in {"0", "1"}:
        raise ConfigError("ALLOW_NEW_DATABASE должен быть 0 или 1.")
    settings = {"storage_backend": backend, "allow_new_database": allow_new == "1"}
    if backend == "google_sheets":
        endpoint = os.environ.get("SHEETS_ENDPOINT", "").strip()
        secret = os.environ.get("SHEETS_SECRET", "")
        owner = os.environ.get("SHEETS_OWNER_EMAIL", "").strip().casefold()
        try:
            parsed = urlsplit(endpoint)
            valid = (parsed.scheme == "https" and parsed.netloc == "script.google.com"
                     and re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parsed.path)
                     and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("Задайте SHEETS_ENDPOINT — опубликованный HTTPS-адрес Google Apps Script.")
        if not 32 <= len(secret) <= 512 or not secret.isascii() or any(char.isspace() for char in secret):
            raise ConfigError("Задайте SHEETS_SECRET длиной от 32 до 512 символов без пробелов.")
        if len(owner) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", owner):
            raise ConfigError("Задайте SHEETS_OWNER_EMAIL — Google-аккаунт владельца хранилища.")
        settings.update(sheets_endpoint=endpoint, sheets_secret=secret, google_owner_email=owner)
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
