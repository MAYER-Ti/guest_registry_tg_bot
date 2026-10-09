"""Environment configuration. Secrets are never included in validation errors."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """The bot cannot start safely with the supplied configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    db_path: Path
    extra_db_paths: tuple[Path, ...] = field(default=(), repr=False)


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
    )
