"""Owner-only employee grants exercised with real SQLite and fake Telegram."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import User

import main
import test_bot as helpers
from config import Config
from storage import Store


OWNER = 7011639945
WORKER = 101
NEW_WORKER = 2000000201
STRANGER = 2000000999


class AdminBotTests(unittest.IsolatedAsyncioTestCase):
    callback_helper = helpers.BotWorkflowTests.callback
    message_helper = helpers.BotWorkflowTests.message
    draft = helpers.BotWorkflowTests.draft
    button = staticmethod(helpers.BotWorkflowTests.button)
    card_text = staticmethod(helpers.BotWorkflowTests.card_text)

    async def asyncSetUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.store = Store(self.runtime / "registry.sqlite3")
        self.session = helpers.RecordingSession()
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", session=self.session)
        self.update_id = 0
        self.dp = None
        self.photo_patch = patch.object(main, "read_photo", new_callable=AsyncMock,
                                       return_value=(b"fake-photo", "saved-photo"))
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

    async def configure(self, legacy_ids=frozenset({OWNER, WORKER})):
        if self.dp:
            await self.dp.storage.close()
            await self.dp.fsm.events_isolation.close()
        self.dp = main.build_dispatcher(
            Config(token=self.bot.token, allowed_user_ids=legacy_ids, db_path=self.store.path), self.store,
        )

    def person(self, user_id=OWNER):
        return User(id=user_id, is_bot=False, first_name=f"Employee {user_id}",
                    username=f"worker_{user_id}")

    async def message(self, text=None, *, user_id=OWNER, group=False, photo=False):
        return await self.message_helper(text, user_id=user_id, group=group, photo=photo)

    async def callback(self, data, *, user_id=OWNER, group=False):
        return await self.callback_helper(data, user_id=user_id, group=group)

    @staticmethod
    def texts(calls):
        return "\n".join(call.text for call in calls if isinstance(call, SendMessage))

    @staticmethod
    def callbacks(calls):
        return [button.callback_data for call in calls
                for row in getattr(getattr(call, "reply_markup", None), "inline_keyboard", [])
                for button in row if button.callback_data]

    @staticmethod
    def menu_labels(calls):
        return {button.text for call in calls
                for row in getattr(getattr(call, "reply_markup", None), "keyboard", [])
                for button in row}

    async def preview(self, user_id=NEW_WORKER):
        await self.message("/addstaff")
        calls = await self.message(str(user_id))
        return self.button(calls, "staffconfirm:"), calls

    async def grant(self, user_id=NEW_WORKER):
        confirmation, _ = await self.preview(user_id)
        return await self.callback(confirmation)

    async def state_for(self, user_id=OWNER):
        return await self.dp.fsm.get_context(bot=self.bot, chat_id=user_id, user_id=user_id).get_state()

    async def test_owner_alone_sees_employee_management_button(self):
        owner = await self.message("/start")
        worker = await self.message("/start", user_id=WORKER)
        self.assertIn(main.EMPLOYEES, self.menu_labels(owner))
        owner_count = await self.message(main.TOTAL)
        self.assertIn(main.EMPLOYEES, self.menu_labels(owner_count))
        self.assertNotIn(main.EMPLOYEES, self.menu_labels(worker))
        self.assertTrue({main.ADD, main.FIND, main.TOTAL, main.PRESENT, main.STATS} <= self.menu_labels(worker))
        panel = await self.message(main.EMPLOYEES)
        self.assertIn("staff:add", self.callbacks(panel))
        self.assertIn(str(OWNER), self.texts(panel))

    async def test_owner_confirms_new_employee_then_employee_can_create_and_find_guest(self):
        before = await self.message("/start", user_id=NEW_WORKER)
        self.assertNotIn(main.ADD, self.menu_labels(before))
        confirmation, preview = await self.preview()
        self.assertIn(str(NEW_WORKER), self.texts(preview))
        self.assertFalse(self.store.has_employee_access(NEW_WORKER))
        await self.callback(confirmation)
        self.assertTrue(self.store.has_employee_access(NEW_WORKER))
        self.assertIsNone(await self.state_for())
        started = await self.message("/start", user_id=NEW_WORKER)
        self.assertIn(main.ADD, self.menu_labels(started))
        self.assertNotIn(main.EMPLOYEES, self.menu_labels(started))
        save, _ = await self.draft(user_id=NEW_WORKER, name="Гость нового сотрудника",
                                   phone="+79991234567", comment="Внёс новый сотрудник")
        await self.callback(save, user_id=NEW_WORKER)
        guest = self.store.find_phone("+79991234567")
        self.assertIsNotNone(guest)
        self.assertEqual(guest.created_by, NEW_WORKER)
        await self.message(main.FIND, user_id=WORKER)
        found = await self.message("+79991234567", user_id=WORKER)
        self.assertIn("Гость нового сотрудника", self.card_text(found))
        self.assertIn("Внёс новый сотрудник", self.card_text(found))

    async def test_added_employee_keeps_access_after_recreating_store_and_dispatcher(self):
        await self.grant()
        self.store = Store(self.store.path)
        await self.configure(frozenset({OWNER}))
        start = await self.message("/start", user_id=NEW_WORKER)
        self.assertIn(main.ADD, self.menu_labels(start))
        count = await self.message(main.TOTAL, user_id=NEW_WORKER)
        self.assertIn("Всего гостей: 0", self.texts(count))
        self.assertNotIn(main.EMPLOYEES, self.menu_labels(count))

    async def test_profiles_and_myid_never_self_register_a_stranger(self):
        self.store.remember_staff(STRANGER, "Случайный пользователь", "outsider")
        identity = await self.message("/myid", user_id=STRANGER)
        self.assertIn(str(STRANGER), self.texts(identity))
        self.assertFalse(self.store.has_employee_access(STRANGER))
        denied = await self.message(main.ADD, user_id=STRANGER)
        self.assertNotIn(main.ADD, self.menu_labels(denied))
        self.assertIsNone(await self.state_for(STRANGER))

    async def test_workers_and_strangers_cannot_use_management_messages_or_callbacks(self):
        await self.grant()
        confirmation, _ = await self.preview(NEW_WORKER + 1)
        with patch.object(self.store, "add_employee", side_effect=AssertionError("Must not grant")) as write, \
                patch.object(self.store, "list_employees", side_effect=AssertionError("Must not list")) as read:
            for actor in (WORKER, NEW_WORKER, STRANGER):
                for command in ("/staff", "/addstaff", main.EMPLOYEES, main.ADD_EMPLOYEE):
                    with self.subTest(actor=actor, command=command):
                        denied = await self.message(command, user_id=actor)
                        self.assertNotIn("staff:add", self.callbacks(denied))
                        self.assertNotIn("staffconfirm:", "".join(self.callbacks(denied)))
                        self.assertIsNone(await self.state_for(actor))
                for data in ("staff:add", "staff:list:0", confirmation):
                    with self.subTest(actor=actor, callback=data):
                        denied = await self.callback(data, user_id=actor)
                        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in denied))
            write.assert_not_called()
            read.assert_not_called()
        self.assertFalse(self.store.has_employee_access(NEW_WORKER + 1))
        await self.callback(confirmation)
        self.assertTrue(self.store.has_employee_access(NEW_WORKER + 1))

    async def test_owner_management_is_forbidden_in_groups(self):
        with patch.object(self.store, "add_employee", side_effect=AssertionError("Must not grant")) as write, \
                patch.object(self.store, "list_employees", side_effect=AssertionError("Must not list")) as read:
            for command in ("/staff", "/addstaff", main.EMPLOYEES):
                calls = await self.message(command, group=True)
                self.assertNotIn("staff:add", self.callbacks(calls))
            denied = await self.callback("staff:add", group=True)
            self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in denied))
            write.assert_not_called()
            read.assert_not_called()
        self.assertIsNone(await self.state_for())

    async def test_invalid_identifiers_never_create_confirmation_or_access(self):
        original_count = self.store.count_employees()
        for raw in ("@someone", "+79991234567", "0", "-1", "1.5", "١٢٣", "²", "12 34",
                    "9999999999999999999999999", str(2**52), str(OWNER)):
            with self.subTest(raw=raw):
                await self.message("/addstaff")
                calls = await self.message(raw)
                self.assertFalse(any(value.startswith("staffconfirm:") for value in self.callbacks(calls)))
                self.assertEqual(original_count, self.store.count_employees())

    async def test_confirmation_is_bound_to_target_nonce_and_active_draft(self):
        confirmation, _ = await self.preview()
        original_count = self.store.count_employees()
        parts = confirmation.split(":")
        for data in (f"staffconfirm:{NEW_WORKER + 1}:{parts[-1]}",
                     f"staffconfirm:{NEW_WORKER}:wrongnonce", "staffconfirm:²:abc",
                     "staffconfirm:0:abc", "staffconfirm:9223372036854775808:abc"):
            with self.subTest(data=data):
                calls = await self.callback(data)
                self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in calls))
                self.assertEqual(original_count, self.store.count_employees())
        await self.callback(confirmation)
        count = self.store.count_employees()
        replay = await self.callback(confirmation)
        self.assertEqual(count, self.store.count_employees())
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in replay))

    async def test_cancelled_and_replaced_drafts_cannot_grant_access(self):
        for cancel in ("/cancel", main.ADD, "/staff"):
            with self.subTest(cancel=cancel):
                confirmation, _ = await self.preview()
                await self.message(cancel)
                await self.callback(confirmation)
                self.assertFalse(self.store.has_employee_access(NEW_WORKER))
        confirmation, _ = await self.preview()
        await self.callback("cancel")
        await self.callback(confirmation)
        self.assertFalse(self.store.has_employee_access(NEW_WORKER))

    async def test_reopening_add_flow_invalidates_earlier_confirmation(self):
        older, _ = await self.preview()
        newer, _ = await self.preview(NEW_WORKER + 1)
        await self.callback(older)
        self.assertFalse(self.store.has_employee_access(NEW_WORKER))
        await self.callback(newer)
        self.assertTrue(self.store.has_employee_access(NEW_WORKER + 1))

    async def test_duplicate_employee_does_not_replace_original_grant(self):
        await self.grant()
        count = self.store.count_employees()
        original = next(row for row in self.store.list_employees(limit=100) if row.user_id == NEW_WORKER)
        await self.message("/addstaff")
        duplicate = await self.message(str(NEW_WORKER))
        confirmations = [value for value in self.callbacks(duplicate) if value.startswith("staffconfirm:")]
        if confirmations:
            await self.callback(confirmations[0])
        self.assertEqual(count, self.store.count_employees())
        stored = next(row for row in self.store.list_employees(limit=100) if row.user_id == NEW_WORKER)
        self.assertEqual(original, stored)

    async def test_employee_list_paginates_and_shows_registered_profiles(self):
        ids = [NEW_WORKER + index for index in range(11)]
        for user_id in ids:
            self.store.add_employee(user_id, OWNER)
        self.store.remember_staff(ids[0], "Новый сотрудник", "new_worker")
        first = await self.message("/staff")
        self.assertIn("staff:list:8", self.callbacks(first))
        self.assertIn("Новый сотрудник", self.texts(first))
        second = await self.callback("staff:list:8")
        combined = self.texts(first) + self.texts(second)
        for user_id in ids:
            self.assertIn(str(user_id), combined)
        self.assertIn("staff:list:0", self.callbacks(second))
        for data in ("staff:list:²", "staff:list:-1", "staff:list:9223372036854775808"):
            malformed = await self.callback(data)
            self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in malformed))


if __name__ == "__main__":
    unittest.main()
