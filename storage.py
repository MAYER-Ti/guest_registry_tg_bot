"""Persistent, shared guest cards backed by one SQLite database.

Search results omit photo bytes; use ``get_guest`` before displaying a card.
Edits and deletion require the version shown to the employee. A changed or
missing card raises ``StaleGuestError`` so one employee cannot silently undo
another employee's work. Successful deletion returns ``True``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterator
import unicodedata


MAX_PHOTO_BYTES = 10 * 1024 * 1024
_PHONE_FORMAT = re.compile(r"^[\d\s()+.\-]+$")
_EDITABLE_FIELDS = {"name", "phone", "comment", "photo", "photo_file_id"}


class DuplicatePhoneError(ValueError):
    """Another guest already has this normalized telephone number."""


class StaleGuestError(RuntimeError):
    """The card was changed or deleted after the employee opened it."""


@dataclass(frozen=True, slots=True)
class Guest:
    id: int
    name: str
    phone: str
    phone_key: str
    comment: str
    photo: bytes
    photo_file_id: str
    created_by: int
    updated_by: int
    created_at: str
    updated_at: str
    version: int


def normalize_name(value: str) -> str:
    """Normalize Unicode, case, Russian ё, and whitespace for name lookup."""
    if not isinstance(value, str):
        raise ValueError("Имя должно быть текстом.")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def _phone_digits(value: str) -> str:
    # Convert decimal Unicode digits to ASCII so different input methods match.
    return "".join(str(unicodedata.decimal(char)) for char in value if char.isdecimal())


def normalize_phone(value: str) -> str:
    """Accept a formatted 7–15 digit number; Russian 8xxxxxxxxxx becomes 7."""
    if not isinstance(value, str) or len(value) > 100 or not _PHONE_FORMAT.fullmatch(value):
        raise ValueError("Укажите телефон цифрами, например +7 999 123-45-67.")
    digits = _phone_digits(value)
    if not 7 <= len(digits) <= 15:
        raise ValueError("В телефоне должно быть от 7 до 15 цифр.")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _validate_actor(actor_id: int) -> None:
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id <= 0:
        raise ValueError("Некорректный Telegram ID сотрудника.")


def _validate_fields(*, name: str, phone: str, comment: str, photo: bytes,
                     photo_file_id: str) -> dict[str, object]:
    if not isinstance(name, str):
        raise ValueError("Имя должно быть текстом.")
    name = " ".join(name.split())
    if not name or len(name) > 100:
        raise ValueError("Имя должно содержать от 1 до 100 символов.")
    phone_key = normalize_phone(phone)
    if not isinstance(comment, str) or len(comment) > 3000:
        raise ValueError("Комментарий должен содержать не более 3000 символов.")
    if not isinstance(photo, bytes) or not photo or len(photo) > MAX_PHOTO_BYTES:
        raise ValueError("Нужно фото размером не более 10 МБ.")
    if not isinstance(photo_file_id, str) or not photo_file_id.strip() or len(photo_file_id) > 1024:
        raise ValueError("Некорректный идентификатор фото Telegram.")
    return {
        "name": name,
        "name_key": normalize_name(name),
        "phone": phone.strip(),
        "phone_key": phone_key,
        "comment": comment.strip(),
        "photo": photo,
        "photo_file_id": photo_file_id.strip(),
    }


def _restrict_permissions(path: Path, mode: int = 0o600) -> None:
    # Unix permissions are effective on Linux hosting; Windows chmod is limited.
    try:
        path.chmod(mode)
    except OSError:
        pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _guest(row: sqlite3.Row | None) -> Guest | None:
    if row is None:
        return None
    return Guest(**{field: row[field] for field in Guest.__dataclass_fields__})


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_filter(query: str) -> tuple[str, list[str]]:
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError("Поисковый запрос должен содержать не более 100 символов.")
    name_key = normalize_name(query)
    if not name_key:
        return "0", []
    params = ["%" + _like_literal(name_key) + "%"]
    clauses = ["name_key LIKE ? ESCAPE '\\'"]
    if _PHONE_FORMAT.fullmatch(query):
        digits = _phone_digits(query)
        if digits:
            if len(digits) == 11 and digits.startswith("8"):
                digits = "7" + digits[1:]
            clauses.append("phone_key LIKE ? ESCAPE '\\'")
            params.append("%" + digits + "%")
    return "(" + " OR ".join(clauses) + ")", params


class Store:
    """Shared database; each operation opens its own short-lived connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        # Python 3.13 maps 0700 to a Windows DACL that can exclude sandbox
        # tokens. Use the inherited Windows ACL; keep owner-only Linux dirs.
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS guests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    phone_key TEXT NOT NULL UNIQUE,
                    comment TEXT NOT NULL,
                    photo BLOB NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
                )
            """)
            connection.commit()
        _restrict_permissions(self.path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def add_guest(self, *, name: str, phone: str, comment: str, photo: bytes,
                  photo_file_id: str, actor_id: int) -> Guest:
        _validate_actor(actor_id)
        fields = _validate_fields(name=name, phone=phone, comment=comment,
                                  photo=photo, photo_file_id=photo_file_id)
        now = _timestamp()
        fields.update(created_by=actor_id, updated_by=actor_id,
                      created_at=now, updated_at=now)
        with self._connection() as connection, connection:
            try:
                cursor = connection.execute("""
                    INSERT INTO guests (name, name_key, phone, phone_key, comment,
                        photo, photo_file_id, created_by, updated_by, created_at, updated_at)
                    VALUES (:name, :name_key, :phone, :phone_key, :comment, :photo,
                        :photo_file_id, :created_by, :updated_by, :created_at, :updated_at)
                """, fields)
            except sqlite3.IntegrityError as exc:
                if "guests.phone_key" in str(exc):
                    raise DuplicatePhoneError("Гость с таким телефоном уже есть.") from exc
                raise
            result = _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (cursor.lastrowid,)).fetchone())
        assert result is not None
        return result

    def get_guest(self, id: int) -> Guest | None:
        with self._connection() as connection:
            return _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (id,)).fetchone())

    def find_phone(self, phone: str) -> Guest | None:
        phone_key = normalize_phone(phone)
        with self._connection() as connection:
            return _guest(connection.execute("SELECT * FROM guests WHERE phone_key = ?", (phone_key,)).fetchone())

    def search_guests(self, query: str, limit: int = 8, offset: int = 0) -> list[Guest]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Размер страницы должен быть от 1 до 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Смещение должно быть неотрицательным целым числом.")
        where, params = _search_filter(query)
        # Avoid loading every photo merely to display a list of possible matches.
        columns = ", ".join("X'' AS photo" if field == "photo" else field
                            for field in Guest.__dataclass_fields__)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM guests WHERE {where} ORDER BY name_key, id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_guest(row) for row in rows]  # type: ignore[misc]

    def count_search(self, query: str) -> int:
        where, params = _search_filter(query)
        with self._connection() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM guests WHERE {where}", params).fetchone()[0])

    def update_guest(self, id: int, expected_version: int, actor_id: int,
                     **fields: object) -> Guest:
        _validate_actor(actor_id)
        if not fields or fields.keys() - _EDITABLE_FIELDS:
            raise ValueError("Укажите только изменяемые поля карточки.")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (id,)).fetchone())
            if current is None or current.version != expected_version:
                raise StaleGuestError("Карточка уже изменена или удалена. Откройте её заново.")
            values = {field: fields.get(field, getattr(current, field)) for field in _EDITABLE_FIELDS}
            validated = _validate_fields(**values)  # type: ignore[arg-type]
            validated.update(id=id, updated_by=actor_id, updated_at=_timestamp(),
                             expected_version=expected_version)
            try:
                connection.execute("""
                    UPDATE guests SET name = :name, name_key = :name_key,
                        phone = :phone, phone_key = :phone_key, comment = :comment,
                        photo = :photo, photo_file_id = :photo_file_id,
                        updated_by = :updated_by, updated_at = :updated_at,
                        version = version + 1
                    WHERE id = :id AND version = :expected_version
                """, validated)
            except sqlite3.IntegrityError as exc:
                if "guests.phone_key" in str(exc):
                    raise DuplicatePhoneError("Гость с таким телефоном уже есть.") from exc
                raise
            result = _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (id,)).fetchone())
        assert result is not None
        return result

    def delete_guest(self, id: int, expected_version: int) -> bool:
        """Return True when deleted; missing/stale cards raise StaleGuestError."""
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM guests WHERE id = ? AND version = ?", (id, expected_version))
            if cursor.rowcount != 1:
                raise StaleGuestError("Карточка уже изменена или удалена. Откройте её заново.")
        return True

    def backup(self, destination: Path | str) -> Path:
        """Write an atomic SQLite online backup, including every guest photo."""
        destination = Path(destination).expanduser().resolve()
        if destination == self.path:
            raise ValueError("Резервная копия должна иметь другой путь.")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".guest-backup-", suffix=".sqlite3", dir=destination.parent)
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with self._connection() as source:
                target = sqlite3.connect(str(temporary_path))
                try:
                    source.backup(target)
                finally:
                    target.close()
            _restrict_permissions(temporary_path)
            os.replace(temporary_path, destination)
            _restrict_permissions(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return destination
