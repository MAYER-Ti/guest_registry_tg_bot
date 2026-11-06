"""Authenticated, fail-closed storage client for the private Google Sheets bridge.

Every RPC has one stable operation ID and signature, including retries after an
ambiguous network failure. The Apps Script bridge must persist operation results
under that ID before replying. Google-owned ContentService redirects are allowed;
credentials, payloads, server error messages and URLs are never logged.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
import hmac
import json
import re
import time
from typing import Callable, Iterable
from urllib import error, request
from urllib.parse import urlsplit
from uuid import uuid4

from storage import (
    AuditEvent, DuplicatePhoneError, EmployeeAccess, Guest, MAX_PHOTO_BYTES,
    StaleGuestError, Visit, VisitStateError, VisitSummary, _utc_datetime,
    _validate_fields, _validate_page, normalize_phone,
)


MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
_ENDPOINT_PATH = re.compile(r"/macros/s/[A-Za-z0-9_-]+/exec")
_EDIT_FIELDS = {"name", "phone", "comment", "photo", "photo_file_id", "entry_status", "entry_reason"}
_UNAVAILABLE = "Не удалось обратиться к облачной базе. Данные не заменены пустой базой. Попробуй позже."
_BAD_RESPONSE = "Облачная база вернула некорректные данные. Операция остановлена."


class SheetsStorageError(RuntimeError):
    """Cloud data could not be read or safely verified; never imply an empty DB."""


def _safe_url(url: str, *, redirect: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)):
            return False
        if redirect:
            return parsed.hostname == "script.googleusercontent.com" and parsed.path == "/macros/echo"
        return (parsed.hostname == "script.google.com" and not parsed.query
                and _ENDPOINT_PATH.fullmatch(parsed.path) is not None)
    except ValueError:
        return False


class _GoogleRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Apps Script ContentService returns its result through a temporary URL.
        # Never follow login pages, arbitrary hosts, or POST-preserving redirects.
        if code not in (301, 302, 303) or not _safe_url(newurl, redirect=True):
            raise SheetsStorageError(_UNAVAILABLE)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None or redirected.get_method() != "GET" or redirected.data is not None:
            raise SheetsStorageError(_UNAVAILABLE)
        return redirected


def _http_post(url: str, body: bytes, timeout: float) -> bytes:
    opener = request.build_opener(_GoogleRedirects())
    outgoing = request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
        "User-Agent": "PC-Arena-Guest-Bot/1",
    })
    with opener.open(outgoing, timeout=timeout) as response:
        final_url = response.geturl()
        if not (_safe_url(final_url) or _safe_url(final_url, redirect=True)):
            raise SheetsStorageError(_UNAVAILABLE)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SheetsStorageError("Полный ответ облачной базы слишком большой. Данные не обрезаны.")
    return raw


def _integer(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise ValueError("Некорректный числовой идентификатор.")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректное время.")
    moment = datetime.fromisoformat(value)
    _utc_datetime(moment)
    return value


def _now(value: datetime | None) -> str | None:
    return _utc_datetime(value).isoformat(timespec="microseconds") if value is not None else None


def _object(value: object, fields: Iterable[str]) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Некорректная структура.")
    return value


def _photo(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > ((MAX_PHOTO_BYTES + 2) // 3) * 4:
        raise ValueError("Некорректное фото.")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Некорректное фото.") from None
    if len(result) > MAX_PHOTO_BYTES:
        raise ValueError("Некорректное фото.")
    return result


def decode_guest(value: object, *, require_photo: bool = False) -> Guest:
    """Decode and validate a complete Guest wire object; raises ValueError."""
    row = _object(value, Guest.__dataclass_fields__)
    photo = _photo(row["photo"])
    if require_photo and not photo:
        raise ValueError("Фото карточки недоступно.")
    for field in ("id", "created_by", "updated_by", "version"):
        _integer(row[field])
    for field in ("created_at", "updated_at"):
        _timestamp(row[field])
    checked = _validate_fields(
        name=row["name"], phone=row["phone"], comment=row["comment"], photo=photo or b"\0",
        photo_file_id=row["photo_file_id"], entry_status=row["entry_status"], entry_reason=row["entry_reason"],
    )
    if row["phone_key"] != checked["phone_key"]:
        raise ValueError("Некорректный телефон карточки.")
    return Guest(**{**row, "photo": photo})


def decode_visit(value: object) -> Visit:
    """Decode and validate a complete Visit wire object; raises ValueError."""
    row = _object(value, Visit.__dataclass_fields__)
    for field in ("id", "guest_id", "started_by"):
        _integer(row[field])
    _timestamp(row["started_at"])
    if (row["stopped_at"] is None) != (row["stopped_by"] is None):
        raise ValueError("Некорректный визит.")
    if row["stopped_at"] is not None:
        _timestamp(row["stopped_at"])
        _integer(row["stopped_by"])
        if datetime.fromisoformat(row["stopped_at"]) < datetime.fromisoformat(row["started_at"]):
            raise ValueError("Некорректный визит.")
    return Visit(**row)


def _audit(value: object) -> AuditEvent:
    row = _object(value, AuditEvent.__dataclass_fields__)
    _integer(row["id"])
    _integer(row["guest_id"])
    if row["actor_id"] is not None:
        _integer(row["actor_id"])
    _timestamp(row["created_at"])
    if (not isinstance(row["guest_name"], str) or not row["guest_name"]
            or row["action"] not in {"add", "update", "delete", "start", "stop"}
            or not isinstance(row["changes"], dict)):
        raise ValueError("Некорректное событие.")
    for field, change in row["changes"].items():
        if not isinstance(field, str):
            raise ValueError("Некорректное поле события.")
        _object(change, ("before", "after"))
        if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in change.values()):
            raise ValueError("Некорректное значение события.")
    return AuditEvent(**row)


def _employee(value: object) -> EmployeeAccess:
    row = _object(value, EmployeeAccess.__dataclass_fields__)
    _integer(row["user_id"])
    _integer(row["added_by"])
    _timestamp(row["added_at"])
    return EmployeeAccess(**row)


def _wire(value):
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_wire(item) for item in value]
    return value


class SheetsStore:
    """Synchronous Store-compatible facade; call through asyncio.to_thread."""
    backend = "google_sheets"

    def __init__(
        self, url: str, secret: str, *, expected_owner_email: str,
        expected_admin_id: int = 7011639945,
        transport: Callable[[str, bytes, float], bytes] | None = None,
    ):
        if not _safe_url(url):
            raise ValueError("Укажите HTTPS-адрес опубликованного Google Apps Script, оканчивающийся на /exec.")
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
            raise ValueError("Секрет облачной базы должен содержать не менее 32 байт.")
        if not isinstance(expected_owner_email, str) or not expected_owner_email.strip():
            raise ValueError("Укажите владельца облачной базы.")
        _integer(expected_admin_id)
        self._url = url
        self._secret = secret.encode("utf-8")
        self._transport = transport or _http_post
        health = self._rpc("health", {})
        try:
            if (not isinstance(health, dict) or health.get("ready") is not True
                    or type(health.get("version")) is not int or health["version"] != 1
                    or type(health.get("schema_version")) is not int or health["schema_version"] != 1
                    or health.get("owner_email", "").strip().casefold() != expected_owner_email.strip().casefold()
                    or type(health.get("admin_id")) is not int or health["admin_id"] != expected_admin_id):
                raise ValueError("Cloud identity mismatch")
        except (AttributeError, TypeError, ValueError):
            raise SheetsStorageError("Не удалось подтвердить владельца и структуру облачной базы. Запуск остановлен.") from None

    def __repr__(self):
        return "SheetsStore(backend='google_sheets')"

    def _rpc(self, action: str, params: dict):
        payload = json.dumps({"version": 1, "operation_id": uuid4().hex, "issued_at": int(time.time()),
                              "action": action, "params": _wire(params)}, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
        signature = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        body = json.dumps({"payload": payload, "signature": signature}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        # An Apps Script backup/import may run for several minutes. Do not
        # automatically start another ambiguous long operation after timeout;
        # the server's receipt or empty-destination guard allows reconciliation.
        long_operation = action in {"create_backup", "import_state", "restore_backup"}
        attempts, timeout = (1, 300.0) if long_operation else (3, 40.0)
        for attempt in range(attempts):
            try:
                raw = self._transport(self._url, body, timeout)
            except error.HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code <= 599
                if not transient or attempt + 1 == attempts:
                    raise SheetsStorageError(_UNAVAILABLE) from None
                delay = float(2**attempt)
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after:
                        try:
                            requested = float(retry_after)
                        except ValueError:
                            raise SheetsStorageError(_UNAVAILABLE) from None
                        if requested < 0 or requested > 10 or requested != requested:
                            raise SheetsStorageError(_UNAVAILABLE) from None
                        delay = max(delay, requested)
                time.sleep(delay)
                continue
            except (error.URLError, TimeoutError, OSError):
                if attempt + 1 == attempts:
                    raise SheetsStorageError(_UNAVAILABLE) from None
                time.sleep(2**attempt)
                continue
            try:
                if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Response too large")
                response = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ValueError("Invalid response")
                if response["ok"]:
                    if "result" not in response:
                        raise ValueError("Missing result")
                    return response["result"]
                failure = response.get("error")
                if not isinstance(failure, dict) or not isinstance(failure.get("code"), str):
                    raise ValueError("Invalid failure")
            except (UnicodeDecodeError, ValueError, TypeError):
                raise SheetsStorageError(_BAD_RESPONSE) from None
            code = failure["code"]
            mapped = {
                "DUPLICATE_PHONE": (DuplicatePhoneError, "Гость с таким телефоном уже есть."),
                "STALE_GUEST": (StaleGuestError, "Карточка уже изменена или удалена. Откройте её заново."),
                "VISIT_STATE": (VisitStateError, "Этот отсчёт уже изменён. Откройте карточку заново."),
                "VALIDATION": (ValueError, "Проверь данные и попробуй ещё раз."),
                "PERMISSION": (PermissionError, "Недостаточно прав для этой операции."),
            }
            if code in mapped:
                kind, message = mapped[code]
                raise kind(message)
            raise SheetsStorageError(_UNAVAILABLE)
        raise SheetsStorageError(_UNAVAILABLE)

    def _decoded(self, action, params, decoder):
        result = self._rpc(action, params)
        try:
            return decoder(result)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise SheetsStorageError(_BAD_RESPONSE) from None

    @staticmethod
    def _boolean(value):
        if type(value) is not bool:
            raise ValueError("Invalid boolean")
        return value

    @staticmethod
    def _count(value):
        return _integer(value, minimum=0)

    @staticmethod
    def _optional(decoder):
        return lambda value: None if value is None else decoder(value)

    @staticmethod
    def _match(decoder, **attributes):
        def decode(value):
            result = decoder(value)
            if any(getattr(result, name) != expected for name, expected in attributes.items()):
                raise ValueError("Wrong object identity")
            return result
        return decode

    @staticmethod
    def _list(decoder, limit=None):
        def decode(value):
            if not isinstance(value, list) or limit is not None and len(value) > limit:
                raise ValueError("Invalid list")
            return [decoder(item) for item in value]
        return decode

    def add_guest(self, *, name, phone, comment, photo, photo_file_id, actor_id,
                  entry_status="open", entry_reason="") -> Guest:
        _integer(actor_id)
        _validate_fields(name=name, phone=phone, comment=comment, photo=photo,
                         photo_file_id=photo_file_id, entry_status=entry_status, entry_reason=entry_reason)
        return self._decoded("add_guest", {"name": name, "phone": phone, "comment": comment,
            "photo": photo, "photo_file_id": photo_file_id, "actor_id": actor_id,
            "entry_status": entry_status, "entry_reason": entry_reason},
            lambda result: decode_guest(result, require_photo=True))

    def get_guest(self, id: int) -> Guest | None:
        _integer(id)
        return self._decoded("get_guest", {"id": id},
                             self._optional(self._match(lambda result: decode_guest(result, require_photo=True), id=id)))

    def find_phone(self, phone: str) -> Guest | None:
        phone_key = normalize_phone(phone)
        return self._decoded("find_phone", {"phone": phone},
                             self._optional(self._match(lambda result: decode_guest(result, require_photo=True), phone_key=phone_key)))

    @staticmethod
    def _query(query):
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("Поисковый запрос должен содержать не более 100 символов.")

    def search_guests(self, query: str, limit: int = 8, offset: int = 0) -> list[Guest]:
        self._query(query)
        _validate_page(limit, offset)
        return self._decoded("search_guests", {"query": query, "limit": limit, "offset": offset},
                             self._list(decode_guest, limit))

    def count_search(self, query: str) -> int:
        self._query(query)
        return self._decoded("count_search", {"query": query}, self._count)

    def update_guest(self, id: int, expected_version: int, actor_id: int, **fields) -> Guest:
        for value in (id, expected_version, actor_id):
            _integer(value)
        if not fields or fields.keys() - _EDIT_FIELDS:
            raise ValueError("Укажите только изменяемые поля карточки.")
        # The server validates the merged card atomically under its write lock.
        for field, value in fields.items():
            if field == "photo":
                if not isinstance(value, bytes) or not value or len(value) > MAX_PHOTO_BYTES:
                    raise ValueError("Нужно фото размером не более 10 МБ.")
            elif field == "phone":
                normalize_phone(value)
            elif field == "entry_status":
                if value not in ("open", "closed"):
                    raise ValueError("Некорректный статус входа.")
            elif not isinstance(value, str):
                raise ValueError("Поле карточки должно быть текстом.")
            elif field == "name" and (not value.strip() or len(" ".join(value.split())) > 100):
                raise ValueError("Некорректное имя.")
            elif field == "photo_file_id" and (not value.strip() or len(value) > 1024):
                raise ValueError("Некорректный идентификатор фото.")
            elif field in {"comment", "entry_reason"} and len(value) > 3000:
                raise ValueError("Текст должен содержать не более 3000 символов.")
        return self._decoded("update_guest", {"id": id, "expected_version": expected_version,
                             "actor_id": actor_id, "fields": fields},
                             self._match(lambda result: decode_guest(result, require_photo=True), id=id))

    def delete_guest(self, id: int, expected_version: int, actor_id: int | None = None) -> bool:
        _integer(id)
        _integer(expected_version)
        if actor_id is not None:
            _integer(actor_id)
        return self._decoded("delete_guest", {"id": id, "expected_version": expected_version,
                             "actor_id": actor_id}, self._boolean)

    def start_visit(self, guest_id: int, expected_version: int, actor_id: int,
                    *, now: datetime | None = None) -> Visit:
        for value in (guest_id, expected_version, actor_id):
            _integer(value)
        return self._decoded("start_visit", {"guest_id": guest_id, "expected_version": expected_version,
                             "actor_id": actor_id, "now": _now(now)}, self._match(decode_visit, guest_id=guest_id))

    def stop_visit(self, guest_id: int, visit_id: int, actor_id: int,
                   *, now: datetime | None = None) -> Visit:
        for value in (guest_id, visit_id, actor_id):
            _integer(value)
        return self._decoded("stop_visit", {"guest_id": guest_id, "visit_id": visit_id,
                             "actor_id": actor_id, "now": _now(now)}, self._match(decode_visit, guest_id=guest_id, id=visit_id))

    def get_visit_summary(self, guest_id: int, *, now: datetime | None = None) -> VisitSummary:
        _integer(guest_id)
        def decode(value):
            row = _object(value, VisitSummary.__dataclass_fields__)
            active = decode_visit(row["active"]) if row["active"] is not None else None
            if active and active.guest_id != guest_id:
                raise ValueError("Wrong guest")
            for field in ("completed_count", "completed_seconds", "current_seconds"):
                self._count(row[field])
            if active is None and row["current_seconds"] != 0:
                raise ValueError("Inactive duration")
            return VisitSummary(**{**row, "active": active})
        return self._decoded("get_visit_summary", {"guest_id": guest_id, "now": _now(now)}, decode)

    def list_visits(self, guest_id: int, limit: int = 5, offset: int = 0) -> list[Visit]:
        _integer(guest_id)
        _validate_page(limit, offset)
        return self._decoded("list_visits", {"guest_id": guest_id, "limit": limit, "offset": offset},
                             self._list(self._match(decode_visit, guest_id=guest_id), limit))

    def list_audit(self, guest_id: int | None = None, limit: int = 5, offset: int = 0) -> list[AuditEvent]:
        if guest_id is not None:
            _integer(guest_id)
        _validate_page(limit, offset)
        return self._decoded("list_audit", {"guest_id": guest_id, "limit": limit, "offset": offset},
                             self._list(self._match(_audit, guest_id=guest_id) if guest_id is not None else _audit, limit))

    def count_audit(self, guest_id: int | None = None) -> int:
        if guest_id is not None:
            _integer(guest_id)
        return self._decoded("count_audit", {"guest_id": guest_id}, self._count)

    def get_audit(self, event_id: int) -> AuditEvent | None:
        _integer(event_id)
        return self._decoded("get_audit", {"event_id": event_id}, self._optional(self._match(_audit, id=event_id)))

    def remember_staff(self, actor_id: int, name: str, username: str | None = None) -> None:
        _integer(actor_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "\0" in name:
            raise ValueError("Некорректное имя сотрудника.")
        if username is not None and (not isinstance(username, str)
                or username.strip().removeprefix("@") and
                not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username.strip().removeprefix("@"))):
            raise ValueError("Некорректный Telegram username сотрудника.")
        self._decoded("remember_staff", {"actor_id": actor_id, "name": name, "username": username},
                      lambda result: None if result is None else (_ for _ in ()).throw(ValueError()))

    def staff_label(self, actor_id: int | None) -> str:
        if actor_id is not None:
            _integer(actor_id)
        def decode(result):
            if not isinstance(result, str) or not result.strip() or len(result) > 400:
                raise ValueError("Invalid label")
            return result
        return self._decoded("staff_label", {"actor_id": actor_id}, decode)

    def bootstrap_employee_access(self, initial_ids: Iterable[int], admin_id: int) -> None:
        _integer(admin_id)
        ids = list(initial_ids)
        for value in ids:
            # Preserve the legacy documented placeholder without granting it.
            if type(value) is not int or value != 2**53:
                _integer(value)
        self._decoded("bootstrap_employee_access", {"initial_ids": ids, "admin_id": admin_id},
                      lambda result: None if result is None else (_ for _ in ()).throw(ValueError()))

    def add_employee(self, user_id: int, actor_id: int) -> bool:
        _integer(user_id)
        _integer(actor_id)
        return self._decoded("add_employee", {"user_id": user_id, "actor_id": actor_id}, self._boolean)

    def has_employee_access(self, user_id: int) -> bool:
        _integer(user_id)
        return self._decoded("has_employee_access", {"user_id": user_id}, self._boolean)

    def employee_ids(self) -> frozenset[int]:
        ids = self._decoded("employee_ids", {}, self._list(_integer))
        if len(ids) != len(set(ids)):
            raise SheetsStorageError(_BAD_RESPONSE)
        return frozenset(ids)

    def list_employees(self, limit: int = 8, offset: int = 0) -> list[EmployeeAccess]:
        _validate_page(limit, offset)
        return self._decoded("list_employees", {"limit": limit, "offset": offset}, self._list(_employee, limit))

    def count_employees(self) -> int:
        return self._decoded("count_employees", {}, self._count)

    def overdue_visits(self, *, now: datetime | None = None, threshold_seconds: int = 86400) -> list[tuple[Guest, Visit]]:
        _integer(threshold_seconds)
        def decode(item):
            row = _object(item, ("guest", "visit"))
            guest, visit = decode_guest(row["guest"]), decode_visit(row["visit"])
            if visit.guest_id != guest.id or visit.stopped_at is not None:
                raise ValueError("Invalid overdue visit")
            return guest, visit
        return self._decoded("overdue_visits", {"now": _now(now), "threshold_seconds": threshold_seconds},
                             self._list(decode))

    def reminder_sent(self, visit_id: int, recipient_id: int) -> bool:
        _integer(visit_id)
        _integer(recipient_id)
        return self._decoded("reminder_sent", {"visit_id": visit_id, "recipient_id": recipient_id}, self._boolean)

    def mark_reminder_sent(self, visit_id: int, recipient_id: int, *, now: datetime | None = None) -> bool:
        _integer(visit_id)
        _integer(recipient_id)
        return self._decoded("mark_reminder_sent", {"visit_id": visit_id, "recipient_id": recipient_id,
                             "now": _now(now)}, self._boolean)

    def snapshot(self, *, include_photos: bool = False, now: datetime | None = None,
                 photo_transform: Callable[[bytes], bytes] | None = None) -> dict:
        if type(include_photos) is not bool or photo_transform is not None and not callable(photo_transform):
            raise ValueError("Некорректные параметры отчёта.")
        def decode(result):
            row = _object(result, ("generated_at", "guests", "visits"))
            _timestamp(row["generated_at"])
            # Apps Script Date normalizes to milliseconds, Python accepts
            # microseconds. Preserve the returned instant while allowing only
            # that sub-millisecond representation loss, not an unrelated time.
            if now is not None and abs((datetime.fromisoformat(row["generated_at"])
                                       - _utc_datetime(now)).total_seconds()) >= 0.001:
                raise ValueError("Wrong report instant")
            if not isinstance(row["guests"], list):
                raise ValueError("Invalid report guests")
            guest_rows, photo_refs = [], []
            for item in row["guests"]:
                _object(item, (*Guest.__dataclass_fields__, "photo_ref"))
                photo_ref = item["photo_ref"]
                if not isinstance(photo_ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", photo_ref):
                    raise ValueError("Invalid report photo reference")
                photo_refs.append(photo_ref)
                guest_rows.append({key: value for key, value in item.items() if key != "photo_ref"})
            guests = self._list(decode_guest)(guest_rows)
            visits = self._list(decode_visit)(row["visits"])
            if len({guest.id for guest in guests}) != len(guests) or len({visit.id for visit in visits}) != len(visits):
                raise ValueError("Duplicate report IDs")
            ids = {guest.id for guest in guests}
            if any(visit.guest_id not in ids for visit in visits):
                raise ValueError("Unknown report guest")
            active = [visit.guest_id for visit in visits if visit.stopped_at is None]
            if len(set(active)) != len(active):
                raise ValueError("Multiple active visits")
            # Return wire JSON for the reporting adapter. Never silently drop
            # rows or photos because validation/thumbnail generation failed.
            if any(guest.photo for guest in guests):
                raise ValueError("Unexpected report photos")
            row = {**row, "guests": guest_rows}
            if include_photos:
                # Snapshot metadata pins immutable photo references. Read one
                # original at a time, replacing it immediately with a thumbnail
                # when requested. Photo files remain private in Drive.
                for item, photo_ref in zip(guest_rows, photo_refs):
                    original = self._decoded("get_photo", {"photo_ref": photo_ref}, _photo)
                    if not original:
                        raise ValueError("Photo unavailable")
                    transformed = photo_transform(original) if photo_transform is not None else original
                    if not isinstance(transformed, bytes) or len(transformed) > MAX_PHOTO_BYTES:
                        raise ValueError("Invalid thumbnail")
                    item["photo"] = base64.b64encode(transformed).decode("ascii")
            return row
        return self._decoded("snapshot", {"include_photos": False, "now": _now(now)}, decode)

    def create_backup(self, *, only_if_due: bool = True) -> dict:
        if type(only_if_due) is not bool:
            raise ValueError("Некорректный режим резервного копирования.")
        def decode(value):
            row = _object(value, ("complete", "folder_id", "manifest_file_id", "created_at", "photo_count", "counts"))
            if row["complete"] is not True:
                raise ValueError("Incomplete backup")
            for field in ("folder_id", "manifest_file_id"):
                if not isinstance(row[field], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", row[field]):
                    raise ValueError("Invalid backup identifier")
            _timestamp(row["created_at"])
            self._count(row["photo_count"])
            counts = _object(row["counts"], ("guests", "visits", "employees", "audit_events"))
            for count in counts.values():
                self._count(count)
            return row
        return self._decoded("create_backup", {"only_if_due": only_if_due}, decode)

    def stage_photo(self, photo: bytes, *, admin_id: int = 7011639945) -> dict:
        """Upload one import photo without assembling a huge aggregate request."""
        _integer(admin_id)
        if not isinstance(photo, bytes) or not photo or len(photo) > MAX_PHOTO_BYTES:
            raise ValueError("Нужно фото размером не более 10 МБ.")
        def decode(value):
            row = _object(value, ("photo_ref", "bytes"))
            if (not isinstance(row["photo_ref"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", row["photo_ref"])
                    or self._count(row["bytes"]) != len(photo)):
                raise ValueError("Invalid staged photo")
            return row
        return self._decoded("stage_photo", {"photo": photo, "admin_id": admin_id}, decode)

    def restore_backup(self, manifest_file_id: str, *, admin_id: int = 7011639945) -> dict:
        """Restore a complete private manifest into a deliberately fresh store."""
        _integer(admin_id)
        if not isinstance(manifest_file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", manifest_file_id):
            raise ValueError("Некорректный идентификатор резервной копии.")
        def decode(value):
            row = _object(value, ("restored", "guests", "visits", "employees", "audit_events"))
            if row["restored"] is not True:
                raise ValueError("Restore was not acknowledged")
            for key in ("guests", "visits", "employees", "audit_events"):
                self._count(row[key])
            return row
        return self._decoded("restore_backup", {"manifest_file_id": manifest_file_id, "admin_id": admin_id}, decode)

    def import_state(self, state: dict, *, admin_id: int = 7011639945,
                     sequences: dict | None = None) -> dict:
        """Import a preserved SQLite export into an explicitly empty destination.

        The bridge validates source records and atomically refuses a nonempty
        destination. This client only serializes a copy of the supplied state;
        the caller's original objects and photo bytes are not changed.
        """
        _integer(admin_id)
        keys = ("guests", "visits", "audit_events", "staff", "employee_access", "visit_reminders")
        _object(state, keys)
        if any(not isinstance(state[key], list) for key in keys):
            raise ValueError("Экспорт должен содержать все разделы базы данных.")
        params = {"state": state, "admin_id": admin_id}
        if sequences is not None:
            _object(sequences, ("guests", "visits", "audit_events"))
            for count in sequences.values():
                self._count(count)
            params["sequences"] = sequences
        expected = {"guests": len(state["guests"]), "visits": len(state["visits"]),
                    "employees": len(state["employee_access"]), "audit_events": len(state["audit_events"])}
        def decode(value):
            row = _object(value, ("imported", *expected))
            if row["imported"] is not True:
                raise ValueError("Import was not acknowledged")
            for key, count in expected.items():
                if self._count(row[key]) != count:
                    raise ValueError("Import counts do not match")
            return row
        return self._decoded("import_state", params, decode)
