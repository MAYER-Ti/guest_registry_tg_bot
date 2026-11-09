"""Local backup, failure privacy, rate limits and worker-lifetime checks."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import postgres_backup as backups
from config import Config, ConfigError, load_config


ADMIN = 7011639945
TOKEN = "123456789:" + "A" * 35
SECRET = "postgresql://private:NEVER_PRINT_BACKUP_SECRET@example.invalid/postgres"
NOW = datetime(2026, 11, 9, 12, 13, 14, 123456, tzinfo=timezone.utc)


class FakeStore:
    backend = "postgres"

    def __init__(self):
        self.calls = []
        self.payload = dict(format="guest-registry-postgres-backup", version=1,
            state=dict(guests=[{"photo": "cGhvdG8="}], visits=[], staff=[{"actor_id": 1234}],
                       employee_access=[{"user_id": 1234}], audit_events=[{"id": 1}], visit_reminders=[]),
            recovered_totals=[dict(guest_id=7, completed_count=2, completed_seconds=3600,
                                  observed_at=NOW.isoformat(), source_message_id="message115")])

    def backup(self, destination):
        self.calls.append(Path(destination))
        Path(destination).write_text(json.dumps(self.payload), encoding="utf-8")
        return Path(destination)


class BackupFileTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("GUEST_BOT_TEST_TMPDIR", tempfile.gettempdir()))
        self.root = base / ("postgres-backup-test-" + uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def test_complete_copy_uses_one_backend_export_and_keeps_existing_copies(self):
        previous = self.root / "previous-complete.json"
        previous.write_text("retained", encoding="utf-8")
        store = FakeStore()
        result = backups.create_postgres_backup(store, self.root, now=NOW)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8")), store.payload)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(previous.read_text(encoding="utf-8"), "retained")
        self.assertTrue(result.name.startswith("guest-registry-20261109T121314123456Z-"))
        self.assertEqual(set(self.root.iterdir()), {previous, result})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_duplicate_final_name_never_overwrites_complete_copy(self):
        store = FakeStore()
        with patch.object(backups, "uuid4", return_value=SimpleNamespace(hex="fixed-name")):
            first = backups.create_postgres_backup(store, self.root, now=NOW)
            original = first.read_bytes()
            store.payload["state"]["guests"][0]["photo"] = "changed"
            with self.assertRaises(FileExistsError):
                backups.create_postgres_backup(store, self.root, now=NOW)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [first])

    def test_failed_or_empty_backend_copy_never_publishes_a_file(self):
        for operation in (Mock(side_effect=OSError(SECRET)), lambda destination: destination):
            store = SimpleNamespace(backend="postgres", backup=operation)
            with self.subTest(operation=type(operation).__name__), self.assertRaises((OSError, RuntimeError)):
                backups.create_postgres_backup(store, self.root, now=NOW)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_daily_marker_survives_worker_restart_and_keeps_prior_markers(self):
        self.assertTrue(backups._reserve_daily_alert(self.root, "2026-11-09"))
        self.assertFalse(backups._reserve_daily_alert(self.root, "2026-11-09"))
        self.assertTrue(backups._reserve_daily_alert(self.root, "2026-11-10"))
        self.assertEqual(len(list(self.root.iterdir())), 2)


class BackupLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_waits_six_hours_and_never_alerts(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        store = SimpleNamespace(backend="postgres")
        sleep = AsyncMock(side_effect=asyncio.CancelledError())
        with patch.object(backups, "create_postgres_backup", return_value=Path("copy.json")) as create, \
                patch.object(backups.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await backups.postgres_backup_loop(bot, store, ADMIN, Path("unused"))
        create.assert_called_once_with(store, Path("unused"))
        sleep.assert_awaited_once_with(21600)
        bot.send_message.assert_not_awaited()

    async def test_failure_logs_are_sanitized_and_alert_attempts_at_most_once_per_day(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError(SECRET)))
        clock = Mock()
        clock.now.side_effect = [NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1)]
        delays = []
        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError()
        with patch.object(backups, "create_postgres_backup", side_effect=OSError(SECRET)), \
                patch.object(backups, "_reserve_daily_alert", return_value=True) as reserve, \
                patch.object(backups, "datetime", clock), \
                patch.object(backups.asyncio, "sleep", side_effect=sleep), \
                self.assertLogs("postgres_backup", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await backups.postgres_backup_loop(bot, SimpleNamespace(backend="postgres"), ADMIN, Path("unused"))
        self.assertEqual(delays, [1800, 1800, 1800])
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(reserve.call_count, 2)
        self.assertNotIn("NEVER_PRINT", "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        for call in bot.send_message.await_args_list:
            self.assertEqual(call.kwargs["chat_id"], ADMIN)
            self.assertTrue(call.kwargs["protect_content"])
            self.assertNotIn("NEVER_PRINT", call.kwargs["text"])

    async def test_existing_marker_suppresses_restart_alert_and_marker_failure_stays_private(self):
        for reserve_result, expected_alerts in ((False, 0), (OSError(SECRET), 1)):
            bot = SimpleNamespace(send_message=AsyncMock())
            reserve = Mock(side_effect=reserve_result if isinstance(reserve_result, Exception) else None,
                           return_value=reserve_result)
            with self.subTest(result=type(reserve_result).__name__), \
                    patch.object(backups, "create_postgres_backup", side_effect=OSError(SECRET)), \
                    patch.object(backups, "_reserve_daily_alert", reserve), \
                    patch.object(backups.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())), \
                    self.assertLogs("postgres_backup", level="WARNING") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await backups.postgres_backup_loop(bot, SimpleNamespace(backend="postgres"), ADMIN, Path("unused"))
            self.assertEqual(bot.send_message.await_count, expected_alerts)
            self.assertNotIn("NEVER_PRINT", "\n".join(logs.output))

    async def test_cancellation_awaits_in_progress_backup_before_finishing(self):
        started, release = threading.Event(), threading.Event()
        def create(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test thread did not finish")
            return Path("copy.json")
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(backups, "create_postgres_backup", side_effect=create):
            worker = asyncio.create_task(backups.postgres_backup_loop(bot, SimpleNamespace(backend="postgres"), ADMIN, Path("unused")))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                worker.cancel()
                await asyncio.sleep(0)
                self.assertFalse(worker.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 2)
            finally:
                release.set()
                if not worker.done():
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
        bot.send_message.assert_not_awaited()


class BackupConfigTests(unittest.TestCase):
    def environment(self, **changes):
        return {"BOT_TOKEN": TOKEN, "ALLOWED_USER_IDS": str(ADMIN), "STORAGE_BACKEND": "postgres",
                "DATABASE_URL": SECRET, "REGISTRY_ID": "private-registry-0123456789", **changes}

    def test_compatible_default_and_explicit_persistent_backup_directory(self):
        self.assertEqual(Config(TOKEN, frozenset({ADMIN}), Path("unused")).postgres_backup_dir, Path("data/backups"))
        with patch.dict(os.environ, self.environment(), clear=True):
            self.assertEqual(load_config().postgres_backup_dir, Path("data/backups"))
        with patch.dict(os.environ, self.environment(POSTGRES_BACKUP_DIR=" /app/data/private backups "), clear=True):
            self.assertEqual(load_config().postgres_backup_dir, Path("/app/data/private backups"))

    def test_invalid_directory_fails_without_secrets(self):
        for value in ("", " ", ":memory:", "nul\0path"):
            with self.subTest(value=value), patch("config.os.environ", self.environment(POSTGRES_BACKUP_DIR=value)):
                with self.assertRaises(ConfigError) as caught:
                    load_config()
                self.assertNotIn("NEVER_PRINT", str(caught.exception))


class BackupRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_worker_starts_and_is_awaited_before_bot_closes(self):
        import main
        for fail in (False, True):
            events, tasks = [], {}
            started = {name: asyncio.Event() for name in ("reminders", "backups")}
            config = SimpleNamespace(token="test", storage_backend="postgres", admin_user_id=ADMIN,
                                     postgres_backup_dir=Path("persistent-backups"))
            store = object()
            access = Mock()
            class FakeBot:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    events.append("closed")
                async def get_me(self):
                    return SimpleNamespace(username="Test", id=123)
                async def delete_webhook(self, **kwargs):
                    pass
            bot = FakeBot()
            async def worker(name):
                tasks[name] = asyncio.current_task()
                started[name].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    events.append(name + " cancelled")
            async def reminders(*args):
                await worker("reminders")
            async def backup_worker(*args):
                self.assertEqual(args, (bot, store, ADMIN, config.postgres_backup_dir))
                await worker("backups")
            async def polling(*args, **kwargs):
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
                if fail:
                    raise RuntimeError("Polling failure")
            dispatcher = Mock()
            dispatcher.__getitem__ = Mock(return_value=access)
            dispatcher.resolve_used_update_types.return_value = []
            dispatcher.start_polling = AsyncMock(side_effect=polling)
            with self.subTest(fail=fail), patch.object(main, "load_config", return_value=config), \
                    patch.object(main, "open_store", return_value=store), \
                    patch.object(main, "build_dispatcher", return_value=dispatcher), \
                    patch.object(main, "Bot", return_value=bot), \
                    patch.object(main, "reminder_loop", side_effect=reminders), \
                    patch.object(main, "postgres_backup_loop", side_effect=backup_worker):
                if fail:
                    with self.assertRaises(RuntimeError):
                        await main.main()
                else:
                    await main.main()
            self.assertEqual(events[-1], "closed")
            self.assertEqual(set(events[:-1]), {"reminders cancelled", "backups cancelled"})
            self.assertTrue(all(task.done() and task.cancelled() for task in tasks.values()))


if __name__ == "__main__":
    unittest.main()
