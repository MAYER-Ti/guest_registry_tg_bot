"""The reminder worker shares the bot lifetime and is always cancelled."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_workers_both_finish_cancellation_before_bot_closes(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                events = []
                started = {name: asyncio.Event() for name in ("reminders", "backups")}
                tasks = {}
                config = SimpleNamespace(token="test", db_path=Path("unused"),
                                         allowed_user_ids=frozenset({101}),
                                         storage_backend="google_sheets", admin_user_id=7011639945)
                store = object()
                access_policy = Mock()

                class FakeBot:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        events.append("bot_closed")

                    async def get_me(self):
                        return SimpleNamespace(username="Test", id=123)

                    async def delete_webhook(self, **kwargs):
                        events.append("webhook_deleted")

                bot = FakeBot()

                async def worker(name):
                    tasks[name] = asyncio.current_task()
                    events.append(name + "_started")
                    started[name].set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        # Yield during cleanup: the runtime must await both
                        # cancellations, not only issue Task.cancel().
                        await asyncio.sleep(0)
                        events.append(name + "_cancelled")

                async def reminder_worker(*args):
                    self.assertEqual(args, (bot, store, access_policy.allowed_ids))
                    await worker("reminders")

                async def backup_worker(*args):
                    self.assertEqual(args, (bot, store, config.admin_user_id))
                    await worker("backups")

                async def polling(*args, **kwargs):
                    self.assertEqual(args, (bot,))
                    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=2)
                    events.append("polling_finished")
                    if fail:
                        raise RuntimeError("Simulated polling failure")

                dispatcher = Mock()
                dispatcher.__getitem__ = Mock(return_value=access_policy)
                dispatcher.resolve_used_update_types.return_value = ["message", "callback_query"]
                dispatcher.start_polling = AsyncMock(side_effect=polling)
                with patch.object(main, "load_config", return_value=config), \
                        patch.object(main, "open_store", return_value=store) as open_store, \
                        patch.object(main, "build_dispatcher", return_value=dispatcher) as build_dispatcher, \
                        patch.object(main, "Bot", return_value=bot), \
                        patch.object(main, "reminder_loop", side_effect=reminder_worker), \
                        patch.object(main, "cloud_backup_loop", side_effect=backup_worker):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "Simulated polling failure"):
                            await main.main()
                    else:
                        await main.main()
                open_store.assert_called_once_with(config)
                build_dispatcher.assert_called_once_with(config, store)
                self.assertEqual(set(tasks), {"reminders", "backups"})
                self.assertTrue(all(task.done() and task.cancelled() for task in tasks.values()))
                self.assertEqual(events[0], "webhook_deleted")
                self.assertEqual(events[-1], "bot_closed")
                for name in started:
                    self.assertLess(events.index(name + "_started"), events.index("polling_finished"))
                    self.assertLess(events.index("polling_finished"), events.index(name + "_cancelled"))
                    self.assertLess(events.index(name + "_cancelled"), events.index("bot_closed"))

    async def test_worker_starts_and_stops_before_bot_closes_even_on_polling_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                events = []
                started = asyncio.Event()
                config = SimpleNamespace(token="test", db_path=Path("unused"), allowed_user_ids=frozenset({101}))
                store = object()

                class FakeBot:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        events.append("bot_closed")

                    async def get_me(self):
                        return SimpleNamespace(username="Test", id=123)

                    async def delete_webhook(self, **kwargs):
                        events.append("webhook_deleted")

                bot = FakeBot()
                access_policy = Mock()

                async def reminder_worker(*args):
                    self.assertEqual(args, (bot, store, access_policy.allowed_ids))
                    events.append("worker_started")
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        events.append("worker_cancelled")

                async def polling(*args, **kwargs):
                    await asyncio.wait_for(started.wait(), timeout=2)
                    if fail:
                        raise RuntimeError("Simulated polling failure")

                dispatcher = Mock()
                dispatcher.__getitem__ = Mock(return_value=access_policy)
                dispatcher.resolve_used_update_types.return_value = ["message", "callback_query"]
                dispatcher.start_polling = AsyncMock(side_effect=polling)
                with patch.object(main, "load_config", return_value=config), \
                        patch.object(main, "open_store", return_value=store), \
                        patch.object(main, "build_dispatcher", return_value=dispatcher), \
                        patch.object(main, "Bot", return_value=bot), \
                        patch.object(main, "reminder_loop", side_effect=reminder_worker):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            await main.main()
                    else:
                        await main.main()
                self.assertEqual(events, ["webhook_deleted", "worker_started", "worker_cancelled", "bot_closed"])


if __name__ == "__main__":
    unittest.main()
