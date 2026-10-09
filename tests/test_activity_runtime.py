"""The reminder worker shares the bot lifetime and is always cancelled."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
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

                async def reminder_worker(*args):
                    self.assertEqual(args, (bot, store, config.allowed_user_ids))
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
                dispatcher.resolve_used_update_types.return_value = ["message", "callback_query"]
                dispatcher.start_polling = AsyncMock(side_effect=polling)
                with patch.object(main, "load_config", return_value=config), \
                        patch.object(main, "Store", return_value=store), \
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
