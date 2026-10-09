"""Activity, audit and attendance UI against real SQLite and fake Telegram."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import User

import main
import reporting
import test_bot as workflow_helpers
from config import Config
from storage import Store


UTC = timezone.utc
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)


class ActivityBotTests(unittest.IsolatedAsyncioTestCase):
    # Reuse update construction, without inheriting and duplicating old tests.
    message = workflow_helpers.BotWorkflowTests.message
    callback = workflow_helpers.BotWorkflowTests.callback
    draft = workflow_helpers.BotWorkflowTests.draft
    button = staticmethod(workflow_helpers.BotWorkflowTests.button)
    card_text = staticmethod(workflow_helpers.BotWorkflowTests.card_text)

    async def asyncSetUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.store = Store(self.runtime / "guests.sqlite3")
        self.session = workflow_helpers.RecordingSession()
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", session=self.session)
        self.dp = None
        self.update_id = 0
        self.staff = {
            101: ("Михаил", "Mihun_pihun13"),
            102: ("Дмитрий", "shift_second"),
            103: ("Ольга", None),
            104: ("Алексей", "shift_fourth"),
        }
        self.photo_patch = patch.object(main, "read_photo", new_callable=AsyncMock,
                                       return_value=(b"fake-photo", "stored-photo"))
        self.photo_patch.start()
        self.sleep_patch = patch.object(main.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep_patch.start()
        await self.configure()

    async def asyncTearDown(self):
        self.photo_patch.stop()
        self.sleep_patch.stop()
        if self.dp:
            await self.dp.storage.close()
            await self.dp.fsm.events_isolation.close()
        await self.bot.session.close()
        shutil.rmtree(self.runtime)
        try:
            self.runtime.parent.rmdir()
        except OSError:
            pass

    async def configure(self, extra_paths=()):
        if self.dp:
            await self.dp.storage.close()
            await self.dp.fsm.events_isolation.close()
        self.dp = main.build_dispatcher(
            Config(token=self.bot.token, allowed_user_ids=frozenset(self.staff),
                   db_path=self.store.path, extra_db_paths=tuple(extra_paths)), self.store,
        )

    def person(self, user_id=101):
        name, username = self.staff.get(user_id, ("Посторонний", "outsider"))
        return User(id=user_id, is_bot=False, first_name=name, username=username)

    def seed(self, index=1, *, name=None, store=None, reason="", status="open"):
        return (store or self.store).add_guest(
            name=name or f"Гость {index:03d}", phone=f"+7999123{index:04d}", comment="Комментарий",
            photo=b"seed-photo", photo_file_id=f"photo-{index}", actor_id=101,
            entry_status=status, entry_reason=reason,
        )

    def begin(self, guest, start, *, actor_id=101, store=None):
        source = store or self.store
        current = source.get_guest(guest.id)
        return source.start_visit(current.id, current.version, actor_id, now=start)

    def completed(self, guest, start, seconds, *, store=None):
        source = store or self.store
        item = self.begin(guest, start, store=source)
        return source.stop_visit(guest.id, item.id, 102, now=start + timedelta(seconds=seconds))

    @staticmethod
    def texts(calls):
        return "".join(call.text or "" for call in calls if isinstance(call, SendMessage))

    @staticmethod
    def callbacks(calls):
        return [button.callback_data for call in calls
                for row in getattr(getattr(call, "reply_markup", None), "inline_keyboard", [])
                for button in row if button.callback_data]

    @staticmethod
    def report_at(now=NOW):
        def fixed_snapshot(*args, **kwargs):
            kwargs["now"] = now
            return reporting.report_snapshot(*args, **kwargs)
        return patch.object(main, "report_snapshot", side_effect=fixed_snapshot)

    def assert_alert(self, calls):
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in calls))
        self.assertFalse(any(isinstance(call, (SendMessage, SendPhoto)) for call in calls))

    async def assert_no_draft(self):
        context = self.dp.fsm.get_context(bot=self.bot, chat_id=101, user_id=101)
        self.assertIsNone(await context.get_state())
        self.assertEqual(await context.get_data(), {})

    async def test_menu_commands_and_empty_states(self):
        started = await self.message("/start")
        menu = next(call.reply_markup for call in started if isinstance(call, SendMessage))
        labels = {button.text for row in menu.keyboard for button in row}
        self.assertTrue({main.STATS, main.PRESENT, main.AUDIT} <= labels)
        self.assertIn("24 часа", self.texts(started))
        with self.report_at():
            for command in (main.STATS, "/stats", "Статистика"):
                calls = await self.message(command)
                self.assertIn("Посещений: 0\nУникальных гостей: 0", self.texts(calls))
                self.assertIn("Общее время: 0 ч. 00 мин. 00 сек.", self.texts(calls))
                self.assertTrue({"stats:day", "stats:week", "stats:month"} <= set(self.callbacks(calls)))
            for command in (main.PRESENT, "/present", "Сейчас в заведении"):
                calls = await self.message(command)
                self.assertIn("Сейчас в заведении: 0", self.texts(calls))
                self.assertIn("Гостей с запущенным таймером нет", self.texts(calls))
        for command in (main.AUDIT, "/audit", "История изменений"):
            calls = await self.message(command)
            self.assertIn("Изменений пока нет", self.texts(calls))
            self.assertIn("момента включения функции", self.texts(calls))

    async def test_statistics_day_week_month_counts_time_and_frequency(self):
        frequent, yesterday, last_week, active = [self.seed(i) for i in range(1, 5)]
        self.completed(frequent, NOW - timedelta(hours=5), 3600)
        self.completed(frequent, NOW - timedelta(hours=3), 3600)
        self.completed(yesterday, NOW - timedelta(days=1, hours=5), 7200)
        self.completed(last_week, datetime(2026, 10, 2, 10, tzinfo=UTC), 10800)
        self.begin(active, NOW - timedelta(minutes=30))
        with self.report_at():
            for period, visits, unique, hours, label in (
                ("day", 3, 2, 2, "за день"),
                ("week", 4, 3, 4, "за неделю"),
                ("month", 5, 4, 7, "за месяц"),
            ):
                with self.subTest(period=period):
                    calls = await self.callback(f"stats:{period}")
                    text = self.texts(calls)
                    self.assertIn(f"Статистика · {label}", text)
                    self.assertIn(f"Посещений: {visits}\nУникальных гостей: {unique}\n", text)
                    self.assertIn(f"Общее время: {hours} ч. 30 мин. 00 сек.", text)
                    self.assertIn(f"1. {frequent.name} · {frequent.phone}", text)
                    self.assertIn("Посещений: 2 · 2 ч. 00 мин. 00 сек.", text)
                    self.assertIn("включая текущие визиты", text)
                    self.assertIn("МСК", text)
                    self.assertTrue(all(call.protect_content for call in calls if isinstance(call, SendMessage)))

    async def test_statistics_keeps_colliding_guest_ids_from_each_connected_database(self):
        first = self.seed(name="Основной Иван")
        extra = Store(self.runtime / "extra.sqlite3")
        second = self.seed(store=extra, name="Другой Иван")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.phone, second.phone)
        self.completed(first, NOW - timedelta(hours=3), 60)
        self.completed(second, NOW - timedelta(hours=2), 120, store=extra)
        await self.configure((extra.path,))
        with self.report_at():
            calls = await self.message(main.STATS)
        text = self.texts(calls)
        self.assertIn("Посещений: 2\nУникальных гостей: 2", text)
        self.assertIn("Общее время: 0 ч. 03 мин. 00 сек.", text)
        for name in (first.name, second.name, "Основная база", "База 2"):
            self.assertIn(name, text)

    async def test_statistics_missing_connected_source_sends_no_partial_data(self):
        card = self.seed(name="Секретное имя")
        self.completed(card, NOW - timedelta(hours=1), 60)
        missing = self.runtime / "private-connection" / "missing.sqlite3"
        await self.configure((missing,))
        with self.report_at(), self.assertLogs("main", level="WARNING"):
            calls = await self.message(main.STATS)
        text = self.texts(calls)
        self.assertIn("Не удалось получить полный отчёт", text)
        self.assertNotIn(card.name, text)
        self.assertNotIn("Посещений: 1", text)
        self.assertNotIn(str(missing), text)
        self.assertFalse(missing.exists())

    async def test_present_paginates_all_active_cards_oldest_first_and_excludes_inactive_future(self):
        current = datetime.now(UTC)
        cards = {}
        for index in reversed(range(main.PAGE_SIZE + 3)):
            card = self.seed(index=index, name=f"Посетитель-{index:03d}")
            cards[index] = card
            self.begin(card, current - timedelta(days=1) + timedelta(minutes=index))
        inactive = self.seed(index=99, name="Ушедший гость")
        future = self.seed(index=98, name="Будущий таймер")
        self.begin(future, current + timedelta(hours=1))
        with self.report_at(current):
            first = await self.message(main.PRESENT)
            text = self.texts(first)
            self.assertIn(f"Сейчас в заведении: {len(cards)}", text)
            for index in range(main.PAGE_SIZE):
                self.assertIn(cards[index].name, text)
                self.assertIn(cards[index].phone, text)
                self.assertIn(f"view:{cards[index].id}", self.callbacks(first))
            self.assertEqual(sorted(range(main.PAGE_SIZE), key=lambda index: text.index(cards[index].name)),
                             list(range(main.PAGE_SIZE)))
            self.assertNotIn(cards[main.PAGE_SIZE].name, text)
            self.assertNotIn(inactive.name, text)
            self.assertNotIn(future.name, text)
            next_page = self.button(first, "present:8")
            second = await self.callback(next_page)
            second_text = self.texts(second)
            for index in range(main.PAGE_SIZE, len(cards)):
                self.assertIn(cards[index].name, second_text)
            self.assertNotIn(cards[0].name, second_text)
            self.assertIn("present:0", self.callbacks(second))
            last = await self.callback("present:999999")
            self.assertIn(cards[len(cards) - 1].name, self.texts(last))
        self.assertIn("Пришёл:", text)
        self.assertIn("Уже у нас:", text)
        self.assertIn("МСК", text)
        self.assertTrue(all(call.protect_content for call in [*first, *second, *last]
                            if isinstance(call, SendMessage) and "Сейчас в заведении:" in (call.text or "")))

    async def test_present_card_and_stop_buttons_work_and_stale_stop_cannot_stop_new_visit(self):
        current = datetime.now(UTC)
        card = self.seed()
        first_visit = self.begin(card, current - timedelta(hours=1))
        with self.report_at(current):
            calls = await self.message(main.PRESENT)
            open_card = self.button(calls, "view:")
            stop = self.button(calls, "stop:")
            self.assertEqual(stop, f"stop:{card.id}:{first_visit.id}")
            shown = await self.callback(open_card, user_id=102)
            self.assertIn(card.name, self.card_text(shown))
            self.assertIn(main.AUDIT, [button.text for call in shown
                                      for row in getattr(getattr(call, "reply_markup", None), "inline_keyboard", [])
                                      for button in row])
            stopped = await self.callback(stop, user_id=103)
            self.assertIn("Посещение завершено", self.card_text(stopped))
            self.assertIsNone(self.store.get_visit_summary(card.id).active)
            refreshed = await self.callback("present:0")
            self.assertIn("Сейчас в заведении: 0", self.texts(refreshed))
        second_visit = self.begin(card, datetime.now(UTC))
        stale = await self.callback(stop, user_id=104)
        self.assert_alert(stale)
        self.assertEqual(self.store.get_visit_summary(card.id).active.id, second_visit.id)

    async def test_present_only_offers_editable_primary_cards_with_colliding_readonly_source(self):
        primary = self.seed(name="Основной посетитель")
        extra = Store(self.runtime / "extra.sqlite3")
        secondary = self.seed(store=extra, name="Посетитель чужой базы")
        self.assertEqual(primary.id, secondary.id)
        self.begin(primary, NOW - timedelta(hours=2))
        self.begin(secondary, NOW - timedelta(hours=1), store=extra)
        await self.configure((extra.path,))
        with self.report_at():
            calls = await self.message(main.PRESENT)
        self.assertIn("Сейчас в заведении: 1", self.texts(calls))
        self.assertIn(primary.name, self.texts(calls))
        self.assertNotIn(secondary.name, self.texts(calls))

    async def test_audit_status_reason_staff_identity_and_deleted_card_survive_in_history(self):
        save, _ = await self.draft(name="Гость для журнала")
        await self.callback(save)
        card = self.store.find_phone("+79991234567")
        await self.callback(f"field:entry_status:{card.id}:{card.version}", user_id=102)
        await self.message(main.ENTRY_CLOSED, user_id=102)
        await self.message("Первая причина запрета", user_id=102)
        closed = self.store.get_guest(card.id)
        status_event = self.store.list_audit(card.id)[0]
        await self.callback(f"field:entry_reason:{card.id}:{closed.version}", user_id=103)
        await self.message("Уточнённая причина", user_id=103)
        updated = self.store.get_guest(card.id)
        reason_event = self.store.list_audit(card.id)[0]
        started = await self.callback(f"start:{card.id}:{updated.version}", user_id=104)
        await self.callback(self.button(started, "stop:"), user_id=101)
        current = self.store.get_guest(card.id)
        asked = await self.callback(f"delete:{card.id}:{current.version}", user_id=104)
        await self.callback(self.button(asked, "remove:"), user_id=104)
        self.assertIsNone(self.store.get_guest(card.id))
        self.assertEqual(self.store.count_audit(card.id), 6)
        detail = await self.callback(f"auditdetail:{status_event.id}")
        text = self.texts(detail)
        self.assertIn("Сотрудник: Дмитрий (@shift_second)", text)
        self.assertIn("Статус входа: Вход открыт → Вход закрыт", text)
        self.assertIn("Причина: — → Первая причина запрета", text)
        detail = await self.callback(f"auditdetail:{reason_event.id}")
        self.assertIn("Сотрудник: Ольга", self.texts(detail))
        self.assertIn("Причина: Первая причина запрета → Уточнённая причина", self.texts(detail))
        history = await self.callback(f"audit:{card.id}:0")
        full = self.texts(history)
        for label in ("Удалена карточка", "Запущен таймер", "Остановлен таймер", "Гость для журнала"):
            self.assertIn(label, full)
        self.assertIn("Алексей (@shift_fourth)", full)
        self.assertIn("Михаил (@Mihun_pihun13)", full)
        self.assertIn(f"audit:{card.id}:5", self.callbacks(history))
        final = await self.callback(f"audit:{card.id}:5")
        self.assertIn("Создана карточка", self.texts(final))
        self.assertIn("История изменений", self.texts(await self.message(main.AUDIT)))

    async def test_audit_global_pagination_is_complete_and_per_card_scope_does_not_leak_other_cards(self):
        card, unrelated = self.seed(name="Карточка с изменениями"), self.seed(2, name="Другая карточка")
        for index in range(main.AUDIT_PAGE_SIZE + 4):
            card = self.store.update_guest(card.id, card.version, 102, comment=f"Правка {index}")
        expected = [event.id for event in self.store.list_audit(limit=100)]
        current = await self.message(main.AUDIT)
        delivered = []
        while True:
            delivered.extend(int(value) for value in re.findall(r"(?m)^#(\d+) ·", self.texts(current)))
            forward = [value for value in self.callbacks(current) if value.startswith("audit:all:")
                       and value != "audit:all:0"]
            # The only nonzero global navigation on the first page is Next;
            # later pages also offer Back, so follow the largest next offset.
            offset = len(delivered)
            next_value = f"audit:all:{offset}"
            if next_value not in forward:
                break
            current = await self.callback(next_value)
        self.assertEqual(delivered, expected)
        scoped = await self.callback(f"audit:{card.id}:0")
        self.assertIn(card.name, self.texts(scoped))
        self.assertNotIn(unrelated.name, self.texts(scoped))
        self.assertIn("Telegram ID 102", self.texts(scoped))

    async def test_audit_detail_preserves_full_long_unicode_values_and_splits_utf16_safely(self):
        before = "Старая причина\n" + "😀" * 2800
        after = "Новая причина\n" + "🦉" * 2800
        card = self.seed(status="closed", reason=before)
        self.store.update_guest(card.id, card.version, 102, entry_reason=after)
        event = self.store.list_audit(card.id)[0]
        compact = await self.callback(f"audit:{card.id}:0")
        self.assertNotIn(before, self.texts(compact))
        detail = await self.callback(f"auditdetail:{event.id}")
        messages = [call for call in detail if isinstance(call, SendMessage)]
        text = self.texts(detail)
        self.assertIn(before, text)
        self.assertIn(after, text)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(call.text.encode("utf-16-le")) // 2 <= 4000 for call in messages))
        self.assertTrue(all(call.protect_content for call in messages))
        self.assertTrue(all(call.reply_markup is None for call in messages[:-1]))
        self.assertTrue({f"audit:{card.id}:0", "audit:all:0"} <= set(self.callbacks(detail)))

    async def test_all_new_actions_clear_pending_fsm_and_stale_delete_confirmation(self):
        card = self.seed()
        detail_id = self.store.list_audit(card.id)[0].id
        with self.report_at():
            for action in (main.STATS, main.PRESENT, main.AUDIT):
                with self.subTest(action=action):
                    await self.message(main.ADD)
                    await self.message(action)
                    await self.assert_no_draft()
            for action in ("stats:week", "present:0", f"audit:{card.id}:0", f"auditdetail:{detail_id}"):
                with self.subTest(callback=action):
                    await self.message(main.ADD)
                    await self.callback(action)
                    await self.assert_no_draft()
            for action in (main.STATS, main.PRESENT, main.AUDIT):
                asked = await self.callback(f"delete:{card.id}:{card.version}")
                confirmation = self.button(asked, "remove:")
                await self.message(action)
                rejected = await self.callback(confirmation)
                self.assert_alert(rejected)
                self.assertIsNotNone(self.store.get_guest(card.id))

    async def test_access_guard_precedes_reports_audit_reads_and_staff_recording(self):
        card = self.seed()
        with ExitStack() as stack:
            reads = [stack.enter_context(patch.object(main, "report_snapshot",
                                                       side_effect=AssertionError("Unauthorized report read")))]
            for method in ("remember_staff", "count_audit", "list_audit", "get_audit", "staff_label"):
                reads.append(stack.enter_context(patch.object(self.store, method,
                                                              side_effect=AssertionError("Unauthorized storage access"))))
            for user_id, group in ((999, False), (101, True)):
                for action in (main.STATS, main.PRESENT, main.AUDIT):
                    calls = await self.message(action, user_id=user_id, group=group)
                    self.assertNotIn(card.name, self.texts(calls))
                for action in ("stats:day", "present:0", f"audit:{card.id}:0", "auditdetail:1"):
                    calls = await self.callback(action, user_id=user_id, group=group)
                    self.assert_alert(calls)
            for read in reads:
                read.assert_not_called()

    async def test_malformed_new_callbacks_are_rejected_before_report_or_audit_access(self):
        with ExitStack() as stack:
            read = stack.enter_context(patch.object(main, "report_snapshot",
                                                    side_effect=AssertionError("Malformed report read")))
            audit_reads = [stack.enter_context(patch.object(self.store, method,
                                                            side_effect=AssertionError("Malformed audit read")))
                           for method in ("count_audit", "list_audit", "get_audit")]
            for data in (
                "stats:", "stats:year", "stats:day:extra", "stats:DAY",
                "present:", "present:-1", "present:1.5", "present:²", "present:٠", "present:0:extra",
                "present:9999999999999999999",
                "audit:", "audit:all:-1", "audit:0:0", "audit:²:0", "audit:all:²",
                "audit:9999999999999999999:0", "audit:all:9999999999999999999",
                "auditdetail:", "auditdetail:0", "auditdetail:-1", "auditdetail:²", "auditdetail:1:extra",
                "auditdetail:9999999999999999999",
            ):
                with self.subTest(data=data):
                    self.assert_alert(await self.callback(data))
            read.assert_not_called()
            for audit_read in audit_reads:
                audit_read.assert_not_called()

    async def test_missing_audit_detail_returns_alert_without_empty_or_partial_message(self):
        calls = await self.callback("auditdetail:99999")
        self.assert_alert(calls)
        self.assertIn("недоступна", next(call.text for call in calls if isinstance(call, AnswerCallbackQuery)))


if __name__ == "__main__":
    unittest.main()
