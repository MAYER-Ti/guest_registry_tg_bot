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
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterator
import unicodedata
from uuid import uuid4


MAX_PHOTO_BYTES = 10 * 1024 * 1024
_PHONE_FORMAT = re.compile(r"^[\d\s()+.\-]+$")
_EDITABLE_FIELDS = {"name", "phone", "comment", "photo", "photo_file_id",
                    "entry_status", "entry_reason"}


class DuplicatePhoneError(ValueError):
    """Another guest already has this normalized telephone number."""


class StaleGuestError(RuntimeError):
    """The card was changed or deleted after the employee opened it."""


class VisitStateError(RuntimeError):
    """The requested visit is already active, stopped, or no longer exists."""


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
    entry_status: str = "open"
    entry_reason: str = ""


@dataclass(frozen=True, slots=True)
class Visit:
    id: int
    guest_id: int
    started_at: str
    stopped_at: str | None
    started_by: int
    stopped_by: int | None

    def duration_seconds(self, now: datetime | None = None) -> int:
        """Whole elapsed seconds; active visits use the current UTC time."""
        started = datetime.fromisoformat(self.started_at)
        stopped = datetime.fromisoformat(self.stopped_at) if self.stopped_at else _utc_datetime(now)
        return max(0, int((stopped - started).total_seconds()))


@dataclass(frozen=True, slots=True)
class VisitSummary:
    active: Visit | None
    completed_count: int
    completed_seconds: int
    current_seconds: int

    @property
    def total_seconds(self) -> int:
        return self.completed_seconds + self.current_seconds


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: int
    guest_id: int
    guest_name: str
    actor_id: int | None
    action: str
    changes: dict[str, object]
    created_at: str


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


def _validate_identifier(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Некорректный идентификатор.")


def _validate_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Размер страницы должен быть от 1 до 100.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Смещение должно быть неотрицательным целым числом.")


def _card_changes(before: Guest | None, after: Guest | None) -> dict[str, object]:
    changes: dict[str, object] = {}
    for field in ("name", "phone", "comment", "entry_status", "entry_reason"):
        old = getattr(before, field) if before else None
        new = getattr(after, field) if after else None
        if old != new:
            changes[field] = {"before": old, "after": new}
    if (before is None or after is None or before.photo != after.photo
            or before.photo_file_id != after.photo_file_id):
        # Audit data never duplicates photos or Telegram file identifiers.
        changes["photo"] = {
            "before": "Есть фото" if before else None,
            "after": ("Новое фото" if before else "Есть фото") if after else None,
        }
    return changes


def _audit_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(id=row["id"], guest_id=row["guest_id"], guest_name=row["guest_name"],
                      actor_id=row["actor_id"], action=row["action"],
                      changes=json.loads(row["changes"]), created_at=row["created_at"])


def _validate_fields(*, name: str, phone: str, comment: str, photo: bytes,
                     photo_file_id: str, entry_status: str = "open",
                     entry_reason: str = "") -> dict[str, object]:
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
    if not isinstance(entry_status, str) or entry_status not in {"open", "closed"}:
        raise ValueError("Статус входа должен быть «вход открыт» или «вход закрыт».")
    if not isinstance(entry_reason, str) or len(entry_reason) > 3000:
        raise ValueError("Причина должна содержать не более 3000 символов.")
    return {
        "name": name,
        "name_key": normalize_name(name),
        "phone": phone.strip(),
        "phone_key": phone_key,
        "comment": comment.strip(),
        "photo": photo,
        "photo_file_id": photo_file_id.strip(),
        "entry_status": entry_status,
        "entry_reason": entry_reason.strip(),
    }


def _restrict_permissions(path: Path, mode: int = 0o600) -> None:
    # Unix permissions are effective on Linux hosting; Windows chmod is limited.
    try:
        path.chmod(mode)
    except OSError:
        pass


def _utc_datetime(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс.")
    return now.astimezone(timezone.utc)


def _timestamp(now: datetime | None = None) -> str:
    return _utc_datetime(now).isoformat(timespec="microseconds")


def _guest(row: sqlite3.Row | None) -> Guest | None:
    if row is None:
        return None
    return Guest(**{field: row[field] for field in Guest.__dataclass_fields__})


def _visit(row: sqlite3.Row | None) -> Visit | None:
    if row is None:
        return None
    return Visit(**{field: row[field] for field in Visit.__dataclass_fields__})


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
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            columns = {row[1] for row in connection.execute("PRAGMA table_info(guests)")}
            missing_entry_columns = {"entry_status", "entry_reason"} - columns
            missing_activity_tables = {"audit_events", "staff", "visit_reminders"} - tables
            if "guests" in tables and ("visits" not in tables or missing_entry_columns
                                       or missing_activity_tables):
                # Back up old/partial schemas before any schema change. Run the
                # online backup outside the transaction; failure aborts startup.
                stamp = _utc_datetime().strftime("%Y%m%dT%H%M%S%fZ")
                upgrade = ("visits" if "visits" not in tables else
                           "entry-status" if missing_entry_columns else "activity")
                self.backup(self.path.parent / "backups" /
                            f"guests-before-{upgrade}-{stamp}-{uuid4().hex}.sqlite3")
            connection.execute("BEGIN IMMEDIATE")
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
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    entry_status TEXT NOT NULL DEFAULT 'open'
                        CHECK (entry_status IN ('open', 'closed')),
                    entry_reason TEXT NOT NULL DEFAULT '' CHECK (length(entry_reason) <= 3000)
                )
            """)
            # Re-read after the write lock, allowing another process to have
            # completed the migration while this one prepared its backup.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(guests)")}
            if "entry_status" not in columns:
                connection.execute("""
                    ALTER TABLE guests ADD COLUMN entry_status TEXT NOT NULL DEFAULT 'open'
                    CHECK (entry_status IN ('open', 'closed'))
                """)
            if "entry_reason" not in columns:
                connection.execute("""
                    ALTER TABLE guests ADD COLUMN entry_reason TEXT NOT NULL DEFAULT ''
                    CHECK (length(entry_reason) <= 3000)
                """)
            # Additive migration leaves every existing guest and photo intact.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    started_by INTEGER NOT NULL,
                    stopped_by INTEGER,
                    CHECK ((stopped_at IS NULL) = (stopped_by IS NULL)),
                    CHECK (stopped_at IS NULL OR stopped_at >= started_at)
                )
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS visits_one_active_per_guest
                ON visits(guest_id) WHERE stopped_at IS NULL
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS visits_guest_started
                ON visits(guest_id, started_at DESC, id DESC)
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guest_id INTEGER NOT NULL,
                    guest_name TEXT NOT NULL,
                    actor_id INTEGER,
                    action TEXT NOT NULL CHECK (action IN ('add', 'update', 'delete', 'start', 'stop')),
                    changes TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS audit_events_guest_id
                ON audit_events(guest_id, id DESC)
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS staff (
                    actor_id INTEGER PRIMARY KEY CHECK (actor_id > 0),
                    name TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS visit_reminders (
                    visit_id INTEGER NOT NULL REFERENCES visits(id) ON DELETE CASCADE,
                    recipient_id INTEGER NOT NULL CHECK (recipient_id > 0),
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (visit_id, recipient_id)
                )
            """)
            connection.commit()
        _restrict_permissions(self.path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def add_guest(self, *, name: str, phone: str, comment: str, photo: bytes,
                  photo_file_id: str, actor_id: int, entry_status: str = "open",
                  entry_reason: str = "") -> Guest:
        _validate_actor(actor_id)
        fields = _validate_fields(name=name, phone=phone, comment=comment,
                                  photo=photo, photo_file_id=photo_file_id,
                                  entry_status=entry_status, entry_reason=entry_reason)
        now = _timestamp()
        fields.update(created_by=actor_id, updated_by=actor_id,
                      created_at=now, updated_at=now)
        with self._connection() as connection, connection:
            try:
                cursor = connection.execute("""
                    INSERT INTO guests (name, name_key, phone, phone_key, comment,
                        photo, photo_file_id, created_by, updated_by, created_at, updated_at,
                        entry_status, entry_reason)
                    VALUES (:name, :name_key, :phone, :phone_key, :comment, :photo,
                        :photo_file_id, :created_by, :updated_by, :created_at, :updated_at,
                        :entry_status, :entry_reason)
                """, fields)
            except sqlite3.IntegrityError as exc:
                if "guests.phone_key" in str(exc):
                    raise DuplicatePhoneError("Гость с таким телефоном уже есть.") from exc
                raise
            result = _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (cursor.lastrowid,)).fetchone())
            assert result is not None
            self._record_audit(connection, result.id, result.name, actor_id, "add",
                               _card_changes(None, result), now)
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
                        entry_status = :entry_status, entry_reason = :entry_reason,
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
            self._record_audit(connection, result.id, result.name, actor_id, "update",
                               _card_changes(current, result), result.updated_at)
        assert result is not None
        return result

    def delete_guest(self, id: int, expected_version: int, actor_id: int | None = None) -> bool:
        """Return True when deleted; missing/stale cards raise StaleGuestError."""
        if actor_id is not None:
            _validate_actor(actor_id)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _guest(connection.execute("SELECT * FROM guests WHERE id = ?", (id,)).fetchone())
            if current is None or current.version != expected_version:
                raise StaleGuestError("Карточка уже изменена или удалена. Откройте её заново.")
            cursor = connection.execute("DELETE FROM guests WHERE id = ? AND version = ?", (id, expected_version))
            if cursor.rowcount != 1:
                raise StaleGuestError("Карточка уже изменена или удалена. Откройте её заново.")
            self._record_audit(connection, current.id, current.name, actor_id, "delete",
                               _card_changes(current, None), _timestamp())
        return True

    def start_visit(self, guest_id: int, expected_version: int, actor_id: int,
                    *, now: datetime | None = None) -> Visit:
        """Start one visit; old card buttons cannot start a later visit."""
        _validate_actor(actor_id)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            guest = connection.execute("SELECT version, name FROM guests WHERE id = ?", (guest_id,)).fetchone()
            if guest is None or guest["version"] != expected_version:
                raise StaleGuestError("Карточка уже изменена или удалена. Откройте её заново.")
            if connection.execute(
                "SELECT 1 FROM visits WHERE guest_id = ? AND stopped_at IS NULL", (guest_id,)
            ).fetchone() is not None:
                raise VisitStateError("Отсчёт времени для этого гостя уже запущен.")
            started_at = _timestamp(now)
            try:
                cursor = connection.execute("""
                    INSERT INTO visits (guest_id, started_at, started_by)
                    VALUES (?, ?, ?)
                """, (guest_id, started_at, actor_id))
            except sqlite3.IntegrityError as exc:
                if "visits.guest_id" in str(exc):
                    raise VisitStateError("Отсчёт времени для этого гостя уже запущен.") from exc
                raise
            connection.execute("""
                UPDATE guests SET version = version + 1, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (started_at, actor_id, guest_id))
            result = _visit(connection.execute("SELECT * FROM visits WHERE id = ?", (cursor.lastrowid,)).fetchone())
            assert result is not None
            self._record_audit(connection, guest_id, guest["name"], actor_id, "start", {
                "visit_id": {"before": None, "after": result.id},
                "started_at": {"before": None, "after": started_at},
            }, started_at)
        assert result is not None
        return result

    def stop_visit(self, guest_id: int, visit_id: int, actor_id: int,
                   *, now: datetime | None = None) -> Visit:
        """Stop the exact active visit; an old button never stops a new one."""
        _validate_actor(actor_id)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _visit(connection.execute("""
                SELECT * FROM visits WHERE id = ? AND guest_id = ? AND stopped_at IS NULL
            """, (visit_id, guest_id)).fetchone())
            if current is None:
                raise VisitStateError("Этот отсчёт уже завершён или удалён. Откройте карточку заново.")
            stopped = _utc_datetime(now)
            if stopped < datetime.fromisoformat(current.started_at):
                raise ValueError("Время завершения не может быть раньше начала визита.")
            stopped_at = _timestamp(stopped)
            connection.execute("""
                UPDATE visits SET stopped_at = ?, stopped_by = ?
                WHERE id = ? AND guest_id = ? AND stopped_at IS NULL
            """, (stopped_at, actor_id, visit_id, guest_id))
            connection.execute("""
                UPDATE guests SET version = version + 1, updated_at = ?, updated_by = ?
                WHERE id = ?
            """, (stopped_at, actor_id, guest_id))
            result = _visit(connection.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone())
            guest_name = connection.execute("SELECT name FROM guests WHERE id = ?", (guest_id,)).fetchone()[0]
            self._record_audit(connection, guest_id, guest_name, actor_id, "stop", {
                "visit_id": {"before": visit_id, "after": visit_id},
                "stopped_at": {"before": None, "after": stopped_at},
            }, stopped_at)
        assert result is not None
        return result

    def get_visit_summary(self, guest_id: int, *, now: datetime | None = None) -> VisitSummary:
        """Read a consistent total; missing/deleted guests have an empty total."""
        moment = _utc_datetime(now)
        with self._connection() as connection, connection:
            # Keep both reads in one snapshot even when another employee stops
            # or starts a visit between them.
            connection.execute("BEGIN")
            active = _visit(connection.execute("""
                SELECT * FROM visits WHERE guest_id = ? AND stopped_at IS NULL
            """, (guest_id,)).fetchone())
            completed = connection.execute("""
                SELECT started_at, stopped_at FROM visits
                WHERE guest_id = ? AND stopped_at IS NOT NULL
            """, (guest_id,)).fetchall()
        # Python datetime arithmetic avoids SQLite julianday floating point
        # rounding (an exact hour must never become 3,599 seconds).
        completed_seconds = sum(max(0, int((datetime.fromisoformat(row["stopped_at"])
                                            - datetime.fromisoformat(row["started_at"])).total_seconds()))
                                for row in completed)
        return VisitSummary(active=active, completed_count=len(completed),
                            completed_seconds=completed_seconds,
                            current_seconds=active.duration_seconds(moment) if active else 0)

    def list_visits(self, guest_id: int, limit: int = 5, offset: int = 0) -> list[Visit]:
        """Newest start first, including the current active visit if present."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Размер страницы должен быть от 1 до 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Смещение должно быть неотрицательным целым числом.")
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT * FROM visits WHERE guest_id = ?
                ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?
            """, (guest_id, limit, offset)).fetchall()
        return [_visit(row) for row in rows]  # type: ignore[misc]

    def _record_audit(self, connection: sqlite3.Connection, guest_id: int, guest_name: str,
                      actor_id: int | None, action: str, changes: dict[str, object],
                      created_at: str) -> None:
        """Called in the same write transaction as the successful operation."""
        connection.execute("""
            INSERT INTO audit_events (guest_id, guest_name, actor_id, action, changes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guest_id, guest_name, actor_id, action,
              json.dumps(changes, ensure_ascii=False, separators=(",", ":")), created_at))

    def list_audit(self, guest_id: int | None = None, limit: int = 5,
                   offset: int = 0) -> list[AuditEvent]:
        """Committed mutation order, newest first; deletion keeps this history."""
        _validate_page(limit, offset)
        if guest_id is not None:
            _validate_identifier(guest_id)
        where = " WHERE guest_id = ?" if guest_id is not None else ""
        params = [guest_id] if guest_id is not None else []
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM audit_events" + where +
                                      " ORDER BY id DESC LIMIT ? OFFSET ?",
                                      [*params, limit, offset]).fetchall()
        return [_audit_event(row) for row in rows]

    def count_audit(self, guest_id: int | None = None) -> int:
        if guest_id is not None:
            _validate_identifier(guest_id)
        where = " WHERE guest_id = ?" if guest_id is not None else ""
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM audit_events" + where,
                                          [guest_id] if guest_id is not None else []).fetchone()[0])

    def get_audit(self, event_id: int) -> AuditEvent | None:
        _validate_identifier(event_id)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        return _audit_event(row) if row is not None else None

    def remember_staff(self, actor_id: int, name: str, username: str | None = None) -> None:
        """Remember names from authorized Telegram updates, never grant access."""
        _validate_actor(actor_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "\x00" in name:
            raise ValueError("Некорректное имя сотрудника.")
        name = " ".join(name.split())
        if username is None:
            username = ""
        if not isinstance(username, str):
            raise ValueError("Некорректный Telegram username сотрудника.")
        username = username.strip().removeprefix("@")
        if username and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
            raise ValueError("Некорректный Telegram username сотрудника.")
        with self._connection() as connection, connection:
            current = connection.execute("SELECT name, username FROM staff WHERE actor_id = ?",
                                         (actor_id,)).fetchone()
            if current and current["name"] == name and current["username"] == username:
                return
            connection.execute("""
                INSERT INTO staff (actor_id, name, username, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id) DO UPDATE SET name=excluded.name,
                    username=excluded.username, updated_at=excluded.updated_at
            """, (actor_id, name, username, _timestamp()))

    def staff_label(self, actor_id: int | None) -> str:
        if actor_id is None:
            return "Сотрудник не указан"
        _validate_actor(actor_id)
        with self._connection() as connection:
            row = connection.execute("SELECT name, username FROM staff WHERE actor_id = ?",
                                     (actor_id,)).fetchone()
        if row is None:
            return f"Telegram ID {actor_id}"
        return f'{row["name"]} (@{row["username"]})' if row["username"] else row["name"]

    def overdue_visits(self, *, now: datetime | None = None,
                       threshold_seconds: int = 86400) -> list[tuple[Guest, Visit]]:
        """All active visits past the threshold; do not read photo BLOBs."""
        if (isinstance(threshold_seconds, bool) or not isinstance(threshold_seconds, int)
                or threshold_seconds <= 0):
            raise ValueError("Порог напоминания должен быть положительным числом секунд.")
        moment = _utc_datetime(now)
        guest_columns = ", ".join("X'' AS guest_photo" if field == "photo"
                                  else f"g.{field} AS guest_{field}"
                                  for field in Guest.__dataclass_fields__)
        visit_columns = ", ".join(f"v.{field} AS visit_{field}" for field in Visit.__dataclass_fields__)
        with self._connection() as connection:
            rows = connection.execute(f"""
                SELECT {guest_columns}, {visit_columns} FROM visits AS v
                JOIN guests AS g ON g.id = v.guest_id
                WHERE v.stopped_at IS NULL ORDER BY v.started_at, v.id
            """).fetchall()
        results = []
        for row in rows:
            visit = Visit(**{field: row[f"visit_{field}"] for field in Visit.__dataclass_fields__})
            if visit.duration_seconds(moment) >= threshold_seconds:
                guest = Guest(**{field: row[f"guest_{field}"] for field in Guest.__dataclass_fields__})
                results.append((guest, visit))
        return results

    def reminder_sent(self, visit_id: int, recipient_id: int) -> bool:
        _validate_identifier(visit_id)
        _validate_actor(recipient_id)
        with self._connection() as connection:
            return connection.execute("""
                SELECT 1 FROM visit_reminders WHERE visit_id = ? AND recipient_id = ?
            """, (visit_id, recipient_id)).fetchone() is not None

    def mark_reminder_sent(self, visit_id: int, recipient_id: int,
                           *, now: datetime | None = None) -> bool:
        """Call only after successful sending; atomically ignore stopped visits."""
        _validate_identifier(visit_id)
        _validate_actor(recipient_id)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            sent_at = _timestamp(now)
            cursor = connection.execute("""
                INSERT OR IGNORE INTO visit_reminders (visit_id, recipient_id, sent_at)
                SELECT id, ?, ? FROM visits WHERE id = ? AND stopped_at IS NULL
            """, (recipient_id, sent_at, visit_id))
            return cursor.rowcount == 1

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
