"""Private, durable reminders for visits left running for at least 24 hours.

The sole production reminder loop sends to the employee who started the visit,
or to the first remaining allowlisted employee. Delivery is recorded only after
Telegram accepts the message. A crash between acceptance and the database write
can therefore repeat a reminder; a failed delivery remains eligible for retry.
No reminder stops a visit automatically.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage import Guest, Store, Visit


REMINDER_AFTER_SECONDS = 24 * 60 * 60
MESSAGE_INTERVAL_SECONDS = 1.05
MOSCOW = timezone(timedelta(hours=3))
logger = logging.getLogger(__name__)


class ReminderRateLimit(RuntimeError):
    """Stop a batch and defer the sole reminder loop for Telegram's cooldown."""

    def __init__(self, retry_after: int | float):
        if (isinstance(retry_after, bool) or not isinstance(retry_after, (int, float))
                or not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("Некорректное время ожидания Telegram.")
        self.retry_after = float(retry_after)
        super().__init__("Telegram ограничил частоту отправки напоминаний.")


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс.")
    return now.astimezone(timezone.utc)


def _recipients(allowed_user_ids: Iterable[int]) -> tuple[int, ...]:
    # A private chat ID must be a positive, explicit allowlist entry.
    return tuple(sorted({value for value in allowed_user_ids
                         if type(value) is int and value > 0}))


def _reminder_text(guest: Guest, visit: Visit, now: datetime) -> str:
    hours, remainder = divmod(visit.duration_seconds(now), 3600)
    minutes, seconds = divmod(remainder, 60)
    started = datetime.fromisoformat(visit.started_at).astimezone(MOSCOW)
    return (
        "⏰ Проверьте таймер гостя\n\n"
        f"Гость: {guest.name}\n"
        f"Телефон: {guest.phone}\n"
        f"Начало: {started:%d.%m.%Y %H:%M:%S} МСК\n"
        f"Текущий визит: {hours} ч. {minutes:02d} мин. {seconds:02d} сек.\n\n"
        "Таймер работает уже 24 часа или дольше. Гость ещё у вас? "
        "Если он ушёл, нажмите «Стоп»."
    )


async def send_overdue_reminders(
    bot: Bot, store: Store, allowed_user_ids: Iterable[int], *,
    now: datetime | None = None,
) -> int:
    """Send each due visit's reminder once per recipient; return sends accepted.

    Recheck the active visit immediately before sending. Stop buttons carry the
    exact visit ID, so a delayed reminder cannot stop a subsequent visit.
    Telegram 429 aborts the batch with ``ReminderRateLimit``; accepted earlier
    messages remain durably marked, and every unsent item stays eligible.
    """
    recipients = _recipients(allowed_user_ids)
    if not recipients:
        return 0
    current = _utc_now(now)
    visits = await asyncio.to_thread(
        store.overdue_visits, now=current,
        threshold_seconds=REMINDER_AFTER_SECONDS,
    )
    sent = 0
    last_attempt: float | None = None
    loop = asyncio.get_running_loop()
    for guest, visit in visits:
        recipient = visit.started_by if visit.started_by in recipients else recipients[0]
        try:
            if await asyncio.to_thread(store.reminder_sent, visit.id, recipient):
                continue
            if last_attempt is not None:
                wait = MESSAGE_INTERVAL_SECONDS - (loop.time() - last_attempt)
                if wait > 0:
                    await asyncio.sleep(wait)
            # Recheck after pacing, so a stop during the delay skips this send.
            summary = await asyncio.to_thread(store.get_visit_summary, guest.id, now=current)
            if (summary.active is None or summary.active.id != visit.id
                    or summary.active.duration_seconds(current) < REMINDER_AFTER_SECONDS):
                continue
            last_attempt = loop.time()
            await bot.send_message(
                chat_id=recipient,
                text=_reminder_text(guest, summary.active, current),
                parse_mode=None,
                protect_content=True,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📋 Открыть карточку", callback_data=f"view:{guest.id}"),
                    InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{guest.id}:{visit.id}"),
                ]]),
            )
            sent += 1
            await asyncio.to_thread(store.mark_reminder_sent, visit.id, recipient, now=current)
        except TelegramRetryAfter as exc:
            logger.warning("Отправка напоминаний приостановлена (%s).", type(exc).__name__)
            raise ReminderRateLimit(exc.retry_after) from None
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError) as exc:
            logger.warning("Не удалось отправить напоминание (%s).", type(exc).__name__)
        except Exception as exc:
            # Guest data, bot tokens and SQLite paths must never enter logs.
            logger.error("Не удалось обработать напоминание (%s).", type(exc).__name__)
    return sent


async def reminder_loop(
    bot: Bot, store: Store, allowed_user_ids: Iterable[int], *,
    interval_seconds: float = 300,
) -> None:
    """Check immediately on startup and then periodically; cancellation escapes."""
    if (isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds) or interval_seconds <= 0):
        raise ValueError("Интервал проверки должен быть положительным числом.")
    recipients = _recipients(allowed_user_ids)
    while True:
        delay = interval_seconds
        try:
            await send_overdue_reminders(bot, store, recipients)
        except ReminderRateLimit as exc:
            delay = max(interval_seconds, exc.retry_after + 0.1)
            logger.warning("Telegram ограничил частоту отправки напоминаний; ожидаем повторной проверки.")
        except Exception as exc:
            logger.error("Не удалось проверить забытые таймеры (%s).", type(exc).__name__)
        await asyncio.sleep(delay)
