"""Telegram update tests with an in-memory API transport and a real database."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

import main
from config import Config
from storage import Store


class RecordingSession(BaseSession):
    """Return Telegram-shaped replies without ever opening a network connection."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(
                message_id=10000 + len(self.calls), date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Guest notebook"),
                text=method.text if isinstance(method, SendMessage) else None,
                caption=method.caption if isinstance(method, SendPhoto) else None,
            )
        raise AssertionError(f"Unexpected Telegram method: {method.__class__.__name__}")

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Tests must not download files from Telegram")
        yield b""  # This abstract method has to be an async generator.


class BotWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 0700 directories exclude the Windows sandbox token on Python 3.13.
        # Explicitly inherit the workspace ACL instead of tempfile.mkdtemp().
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.store = Store(self.runtime / "guests.sqlite3")
        self.session = RecordingSession()
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", session=self.session)
        self.dp = main.build_dispatcher(
            Config(token=self.bot.token, allowed_user_ids=frozenset({101, 102, 103, 104}),
                   db_path=self.store.path), self.store,
        )
        self.update_id = 0
        self.photo_patch = patch.object(
            main, "read_photo", AsyncMock(return_value=(b"fake-jpeg-bytes", "saved-photo-id")),
        )
        self.photo_mock = self.photo_patch.start()

    async def asyncTearDown(self):
        self.photo_patch.stop()
        await self.dp.storage.close()
        await self.dp.fsm.events_isolation.close()
        await self.bot.session.close()
        shutil.rmtree(self.runtime)
        try:
            self.runtime.parent.rmdir()
        except OSError:
            pass

    def person(self, user_id=101):
        return User(id=user_id, is_bot=False, first_name=f"Staff {user_id}")

    async def message(self, text=None, *, user_id=101, group=False, photo=False):
        self.update_id += 1
        kwargs = {}
        if photo:
            kwargs["photo"] = [PhotoSize(file_id="incoming-photo", file_unique_id="unique",
                                         width=640, height=480, file_size=16)]
        incoming = Message(
            message_id=self.update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=-999 if group else user_id, type="group" if group else "private"),
            from_user=self.person(user_id), text=text, **kwargs,
        )
        before = len(self.session.calls)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=incoming))
        return self.session.calls[before:]

    async def callback(self, data, *, user_id=101, group=False):
        self.update_id += 1
        query = CallbackQuery(
            id=f"callback-{self.update_id}", from_user=self.person(user_id),
            chat_instance="test-chat", data=data,
            message=Message(
                message_id=300, date=datetime.now(timezone.utc),
                chat=Chat(id=-999 if group else user_id, type="group" if group else "private"),
                from_user=User(id=self.bot.id, is_bot=True, first_name="Bot"), text="Card",
            ),
        )
        before = len(self.session.calls)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=query))
        return self.session.calls[before:]

    @staticmethod
    def button(calls, prefix):
        for call in reversed(calls):
            markup = getattr(call, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", []):
                for button in row:
                    if button.callback_data and button.callback_data.startswith(prefix):
                        return button.callback_data
        raise AssertionError(f"No callback starting with {prefix!r}")

    def seed(self, *, name="Иван Петров", phone="+79991234567", comment="Постоянный гость"):
        return self.store.add_guest(
            name=name, phone=phone, comment=comment, photo=b"seed-photo",
            photo_file_id="seed-file-id", actor_id=101,
        )

    async def draft(self, *, user_id=101, name="Иван Петров", phone="8 (999) 123-45-67",
                    comment="Постоянный гость"):
        await self.message(main.ADD, user_id=user_id)
        await self.message(user_id=user_id, photo=True)
        await self.message(phone, user_id=user_id)
        await self.message(name, user_id=user_id)
        calls = await self.message(comment, user_id=user_id)
        return self.button(calls, "save:"), calls

    async def test_full_create_then_other_staff_searches_shared_card(self):
        save, preview = await self.draft()
        self.assertEqual(0, self.store.count_search("Иван"))
        photo = next(call for call in preview if isinstance(call, SendPhoto))
        self.assertIn("+79991234567", photo.caption)
        self.assertTrue(photo.protect_content)
        await self.callback(save)
        guest = self.store.find_phone("+79991234567")
        self.assertEqual("Иван Петров", guest.name)
        self.assertEqual(b"fake-jpeg-bytes", guest.photo)
        self.assertEqual(101, guest.created_by)
        await self.message(main.FIND, user_id=102)
        found = await self.message("4567", user_id=102)
        shown = next(call for call in found if isinstance(call, SendPhoto))
        self.assertIn("Иван Петров", shown.caption)
        self.assertIn("Постоянный гость", shown.caption)
        self.assertTrue(shown.protect_content)

    async def test_repeated_save_and_cancelled_draft_cannot_create_again(self):
        save, _ = await self.draft()
        await self.callback(save)
        repeated = await self.callback(save)
        self.assertEqual(1, self.store.count_search("Иван"))
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert
                            for call in repeated))
        new_save, _ = await self.draft(phone="+79991234568", name="Другой гость")
        await self.message("/cancel")
        await self.callback(new_save)
        self.assertEqual(0, self.store.count_search("Другой"))

    async def test_search_selection_and_name_edit_are_visible_to_all_staff(self):
        guest = self.seed()
        self.seed(name="Иван Сидоров", phone="+79991234568")
        await self.message(main.FIND, user_id=102)
        results = await self.message("иван", user_id=102)
        self.assertEqual(f"view:{guest.id}", self.button(results, f"view:{guest.id}"))
        calls = await self.callback(f"view:{guest.id}", user_id=102)
        edit = self.button(calls, "edit:")
        fields = await self.callback(edit, user_id=102)
        rename = self.button(fields, "field:name:")
        await self.callback(rename, user_id=102)
        await self.message("  Пётр   Петров  ", user_id=102)
        updated = self.store.get_guest(guest.id)
        self.assertEqual("Пётр Петров", updated.name)
        self.assertEqual(102, updated.updated_by)
        self.assertEqual(guest.version + 1, updated.version)
        await self.message(main.FIND, user_id=104)
        found = await self.message("петр", user_id=104)
        self.assertTrue(any(isinstance(call, SendPhoto) and "Пётр Петров" in call.caption
                            for call in found))

    async def test_delete_requires_current_confirmation_and_cancel_invalidates_it(self):
        guest = self.seed()
        asked = await self.callback(f"delete:{guest.id}:{guest.version}")
        self.assertIsNotNone(self.store.get_guest(guest.id))
        confirm = self.button(asked, "remove:")
        await self.callback("cancel")
        await self.callback(confirm)
        self.assertIsNotNone(self.store.get_guest(guest.id))
        asked_again = await self.callback(f"delete:{guest.id}:{guest.version}")
        confirm_again = self.button(asked_again, "remove:")
        self.assertNotEqual(confirm, confirm_again)
        await self.callback(confirm_again)
        self.assertIsNone(self.store.get_guest(guest.id))
        await self.callback(confirm_again)
        self.assertEqual(0, self.store.count_search("Иван"))

    async def test_strangers_and_groups_cannot_read_or_mutate_cards(self):
        guest = self.seed()
        calls = await self.message(main.ADD, user_id=999)
        self.assertTrue(any(isinstance(call, SendMessage) and "Доступ" in call.text for call in calls))
        await self.message(photo=True, user_id=999)  # Media has no text.
        for user_id, group in ((999, False), (101, True)):
            await self.message(main.FIND, user_id=user_id, group=group)
            await self.message("Иван", user_id=user_id, group=group)
            for data in (f"view:{guest.id}", f"edit:{guest.id}:1", f"field:name:{guest.id}:1",
                         f"delete:{guest.id}:1", f"remove:{guest.id}:1:forged", "save:forged"):
                denied = await self.callback(data, user_id=user_id, group=group)
                self.assertFalse(any(isinstance(call, SendPhoto) for call in denied))
                self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert
                                    for call in denied))
            await self.message("Новое имя", user_id=user_id, group=group)
        unchanged = self.store.get_guest(guest.id)
        self.assertEqual(guest, unchanged)
        self.photo_mock.assert_not_awaited()

    async def test_myid_is_available_to_unknown_private_user_only(self):
        calls = await self.message("/myid", user_id=999)
        replies = [call for call in calls if isinstance(call, SendMessage)]
        self.assertEqual(1, len(replies))
        self.assertIn("999", replies[0].text)
        self.assertTrue(replies[0].protect_content)
        self.assertEqual([], await self.message("/myid", user_id=999, group=True))

    async def test_long_emoji_comment_is_split_into_valid_protected_messages(self):
        comment = "😀" * 2500
        save, preview = await self.draft(comment=comment)
        photos = [call for call in preview if isinstance(call, SendPhoto)]
        self.assertEqual(1, len(photos))
        self.assertLessEqual(len(photos[0].caption.encode("utf-16-le")) // 2, 1024)
        comments = [call for call in preview if isinstance(call, SendMessage) and "😀" in call.text]
        self.assertGreaterEqual(len(comments), 2)
        for call in comments:
            self.assertLessEqual(len(call.text.encode("utf-16-le")) // 2, 4096)
            self.assertTrue(call.protect_content)
        self.assertEqual(comment, "".join(call.text for call in comments).replace("Комментарий:\n", ""))
        self.assertEqual(save, self.button([comments[-1]], "save:"))
        await self.callback(save)
        self.assertEqual(comment, self.store.find_phone("+79991234567").comment)

    async def test_stale_edit_and_delete_cannot_overwrite_another_employee(self):
        guest = self.seed()
        await self.callback(f"field:name:{guest.id}:1", user_id=101)
        asked = await self.callback(f"delete:{guest.id}:1", user_id=103)
        confirm = self.button(asked, "remove:")
        await self.callback(f"field:comment:{guest.id}:1", user_id=102)
        await self.message("Обновлено другим сотрудником", user_id=102)
        await self.message("Потерянное изменение", user_id=101)
        await self.callback(confirm, user_id=103)
        current = self.store.get_guest(guest.id)
        self.assertEqual(guest.name, current.name)
        self.assertEqual("Обновлено другим сотрудником", current.comment)
        self.assertEqual(2, current.version)

    async def test_search_pages_expire_when_employee_starts_a_new_flow(self):
        for index in range(10):
            self.seed(name=f"Гость {index:02d}", phone=f"+79991234{index:03d}")
        await self.message(main.FIND)
        first = await self.message("Гость")
        next_page = self.button(first, "page:")
        second = await self.callback(next_page)
        texts = [call.text for call in second if isinstance(call, SendMessage)]
        self.assertTrue(any("9–10" in text for text in texts))
        await self.message(main.ADD)
        expired = await self.callback(next_page)
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in expired))
        self.assertFalse(any(isinstance(call, SendMessage) and "Найдено:" in call.text for call in expired))


if __name__ == "__main__":
    unittest.main()
