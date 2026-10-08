"""Create a consistent SQLite backup, including guest photos, without a bot token."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from config import ConfigError, get_db_path
from storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Резервная копия базы гостей вместе с фотографиями.")
    parser.add_argument("destination", type=Path, help="Новый файл резервной копии, например backups/guests-2026-10-08.sqlite3")
    args = parser.parse_args()
    try:
        source = get_db_path()
        if not source.is_file():
            parser.error("База не найдена. Проверьте DB_PATH; пустая база для копии не создаётся.")
        if source.resolve() == args.destination.resolve():
            parser.error("Резервная копия должна находиться в другом файле.")
        if args.destination.exists():
            parser.error("Файл назначения уже существует. Выберите новое имя копии.")
        result = Store(source).backup(args.destination)
    except (ConfigError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Не удалось создать резервную копию: {error}\n")
    print(f"Резервная копия создана: {result}")


if __name__ == "__main__":
    main()
