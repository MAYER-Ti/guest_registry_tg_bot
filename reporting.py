"""Read-only, all-or-nothing reports over explicitly connected SQLite files.

Each source is read inside one SQLite transaction. Independent files do not
share an atomic transaction, but every row within one source comes from the
same snapshot. Reading never initializes a missing database or migrates an
older one. Backups and other files are never discovered automatically.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

from storage import Guest, Store, Visit, VisitSummary


class ReportError(RuntimeError):
    """A connected source failed; the report must not expose partial results."""


@dataclass(frozen=True, slots=True)
class ReportGuest:
    source_index: int
    source_name: str
    guest: Guest
    summary: VisitSummary
    visits: tuple[Visit, ...]


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    generated_at: datetime
    sources: tuple[str, ...]
    rows: tuple[ReportGuest, ...]

    @property
    def total_count(self) -> int:
        return len(self.rows)

    @property
    def blacklist(self) -> tuple[ReportGuest, ...]:
        return tuple(row for row in self.rows if row.guest.entry_status == "closed")


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Время отчёта должно содержать часовой пояс.")
    return now.astimezone(timezone.utc)


def _source_name(index: int) -> str:
    return "Основная база" if index == 1 else f"База {index}"


def _failure(name: str) -> ReportError:
    # No database path, SQLite error, or row content should reach chat/logs.
    return ReportError(f"Не удалось прочитать «{name}». Проверьте подключение базы данных.")


def _unique_paths(primary: Store | Path | str, extras: Iterable[Path | str]) -> tuple[Path, ...]:
    candidates = (primary.path if isinstance(primary, Store) else primary, *extras)
    result: list[Path] = []
    for candidate in candidates:
        name = _source_name(len(result) + 1)
        try:
            path = Path(candidate).expanduser().resolve()
            duplicate = path in result
            if not duplicate:
                for existing in result:
                    try:
                        if path.samefile(existing):
                            duplicate = True
                            break
                    except OSError:
                        # Missing/unreadable sources are rejected by their own
                        # connection, rather than silently skipped here.
                        pass
            if not duplicate:
                result.append(path)
        except (OSError, TypeError, ValueError, RuntimeError):
            raise _failure(name) from None
    return tuple(result)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # table is an internal constant, never a user-supplied SQL identifier.
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _valid_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Invalid timestamp timezone")
    return parsed


def _read_source(path: Path, index: int, include_photos: bool, now: datetime,
                 photo_transform: Callable[[bytes], bytes] | None) -> tuple[ReportGuest, ...]:
    name = _source_name(index)
    try:
        # mode=ro refuses missing files; URI escaping protects spaces, #, ?
        # and other filename characters. Query-only is a second write guard.
        uri = path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            if "guests" not in tables:
                raise ValueError("Missing guests table")
            columns = _columns(connection, "guests")
            field_names = tuple(Guest.__dataclass_fields__)
            required = set(field_names) - {"entry_status", "entry_reason"}
            if not required <= columns:
                raise ValueError("Incompatible guests schema")
            projection = []
            for field in field_names:
                if field == "photo" and not include_photos:
                    projection.append("X'' AS photo")
                elif field == "entry_status" and field not in columns:
                    projection.append("'open' AS entry_status")
                elif field == "entry_reason" and field not in columns:
                    projection.append("'' AS entry_reason")
                else:
                    projection.append(field)
            guest_rows = connection.execute(
                "SELECT " + ", ".join(projection) + " FROM guests ORDER BY id"
            )
            guests: dict[int, Guest] = {}
            for row in guest_rows:
                values = {field: row[field] for field in field_names}
                if include_photos and photo_transform is not None:
                    if not isinstance(values["photo"], bytes):
                        raise ValueError("Invalid photo bytes")
                    try:
                        values["photo"] = photo_transform(values["photo"])
                    except Exception:
                        raise ValueError("Photo transformation failed") from None
                    if not isinstance(values["photo"], bytes):
                        raise ValueError("Invalid transformed photo bytes")
                guest = Guest(**values)
                if (not all(_valid_int(getattr(guest, field)) for field in
                            ("id", "created_by", "updated_by", "version"))
                        or not all(isinstance(getattr(guest, field), str) for field in
                                   ("name", "phone", "phone_key", "comment", "photo_file_id",
                                    "created_at", "updated_at", "entry_status", "entry_reason"))
                        or not isinstance(guest.photo, bytes)
                        or guest.entry_status not in {"open", "closed"}
                        or guest.id in guests):
                    raise ValueError("Invalid guest row")
                _valid_timestamp(guest.created_at)
                _valid_timestamp(guest.updated_at)
                guests[guest.id] = guest

            grouped_visits: dict[int, list[Visit]] = {id: [] for id in guests}
            if "visits" in tables:
                visit_fields = tuple(Visit.__dataclass_fields__)
                if not set(visit_fields) <= _columns(connection, "visits"):
                    raise ValueError("Incompatible visits schema")
                seen_visit_ids: set[int] = set()
                for row in connection.execute("SELECT " + ", ".join(visit_fields) + " FROM visits"):
                    visit = Visit(**{field: row[field] for field in visit_fields})
                    if (not all(_valid_int(getattr(visit, field)) for field in
                                ("id", "guest_id", "started_by"))
                            or visit.guest_id not in guests or visit.id in seen_visit_ids
                            or (visit.stopped_at is None) != (visit.stopped_by is None)
                            or (visit.stopped_by is not None and not _valid_int(visit.stopped_by))):
                        raise ValueError("Invalid visit row")
                    started = _valid_timestamp(visit.started_at)
                    if visit.stopped_at is not None and _valid_timestamp(visit.stopped_at) < started:
                        raise ValueError("Negative visit duration")
                    seen_visit_ids.add(visit.id)
                    grouped_visits[visit.guest_id].append(visit)
            result: list[ReportGuest] = []
            for guest in guests.values():
                visits = tuple(sorted(grouped_visits[guest.id],
                                      key=lambda visit: (_valid_timestamp(visit.started_at), visit.id),
                                      reverse=True))
                active = [visit for visit in visits if visit.stopped_at is None]
                if len(active) > 1:
                    raise ValueError("Multiple active visits")
                completed = [visit for visit in visits if visit.stopped_at is not None]
                summary = VisitSummary(
                    active[0] if active else None,
                    len(completed),
                    sum(visit.duration_seconds(now) for visit in completed),
                    active[0].duration_seconds(now) if active else 0,
                )
                result.append(ReportGuest(index, name, guest, summary, visits))
            connection.rollback()
            return tuple(result)
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        raise _failure(name) from None


def report_snapshot(primary: Store | Path | str, extras: Iterable[Path | str] = (), *,
                    include_photos: bool = False, now: datetime | None = None,
                    photo_transform: Callable[[bytes], bytes] | None = None) -> ReportSnapshot:
    """Read every connected source, failing the entire report if any fails.

    Database identity is deduplicated, while cards in different databases keep
    their own identities even when guest IDs or telephone numbers overlap.
    Photo BLOBs are excluded unless explicitly requested. An optional photo
    transform runs per row while streaming the database cursor, so exports can
    retain only small thumbnails rather than every full-sized original.
    """
    instant = _utc_now(now)
    paths = _unique_paths(primary, extras)
    rows: list[ReportGuest] = []
    for index, path in enumerate(paths, start=1):
        rows.extend(_read_source(path, index, include_photos, instant, photo_transform))
    return ReportSnapshot(instant, tuple(_source_name(index) for index in range(1, len(paths) + 1)),
                          tuple(rows))
