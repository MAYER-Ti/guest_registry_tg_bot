"""One administrator grants persistent employee access to the shared registry.

Telegram profile names and usernames never grant permissions. The sole admin
is supplied by the application's configuration, not selected by a Telegram
command, a forwarded message or an employee database row.
"""
from __future__ import annotations

from config import Config
from storage import Store


MAX_TELEGRAM_USER_ID = 2**52 - 1


def _telegram_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_TELEGRAM_USER_ID


def parse_employee_id(raw: str) -> int:
    """Parse the numeric ID employees obtain from the bot's /myid command."""
    if not isinstance(raw, str):
        raise ValueError("Пришли числовой Telegram ID сотрудника из команды /myid.")
    value = raw.strip()
    if not value or len(value) > 16 or not value.isascii() or not value.isdecimal():
        raise ValueError("Пришли числовой Telegram ID сотрудника из команды /myid.")
    result = int(value)
    if not _telegram_id(result):
        raise ValueError("Telegram ID должен быть положительным числом из команды /myid.")
    return result


class AccessPolicy:
    """Owner-only grants; existing employees cannot appoint or become admins."""

    def __init__(self, config: Config, store: Store):
        if not _telegram_id(config.admin_user_id):
            raise ValueError("Некорректный Telegram ID администратора.")
        self.admin_user_id = config.admin_user_id
        self.store = store
        store.bootstrap_employee_access(config.allowed_user_ids, self.admin_user_id)

    def is_admin(self, user_id: int) -> bool:
        return type(user_id) is int and user_id == self.admin_user_id

    def allows(self, user_id: int) -> bool:
        if not _telegram_id(user_id):
            return False
        return self.is_admin(user_id) or self.store.has_employee_access(user_id)

    def add_employee(self, actor_id: int, target_id: int) -> bool:
        # Check actor first, before target validation and before any DB call.
        if not self.is_admin(actor_id):
            raise PermissionError("Добавлять сотрудников может только администратор.")
        if not _telegram_id(target_id):
            raise ValueError("Некорректный Telegram ID сотрудника.")
        if target_id == self.admin_user_id:
            raise ValueError("Этот ID уже является администратором.")
        return self.store.add_employee(target_id, actor_id)

    def allowed_ids(self) -> frozenset[int]:
        """Take a fresh permission snapshot, including newly added employees."""
        return frozenset({self.admin_user_id} | {
            user_id for user_id in self.store.employee_ids() if _telegram_id(user_id)
        })
