"""Reminder delivery uses fake Telegram transport and durable local SQLite."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

import reminders
from storage import Guest, Store, Visit, VisitSummary


UTC = timezone.utc
START = datetime(2026, 10, 9, 10, 30, 0, tzinfo=UTC)
NOW = START + timedelta(days=1)


def guest(id=1):
    return Guest(
        id=id, name=f"Гость <{id}>", phone="+7 999 123-45-67", phone_key="79991234567",
        comment="", photo=b"", photo_file_id="photo", created_by=101, updated_by=101,
        created_at=START.isoformat(), updated_at=START.isoformat(), version=1,
    )


def visit(id=1, guest_id=1, started_by=101, started_at=START):
    return Visit(id, guest_id, started_at.isoformat(), None, started_by, None)


class FakeStore:
    def __init__(self, candidates=None):
        self.candidates = [(guest(), visit())] if candidates is None else candidates
        self.active = {card.id: item for card, item in self.candidates}
        self.delivered = set()
        self.calls = []
        self.before_summary = None
        self.before_mark = None

    def overdue_visits(self, *, now, threshold_seconds):
        self.calls.append(("overdue", now, threshold_seconds))
        return self.candidates

    def reminder_sent(self, visit_id, recipient_id):
        self.calls.append(("sent", visit_id, recipient_id))
        return (visit_id, recipient_id) in self.delivered

    def get_visit_summary(self, guest_id, *, now):
        if self.before_summary:
            self.before_summary(self)
        active = self.active.get(guest_id)
        return VisitSummary(active, 0, 0, active.duration_seconds(now) if active else 0)

    def mark_reminder_sent(self, visit_id, recipient_id, *, now):
        if self.before_mark:
            self.before_mark(self)
        key = (visit_id, recipient_id)
        if key in self.delivered or not any(item.id == visit_id for item in self.active.values()):
            return False
        self.delivered.add(key)
        return True


class ReminderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = AsyncMock()
        self.store = FakeStore()
        sleep_patch = patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    async def send(self, allowed=(101, 102), *, now=NOW):
        return await reminders.send_overdue_reminders(self.bot, self.store, allowed, now=now)

    async def test_threshold_message_buttons_and_private_recipient(self):
        self.assertEqual(await self.send(), 1)
        message = self.bot.send_message.await_args.kwargs
        self.assertEqual(message["chat_id"], 101)
        self.assertIsNone(message["parse_mode"])
        self.assertTrue(message["protect_content"])
        self.assertIn("Гость <1>", message["text"])
        self.assertIn("+7 999 123-45-67", message["text"])
        self.assertIn("09.10.2026 13:30:00 МСК", message["text"])
        self.assertIn("24 ч. 00 мин. 00 сек.", message["text"])
        self.assertIn("нажмите «Стоп»", message["text"])
        self.assertEqual([button.callback_data for button in message["reply_markup"].inline_keyboard[0]],
                         ["view:1", "stop:1:1"])
        self.assertEqual(self.store.delivered, {(1, 101)})
        self.assertEqual(self.store.calls[0], ("overdue", NOW, 86400))

    async def test_before_24_hours_does_not_send_even_with_stale_candidate(self):
        self.assertEqual(await self.send(now=NOW - timedelta(microseconds=1)), 0)
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.store.delivered)

    async def test_duplicate_check_does_not_repeat_accepted_message(self):
        self.assertEqual(await self.send(), 1)
        self.assertEqual(await self.send(now=NOW + timedelta(days=10)), 0)
        self.bot.send_message.assert_awaited_once()

    async def test_starter_is_preferred_over_lowest_staff_id(self):
        self.store = FakeStore([(guest(), visit(started_by=102))])
        await self.send()
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 102)

    async def test_removed_starter_falls_back_to_lowest_allowed_id(self):
        self.store = FakeStore([(guest(), visit(started_by=999))])
        await self.send(allowed=(103, 102, 102, -777, True, "101"))
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 102)
        self.assertEqual(self.store.delivered, {(1, 102)})

    async def test_empty_or_invalid_allowlist_never_reads_database_or_sends(self):
        self.assertEqual(await self.send(allowed=()), 0)
        self.assertEqual(await self.send(allowed=(-1, True, "101", None)), 0)
        self.assertEqual(self.store.calls, [])
        self.bot.send_message.assert_not_awaited()

    async def test_stopped_deleted_or_replaced_visit_is_skipped(self):
        for active in (None, visit(id=2)):
            with self.subTest(active=active):
                self.store.active = {} if active is None else {1: active}
                self.assertEqual(await self.send(), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_stop_between_candidate_scan_and_summary_recheck_is_skipped(self):
        self.store.before_summary = lambda store: store.active.clear()
        self.assertEqual(await self.send(), 0)
        self.bot.send_message.assert_not_awaited()

    async def test_stop_after_delivery_does_not_record_inactive_visit(self):
        self.store.before_mark = lambda store: store.active.clear()
        self.assertEqual(await self.send(), 1)
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(self.store.delivered)

    async def test_telegram_failures_are_sanitized_and_retried_next_check(self):
        method = SendMessage(chat_id=101, text="private guest token=secret")
        errors = (
            TelegramForbiddenError(method=method, message="private token=secret"),
            TelegramBadRequest(method=method, message="private token=secret"),
            TelegramNetworkError(method=method, message="private token=secret"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.bot.send_message.reset_mock()
                self.bot.send_message.side_effect = error
                with self.assertLogs("reminders", level="WARNING") as logs:
                    with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                        self.assertEqual(await self.send(), 0)
                        sleep.assert_not_awaited()
                self.assertIn(type(error).__name__, " ".join(logs.output))
                self.assertNotIn("secret", " ".join(logs.output))
                self.assertFalse(self.store.delivered)
                self.bot.send_message.side_effect = None
                self.assertEqual(await self.send(), 1)
                self.store.delivered.clear()

    async def test_failure_for_one_visit_does_not_block_another(self):
        self.store = FakeStore([(guest(), visit()), (guest(2), visit(id=2, guest_id=2))])
        method = SendMessage(chat_id=101, text="test")
        self.bot.send_message.side_effect = [TelegramNetworkError(method=method, message="failed"), None]
        with self.assertLogs("reminders", level="WARNING"):
            self.assertEqual(await self.send(), 1)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.store.delivered, {(2, 101)})
        self.sleep.assert_awaited_once()
        self.assertGreater(self.sleep.await_args.args[0], 0)
        self.assertLessEqual(self.sleep.await_args.args[0], reminders.MESSAGE_INTERVAL_SECONDS)

    async def test_429_stops_whole_batch_and_exposes_only_sanitized_cooldown(self):
        self.store = FakeStore([(guest(), visit()), (guest(2), visit(id=2, guest_id=2))])
        method = SendMessage(chat_id=101, text="private guest token=secret")
        self.bot.send_message.side_effect = TelegramRetryAfter(
            method=method, message="private token=secret", retry_after=86400,
        )
        with self.assertLogs("reminders", level="WARNING") as logs:
            with self.assertRaises(reminders.ReminderRateLimit) as caught:
                await self.send()
        self.assertEqual(caught.exception.retry_after, 86400)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", " ".join(logs.output))
        self.bot.send_message.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertFalse(self.store.delivered)

    async def test_429_after_success_keeps_first_mark_and_does_not_attempt_remaining(self):
        self.store = FakeStore([(guest(index), visit(id=index, guest_id=index)) for index in (1, 2, 3)])
        method = SendMessage(chat_id=101, text="private guest token=secret")
        self.bot.send_message.side_effect = [
            None, TelegramRetryAfter(method=method, message="private token=secret", retry_after=100), None,
        ]
        with self.assertLogs("reminders", level="WARNING"):
            with self.assertRaises(reminders.ReminderRateLimit):
                await self.send()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.store.delivered, {(1, 101)})
        self.sleep.assert_awaited_once()
        self.bot.send_message.reset_mock()
        self.bot.send_message.side_effect = None
        self.sleep.reset_mock()
        self.assertEqual(await self.send(), 2)
        self.assertEqual(self.store.delivered, {(1, 101), (2, 101), (3, 101)})
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.sleep.assert_awaited_once()

    async def test_successful_batch_is_paced_even_between_different_recipients(self):
        self.store = FakeStore([
            (guest(1), visit(id=1, guest_id=1, started_by=101)),
            (guest(2), visit(id=2, guest_id=2, started_by=102)),
            (guest(3), visit(id=3, guest_id=3, started_by=101)),
        ])
        self.assertEqual(await self.send(), 3)
        self.assertEqual(self.sleep.await_count, 2)
        for call in self.sleep.await_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLessEqual(call.args[0], reminders.MESSAGE_INTERVAL_SECONDS)

    async def test_stop_during_pacing_is_rechecked_before_delivery(self):
        self.store = FakeStore([(guest(), visit()), (guest(2), visit(id=2, guest_id=2))])

        async def stop_second(_delay):
            self.store.active.pop(2)

        self.sleep.side_effect = stop_second
        self.assertEqual(await self.send(), 1)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.delivered, {(1, 101)})

    async def test_cancellation_during_pacing_propagates_after_only_first_mark(self):
        self.store = FakeStore([(guest(), visit()), (guest(2), visit(id=2, guest_id=2))])
        self.sleep.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.delivered, {(1, 101)})

    async def test_cancellation_propagates_without_delivery_mark(self):
        self.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.send()
        self.assertFalse(self.store.delivered)

    async def test_invalid_naive_time_is_rejected_before_read(self):
        with self.assertRaises(ValueError):
            await self.send(now=NOW.replace(tzinfo=None))
        self.assertEqual(self.store.calls, [])

    async def test_timezone_aware_time_is_normalized_to_utc(self):
        await self.send(now=NOW.astimezone(timezone(timedelta(hours=-4))))
        self.assertEqual(self.store.calls[0][1], NOW)
        self.assertEqual(self.store.calls[0][1].tzinfo, UTC)


class ReminderLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_immediately_and_retries_after_sanitized_failure(self):
        allowed = (value for value in (102, 101))
        send = AsyncMock(side_effect=[RuntimeError("private token=secret"), asyncio.CancelledError()])
        with patch.object(reminders, "send_overdue_reminders", send):
            with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("reminders", level="ERROR") as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await reminders.reminder_loop("bot", "store", allowed)
        self.assertEqual(send.await_count, 2)
        self.assertEqual(send.await_args_list[0].args, ("bot", "store", (101, 102)))
        self.assertEqual(send.await_args_list[1].args, ("bot", "store", (101, 102)))
        sleep.assert_awaited_once_with(300)
        self.assertNotIn("secret", " ".join(logs.output))

    async def test_rate_limit_waits_full_cooldown_before_next_batch(self):
        send = AsyncMock(side_effect=[reminders.ReminderRateLimit(86400), asyncio.CancelledError()])
        with patch.object(reminders, "send_overdue_reminders", send):
            with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("reminders", level="WARNING"):
                    with self.assertRaises(asyncio.CancelledError):
                        await reminders.reminder_loop("bot", "store", (101,), interval_seconds=300)
        sleep.assert_awaited_once_with(86400.1)
        self.assertEqual(send.await_count, 2)

    async def test_short_rate_limit_keeps_regular_check_interval(self):
        send = AsyncMock(side_effect=[reminders.ReminderRateLimit(1), asyncio.CancelledError()])
        with patch.object(reminders, "send_overdue_reminders", send):
            with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("reminders", level="WARNING"):
                    with self.assertRaises(asyncio.CancelledError):
                        await reminders.reminder_loop("bot", "store", (101,), interval_seconds=300)
        sleep.assert_awaited_once_with(300)

    async def test_cancel_during_rate_limit_cooldown_propagates(self):
        send = AsyncMock(side_effect=reminders.ReminderRateLimit(86400))
        with patch.object(reminders, "send_overdue_reminders", send):
            with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock,
                              side_effect=asyncio.CancelledError()) as sleep:
                with self.assertLogs("reminders", level="WARNING"):
                    with self.assertRaises(asyncio.CancelledError):
                        await reminders.reminder_loop("bot", "store", (101,))
        sleep.assert_awaited_once_with(86400.1)
        send.assert_awaited_once()

    async def test_rate_limit_value_validation(self):
        for value in (True, -1, float("inf"), float("nan"), "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reminders.ReminderRateLimit(value)

    async def test_cancel_during_wait_propagates(self):
        send = AsyncMock(return_value=0)
        with patch.object(reminders, "send_overdue_reminders", send):
            with patch.object(reminders.asyncio, "sleep", new_callable=AsyncMock,
                              side_effect=asyncio.CancelledError()):
                with self.assertRaises(asyncio.CancelledError):
                    await reminders.reminder_loop("bot", "store", (101,), interval_seconds=10)
        send.assert_awaited_once()

    async def test_invalid_interval_fails_before_sending(self):
        for interval in (0, -1, True, float("inf"), float("nan"), "300"):
            with self.subTest(interval=interval):
                with patch.object(reminders, "send_overdue_reminders", new_callable=AsyncMock) as send:
                    with self.assertRaises(ValueError):
                        await reminders.reminder_loop("bot", "store", (101,), interval_seconds=interval)
                    send.assert_not_awaited()


class ReminderDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path.cwd() / ".test-runtime" / uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = Store(self.root / "guests.sqlite3")
        self.card = self.store.add_guest(
            name="Александр", phone="89991234567", comment="", photo=b"original-photo",
            photo_file_id="photo", actor_id=101,
        )
        self.active = self.store.start_visit(self.card.id, self.card.version, 101, now=START)
        self.bot = AsyncMock()

    async def test_sent_marker_survives_restart_and_next_visit_gets_new_reminder(self):
        self.assertEqual(await reminders.send_overdue_reminders(self.bot, self.store, (101,), now=NOW), 1)
        self.assertTrue(self.store.reminder_sent(self.active.id, 101))
        reopened = Store(self.store.path)
        self.assertEqual(await reminders.send_overdue_reminders(self.bot, reopened, (101,),
                                                               now=NOW + timedelta(days=1)), 0)
        reopened.stop_visit(self.card.id, self.active.id, 101, now=NOW)
        fresh_card = reopened.get_guest(self.card.id)
        next_visit = reopened.start_visit(self.card.id, fresh_card.version, 101,
                                         now=NOW + timedelta(hours=1))
        self.assertEqual(await reminders.send_overdue_reminders(self.bot, reopened, (101,),
                                                               now=NOW + timedelta(days=1, hours=1)), 1)
        self.assertTrue(reopened.reminder_sent(next_visit.id, 101))
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data,
                         f"stop:{self.card.id}:{next_visit.id}")
        self.assertEqual(reopened.get_visit_summary(self.card.id).active.id, next_visit.id)

    async def test_actual_storage_threshold_and_failed_delivery_does_not_mark(self):
        self.assertEqual(await reminders.send_overdue_reminders(self.bot, self.store, (101,),
                                                               now=NOW - timedelta(microseconds=1)), 0)
        self.bot.send_message.assert_not_awaited()
        method = SendMessage(chat_id=101, text="test")
        self.bot.send_message.side_effect = TelegramNetworkError(method=method, message="offline")
        with self.assertLogs("reminders", level="WARNING"):
            self.assertEqual(await reminders.send_overdue_reminders(self.bot, self.store, (101,), now=NOW), 0)
        self.assertFalse(Store(self.store.path).reminder_sent(self.active.id, 101))
        self.bot.send_message.side_effect = None
        self.assertEqual(await reminders.send_overdue_reminders(self.bot, self.store, (101,), now=NOW), 1)


if __name__ == "__main__":
    unittest.main()
