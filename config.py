"""Environment configuration. Secrets are never included in validation errors."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """The bot cannot start safely with the supplied configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    db_path: Path


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
    )
