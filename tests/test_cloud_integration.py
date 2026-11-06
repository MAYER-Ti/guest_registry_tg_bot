"""Cloud reports and bot authorization use the same remote data without fallback."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import main
from activity import calculate_stats
from cloud_backups import cloud_backup_loop
from config import Config, ConfigError, load_config
from reporting import ReportError, report_snapshot
from sheets_store import SheetsStorageError, SheetsStore
from storage import Store
from storage_factory import open_store
from test_bot import RecordingSession
from test_sheets_store import ADMIN, LocalTransport, NOW, OWNER, SECRET, URL


class CloudReportTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        self.runtime.mkdir(parents=True, mode=0o777)
        self.local = Store(self.runtime / "source.sqlite3")
        self.transport = LocalTransport(self.local)
        self.remote = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=self.transport)
        self.remote.bootstrap_employee_access([ADMIN], ADMIN)

    def tearDown(self):
        shutil.rmtree(self.runtime)

    def seed(self, store, name="Гость"):
        return store.add_guest(name=name, phone="+79991234567", comment="Комментарий",
                               photo=b"photo-bytes", photo_file_id="telegram-photo", actor_id=ADMIN,
                               entry_status="closed", entry_reason="Причина")

    def test_report_and_stats_include_remote_visits_and_additional_database(self):
        guest = self.seed(self.remote)
        self.remote.start_visit(guest.id, guest.version, ADMIN, now=NOW - timedelta(hours=2))
        extra = Store(self.runtime / "another.sqlite3")
        self.seed(extra, "Другой гость с тем же ID")
        snapshot = report_snapshot(self.remote, (extra.path,), include_photos=True, now=NOW,
                                   photo_transform=lambda photo: b"thumbnail-" + photo)
        self.assertEqual(snapshot.total_count, 2)
        self.assertEqual(len(snapshot.blacklist), 2)
        self.assertEqual(snapshot.sources, ("Основная база", "База 2"))
        self.assertEqual({row.guest.id for row in snapshot.rows}, {1})
        self.assertTrue(all(row.guest.photo == b"thumbnail-photo-bytes" for row in snapshot.rows))
        stats = calculate_stats(snapshot, "day")
        self.assertEqual((stats.visit_count, stats.unique_guests, stats.total_seconds), (1, 1, 7200))
        self.assertFalse(hasattr(self.remote, "path"))

    def test_empty_cloud_snapshot_is_valid_but_unavailable_is_not_empty(self):
        self.assertEqual(report_snapshot(self.remote, now=NOW).total_count, 0)
        with patch.object(self.remote, "snapshot", side_effect=SheetsStorageError("private failure")):
            with self.assertRaises(ReportError) as caught:
                report_snapshot(self.remote)
        self.assertNotIn("private failure", str(caught.exception))

    def test_missing_extra_source_cancels_whole_report_without_creating_it(self):
        self.seed(self.remote)
        missing = self.runtime / "missing.sqlite3"
        with self.assertRaises(ReportError):
            report_snapshot(self.remote, (path for path in (missing,)))
        self.assertFalse(missing.exists())

    def test_factory_never_falls_back_to_sqlite_after_cloud_failure(self):
        config = Config("token", frozenset({ADMIN}), self.runtime / "fallback.sqlite3",
                        storage_backend="google_sheets", sheets_endpoint=URL, sheets_secret=SECRET,
                        google_owner_email=OWNER)
        with patch("storage_factory.SheetsStore", side_effect=SheetsStorageError("safe")), \
                patch("storage_factory.Store") as local:
            with self.assertRaises(SheetsStorageError):
                open_store(config)
            local.assert_not_called()
        self.assertFalse(config.db_path.exists())


class CloudConfigTests(unittest.TestCase):
    def test_cloud_credentials_are_required_and_never_echoed(self):
        base = {"BOT_TOKEN": "123456789:" + "A" * 35, "ALLOWED_USER_IDS": str(ADMIN),
                "STORAGE_BACKEND": "google_sheets", "SHEETS_ENDPOINT": URL, "SHEETS_SECRET": SECRET,
                "SHEETS_OWNER_EMAIL": OWNER}
        with patch.dict("os.environ", base, clear=True):
            config = load_config()
        self.assertEqual(config.storage_backend, "google_sheets")
        self.assertEqual(config.google_owner_email, OWNER)
        self.assertFalse(config.allow_new_database)
        self.assertNotIn(SECRET, repr(config))
        self.assertNotIn(URL, repr(config))
        for changes in ({"SHEETS_SECRET": "private-invalid"}, {"SHEETS_ENDPOINT": "https://evil.example/secret"},
                        {"SHEETS_ENDPOINT": URL + "?token=private"}, {"ALLOW_NEW_DATABASE": "yes"},
                        {"STORAGE_BACKEND": "unknown"}):
            with self.subTest(changes=changes), patch.dict("os.environ", {**base, **changes}, clear=True):
                with self.assertRaises(ConfigError) as caught:
                    load_config()
                for value in changes.values():
                    self.assertNotIn(value, str(caught.exception))


class CloudBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cloud_mutation_uses_one_early_ack_and_still_shows_success_card(self):
        runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        runtime.mkdir(parents=True, mode=0o777)
        store = Store(runtime / "source.sqlite3")
        transport = LocalTransport(store)
        remote = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=transport)

        class ExpiringAfterFirstSession(RecordingSession):
            answer_count = 0

            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, AnswerCallbackQuery):
                    self.answer_count += 1
                    if self.answer_count > 1:
                        raise TelegramBadRequest(method=method, message="query is too old")
                return await super().make_request(bot, method, timeout=timeout)

        session = ExpiringAfterFirstSession()
        bot = Bot("123456789:" + "A" * 35, session=session)
        dp = main.build_dispatcher(Config(bot.token, frozenset({ADMIN}), store.path), remote)
        guest = remote.add_guest(name="Облачный гость", phone="+79991234567", comment="Комментарий",
                                 photo=b"photo-bytes", photo_file_id="telegram-photo", actor_id=ADMIN)
        observed_actions = []

        def observed(url, body, timeout):
            self.assertEqual(1, session.answer_count, "Every cloud RPC must follow the neutral ACK")
            import json
            observed_actions.append(json.loads(json.loads(body)["payload"])["action"])
            return transport(url, body, timeout)

        remote._transport = observed
        query = CallbackQuery(id="slow-cloud-start", from_user=User(id=ADMIN, first_name="Admin", is_bot=False),
            chat_instance="test", data=f"start:{guest.id}:{guest.version}",
            message=Message(message_id=10, date=NOW, chat=Chat(id=ADMIN, type="private"),
                            from_user=User(id=bot.id, first_name="Bot", is_bot=True), text="Карточка"))
        try:
            await dp.feed_update(bot, Update(update_id=1, callback_query=query))
            self.assertEqual(1, session.answer_count)
            self.assertEqual("remember_staff", observed_actions[0])
            self.assertIn("start_visit", observed_actions)
            self.assertIsNotNone(store.get_visit_summary(guest.id).active)
            replies = [call.text for call in session.calls if isinstance(call, SendMessage)]
            self.assertTrue(any("Посещение начато" in text for text in replies))
            self.assertFalse(any("Не удалось подтвердить" in text for text in replies))
            self.assertTrue(any(isinstance(call, SendPhoto) and guest.name in call.caption for call in session.calls))
            self.assertTrue(all(call.protect_content for call in session.calls
                                if isinstance(call, SendMessage) and call.text == "Время пошло."))
            self.assertIsNone(main._ACKNOWLEDGED_CLOUD_QUERY.get())
        finally:
            await dp.storage.close()
            await dp.fsm.events_isolation.close()
            await bot.session.close()
            shutil.rmtree(runtime)

    async def test_expired_initial_cloud_ack_stops_before_any_access_or_mutation_rpc(self):
        runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        runtime.mkdir(parents=True, mode=0o777)
        store = Store(runtime / "source.sqlite3")
        remote = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=LocalTransport(store))

        class ExpiredSession(RecordingSession):
            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, AnswerCallbackQuery):
                    raise TelegramBadRequest(method=method, message="query is too old")
                return await super().make_request(bot, method, timeout=timeout)

        session = ExpiredSession()
        bot = Bot("123456789:" + "A" * 35, session=session)
        dp = main.build_dispatcher(Config(bot.token, frozenset({ADMIN}), store.path), remote)
        query = CallbackQuery(id="expired-cloud-start", from_user=User(id=999, first_name="Unknown", is_bot=False),
            chat_instance="test", data="start:1:1",
            message=Message(message_id=10, date=NOW, chat=Chat(id=999, type="private"),
                            from_user=User(id=bot.id, first_name="Bot", is_bot=True), text="Карточка"))
        try:
            with patch.object(remote, "_transport", side_effect=AssertionError("No cloud access after expiry")):
                await dp.feed_update(bot, Update(update_id=1, callback_query=query))
            replies = [call for call in session.calls if isinstance(call, SendMessage)]
            self.assertEqual(1, len(replies))
            self.assertEqual("Кнопка устарела. Открой меню или карточку заново.", replies[0].text)
            self.assertTrue(replies[0].protect_content)
            self.assertEqual(0, store.count_search("1"))
            self.assertEqual(0, store.count_audit())
            self.assertIsNone(main._ACKNOWLEDGED_CLOUD_QUERY.get())
        finally:
            await dp.storage.close()
            await dp.fsm.events_isolation.close()
            await bot.session.close()
            shutil.rmtree(runtime)

    async def test_early_cloud_ack_does_not_authorize_strangers_or_groups(self):
        runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        runtime.mkdir(parents=True, mode=0o777)
        store = Store(runtime / "source.sqlite3")
        remote = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=LocalTransport(store))
        session = RecordingSession()
        bot = Bot("123456789:" + "A" * 35, session=session)
        dp = main.build_dispatcher(Config(bot.token, frozenset({ADMIN}), store.path), remote)
        try:
            for number, (user_id, group) in enumerate(((999, False), (ADMIN, True)), 1):
                query = CallbackQuery(id=f"denied-cloud-{number}",
                    from_user=User(id=user_id, first_name="User", is_bot=False), chat_instance="test", data="view:1",
                    message=Message(message_id=10, date=NOW,
                        chat=Chat(id=-900 if group else user_id, type="group" if group else "private"),
                        from_user=User(id=bot.id, first_name="Bot", is_bot=True), text="Карточка"))
                with patch.object(remote, "get_guest", side_effect=AssertionError("No guest read")), \
                        patch.object(remote, "remember_staff", side_effect=AssertionError("No profile write")):
                    await dp.feed_update(bot, Update(update_id=number, callback_query=query))
                self.assertIsNone(main._ACKNOWLEDGED_CLOUD_QUERY.get())
            self.assertTrue(any(isinstance(call, SendMessage) and call.text == "Нет доступа."
                                for call in session.calls))
            self.assertTrue(any(isinstance(call, AnswerCallbackQuery) and call.text == "Нет доступа."
                                and call.show_alert for call in session.calls))
            self.assertFalse(any(isinstance(call, SendPhoto) for call in session.calls))
        finally:
            await dp.storage.close()
            await dp.fsm.events_isolation.close()
            await bot.session.close()
            shutil.rmtree(runtime)

    async def test_storage_outage_is_reported_without_data_or_a_fake_success(self):
        runtime = Path.cwd() / ".test-runtime" / uuid4().hex
        runtime.mkdir(parents=True, mode=0o777)
        store = Store(runtime / "source.sqlite3")
        remote = SheetsStore(URL, SECRET, expected_owner_email=OWNER, transport=LocalTransport(store))
        session = RecordingSession()
        bot = Bot("123456789:" + "A" * 35, session=session)
        dp = main.build_dispatcher(Config(bot.token, frozenset({ADMIN}), store.path), remote)
        incoming = Message(message_id=1, date=NOW, chat=Chat(id=ADMIN, type="private"),
                           from_user=User(id=ADMIN, first_name="Admin", is_bot=False), text="/start")
        try:
            with patch.object(remote, "remember_staff", side_effect=SheetsStorageError(SECRET)):
                await dp.feed_update(bot, Update(update_id=1, message=incoming))
            replies = [call.text for call in session.calls if isinstance(call, SendMessage)]
            self.assertEqual(len(replies), 1)
            self.assertIn("Не удалось", replies[0])
            self.assertNotIn(SECRET, replies[0])
            self.assertNotIn("Выбери действие", replies[0])
        finally:
            await dp.storage.close()
            await dp.fsm.events_isolation.close()
            await bot.session.close()
            shutil.rmtree(runtime)

    async def test_backup_failure_alerts_once_then_retries_and_cancellation_escapes(self):
        store = Mock()
        store.create_backup.side_effect = [RuntimeError(SECRET), RuntimeError(SECRET), {"complete": True}]
        bot = Mock()
        bot.send_message = AsyncMock()
        tick = 0

        async def sleep(delay):
            nonlocal tick
            tick += 1
            self.assertEqual(delay, 10 if tick < 3 else 100)
            if tick == 3:
                raise asyncio.CancelledError

        with patch("cloud_backups.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await cloud_backup_loop(bot, store, ADMIN, check_seconds=100, retry_seconds=10)
        self.assertEqual(store.create_backup.call_count, 3)
        bot.send_message.assert_awaited_once()
        sent = bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], ADMIN)
        self.assertTrue(sent["protect_content"])
        self.assertNotIn(SECRET, sent["text"])
