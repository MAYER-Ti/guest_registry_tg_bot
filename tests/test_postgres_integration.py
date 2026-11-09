"""PostgreSQL application contracts without a live database or Telegram network."""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from openpyxl import load_workbook

import main
from activity import calculate_stats
from config import Config, ConfigError, load_config
from excel_export import build_guest_workbook, prepare_photo_thumbnail
from postgres_store import PostgresStorageError, PostgresStore
from reporting import ReportError, report_snapshot
from storage import Guest, Visit, VisitSummary
from storage_factory import open_store
from test_bot import RecordingSession


ADMIN = 7011639945
EMPLOYEE = 1234
NOW = datetime(2026, 11, 9, 12, 30, 0, 123456, tzinfo=timezone.utc)
TOKEN = "123456789:" + "A" * 35
DSN = "postgresql://private_bot:NEVER_PRINT_PASSWORD@example.invalid/postgres"
REGISTRY = "private-registry-0123456789"
PHOTO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a63sAAAAASUVORK5CYII="
)


def recovered_guest(**changes):
    values = dict(id=7, name="Восстановленный гость", phone="+79991234567",
                  phone_key="79991234567", comment="=Сохрани весь комментарий",
                  photo=b"", photo_file_id="", created_by=ADMIN, updated_by=EMPLOYEE,
                  created_at=(NOW - timedelta(days=1)).isoformat(), updated_at=NOW.isoformat(),
                  version=9, entry_status="closed", entry_reason="Причина запрета")
    values.update(changes)
    return Guest(**values)


class FakePostgres:
    """Store-shaped adapter; the real app chooses all PostgreSQL-specific paths."""
    backend = "postgres"

    def __init__(self, guests=(), visits=(), recovered_totals=None):
        self.guests = {guest.id: guest for guest in guests}
        self.visits = {visit.id: visit for visit in visits}
        self.recovered_totals = deepcopy(recovered_totals)
        self.employees = {EMPLOYEE}
        self.calls = []
        self.observe = None
        self.failure = None

    def record(self, method):
        self.calls.append(method)
        if self.observe:
            self.observe(method)
        if self.failure == method:
            raise PostgresStorageError(DSN)

    def bootstrap_employee_access(self, initial_ids, admin_id):
        self.record("bootstrap_employee_access")

    def has_employee_access(self, user_id):
        self.record("has_employee_access")
        return user_id in self.employees

    def remember_staff(self, *args):
        self.record("remember_staff")

    def get_guest(self, guest_id):
        self.record("get_guest")
        return self.guests.get(guest_id)

    def get_visit_summary(self, guest_id):
        self.record("get_visit_summary")
        active = next((visit for visit in self.visits.values()
                       if visit.guest_id == guest_id and visit.stopped_at is None), None)
        completed = [visit for visit in self.visits.values()
                     if visit.guest_id == guest_id and visit.stopped_at is not None]
        baseline = next((row for row in self.recovered_totals or [] if row["guest_id"] == guest_id), None)
        return VisitSummary(active, len(completed) + (baseline["completed_count"] if baseline else 0),
                            sum(visit.duration_seconds(NOW) for visit in completed)
                            + (baseline["completed_seconds"] if baseline else 0),
                            active.duration_seconds(NOW) if active else 0)

    def get_recovered_totals(self, guest_id):
        self.record("get_recovered_totals")
        return next((row for row in self.recovered_totals or [] if row["guest_id"] == guest_id), None)

    def start_visit(self, guest_id, expected_version, actor_id):
        self.record("start_visit")
        guest = self.guests[guest_id]
        if guest.version != expected_version:
            raise AssertionError("Wrong version supplied by the application")
        visit = Visit(id=11, guest_id=guest_id, started_at=NOW.isoformat(),
                      stopped_at=None, started_by=actor_id, stopped_by=None)
        self.visits[visit.id] = visit
        self.guests[guest_id] = replace(guest, version=guest.version + 1)
        return visit

    def snapshot(self, *, include_photos=False, now=None, photo_transform=None):
        self.record("snapshot")
        guests = []
        for guest in self.guests.values():
            photo = guest.photo if include_photos else b""
            if photo and photo_transform:
                photo = photo_transform(photo)
            guests.append({**asdict(guest), "photo": base64.b64encode(photo).decode("ascii")})
        result = dict(generated_at=(now or NOW).isoformat(), guests=guests,
                      visits=[asdict(visit) for visit in self.visits.values()])
        if self.recovered_totals is not None:
            result["recovered_totals"] = deepcopy(self.recovered_totals)
        return result


class PostgresConfigIntegrationTests(unittest.TestCase):
    def environment(self, **changes):
        return {"BOT_TOKEN": TOKEN, "ALLOWED_USER_IDS": str(EMPLOYEE),
                "STORAGE_BACKEND": "postgres", "DATABASE_URL": DSN,
                "REGISTRY_ID": REGISTRY, **changes}

    def test_cloud_configuration_keeps_tls_and_secrets_private(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            config = load_config()
        self.assertEqual(config.storage_backend, "postgres")
        self.assertEqual(config.db_sslmode, "require")
        self.assertEqual(config.registry_id, REGISTRY)
        self.assertFalse(config.allow_new_database)
        for secret in (TOKEN, DSN, "NEVER_PRINT_PASSWORD", REGISTRY):
            self.assertNotIn(secret, repr(config))

    def test_weak_tls_invalid_url_and_missing_identity_fail_without_credentials(self):
        for changes in (
            {"DB_SSLMODE": "disable"},
            {"DB_SSLMODE": "prefer"},
            {"DATABASE_URL": DSN + "?sslmode=disable", "DB_SSLMODE": "require"},
            {"DATABASE_URL": DSN + "?sslmode=require&sslmode=require"},
            {"DATABASE_URL": DSN + "#NEVER_PRINT_PASSWORD"},
            {"DATABASE_URL": "postgresql://private_bot:NEVER_PRINT_PASSWORD@host:bad/postgres"},
            {"REGISTRY_ID": ""},
            {"DB_SSLMODE": "verify-full"},
        ):
            with self.subTest(changes=changes), \
                    patch.dict(os.environ, self.environment(**changes), clear=True):
                with self.assertRaises(ConfigError) as caught:
                    load_config()
                self.assertNotIn("NEVER_PRINT_PASSWORD", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_verified_tls_reaches_factory_and_database_failure_never_creates_sqlite(self):
        with patch.dict(os.environ, self.environment(DB_SSLMODE="verify-full", DB_SSLROOTCERT="system"), clear=True):
            config = load_config()
        with patch("postgres_store.PostgresStore", side_effect=PostgresStorageError("safe failure")) as remote, \
                patch("storage_factory.Store") as local:
            with self.assertRaises(PostgresStorageError):
                open_store(config)
            local.assert_not_called()
        remote.assert_called_once_with(DSN, expected_registry_id=REGISTRY,
                                       expected_admin_id=ADMIN, sslmode="verify-full", sslrootcert="system")

    def test_new_postgres_cards_still_require_complete_photographs_before_connection(self):
        store = object.__new__(PostgresStore)
        with patch.object(store, "_connection", side_effect=AssertionError("Must validate before connecting")):
            for photo, file_id in ((b"", ""), (b"", "telegram-id"), (PHOTO, "")):
                with self.subTest(photo=bool(photo), file_id=file_id), self.assertRaises(ValueError):
                    store.add_guest(name="Новый гость", phone="79991234567", comment="",
                                    photo=photo, photo_file_id=file_id, actor_id=ADMIN)


class PostgresReportingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.missing = recovered_guest()
        self.with_photo = recovered_guest(id=8, name="Гость с фото", phone="+79991234568",
                                          phone_key="79991234568", photo=PHOTO,
                                          photo_file_id="telegram-photo", entry_status="open", entry_reason="")
        self.visit = Visit(id=11, guest_id=7, started_at=(NOW - timedelta(hours=2)).isoformat(),
                           stopped_at=None, started_by=EMPLOYEE, stopped_by=None)
        self.remote = FakePostgres((self.missing, self.with_photo), (self.visit,))

    def test_report_and_statistics_preserve_recovered_cards_without_photos(self):
        before = deepcopy(self.remote.guests)
        report = report_snapshot(self.remote, now=NOW)
        self.assertEqual(report.total_count, 2)
        self.assertEqual(report.sources, ("Основная база",))
        self.assertEqual([row.guest.id for row in report.blacklist], [7])
        self.assertEqual(report.rows[0].guest.photo, b"")
        self.assertEqual(report.rows[0].guest.photo_file_id, "")
        self.assertEqual(report.rows[1].guest.photo, b"")
        self.assertEqual(report.rows[1].guest.photo_file_id, "telegram-photo")
        stats = calculate_stats(report, "day")
        self.assertEqual((stats.visit_count, stats.unique_guests, stats.total_seconds), (1, 1, 7200))
        self.assertEqual(self.remote.guests, before)

    def test_excel_contains_missing_photo_label_whole_card_and_visits(self):
        report = report_snapshot(self.remote, now=NOW, include_photos=True,
                                 photo_transform=prepare_photo_thumbnail)
        workbook_bytes = build_guest_workbook(report)
        workbook = load_workbook(BytesIO(workbook_bytes), data_only=False)
        self.assertEqual(set(workbook.sheetnames), {"Гости", "Посещения"})
        guest_sheet = workbook["Гости"]
        values = [cell.value for sheet in workbook for row in sheet for cell in row]
        for value in (self.missing.name, self.missing.phone, self.missing.comment,
                      self.missing.entry_reason, "Фото отсутствует или повреждено"):
            self.assertIn(value, values)
        comment_cell = next(cell for row in guest_sheet for cell in row if cell.value == self.missing.comment)
        self.assertEqual(comment_cell.data_type, "s")
        self.assertEqual(sum(len(sheet._images) for sheet in workbook), 1)
        with ZipFile(BytesIO(workbook_bytes)) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.startswith("xl/media/")]), 1)
        self.assertIn(timedelta(hours=2), values)

    def test_cloud_failure_and_invalid_visit_cancel_entire_report_without_private_error(self):
        self.remote.failure = "snapshot"
        with self.assertRaises(ReportError) as caught:
            report_snapshot(self.remote, now=NOW)
        self.assertNotIn(DSN, str(caught.exception))
        self.remote.failure = None
        self.remote.visits[11] = replace(self.visit, guest_id=999)
        with self.assertRaises(ReportError):
            report_snapshot(self.remote, now=NOW)

    def recovered_history(self, *, count=3, seconds=7200):
        baseline = dict(guest_id=7, completed_count=count, completed_seconds=seconds,
                        observed_at=(NOW - timedelta(hours=2)).isoformat(), source_message_id="chat:100:42")
        # The observation had 4 completed visits / 10800 seconds. Its exact
        # known visit (1 / 3600) was subtracted before saving the 3 / 7200
        # residual. A later completed visit and current visit remain exact.
        old = Visit(id=21, guest_id=7, started_at=(NOW - timedelta(days=1, hours=2)).isoformat(),
                    stopped_at=(NOW - timedelta(days=1, hours=1)).isoformat(),
                    started_by=EMPLOYEE, stopped_by=EMPLOYEE)
        recent = Visit(id=22, guest_id=7, started_at=(NOW - timedelta(hours=1)).isoformat(),
                       stopped_at=(NOW - timedelta(minutes=30)).isoformat(),
                       started_by=EMPLOYEE, stopped_by=EMPLOYEE)
        active = Visit(id=23, guest_id=7, started_at=(NOW - timedelta(minutes=15)).isoformat(),
                       stopped_at=None, started_by=EMPLOYEE, stopped_by=None)
        return FakePostgres((self.missing,), (old, recent, active), [baseline])

    def test_recovered_lifetime_totals_are_added_once_and_period_stats_use_only_exact_visits(self):
        remote = self.recovered_history()
        original = deepcopy(remote.recovered_totals)
        report = report_snapshot(remote, now=NOW)
        row = report.rows[0]
        self.assertEqual(row.summary.completed_count, 5)
        self.assertEqual(row.summary.completed_seconds, 12600)
        self.assertEqual(row.summary.current_seconds, 900)
        self.assertEqual(row.summary.total_seconds, 13500)
        self.assertEqual({visit.id for visit in row.visits}, {21, 22, 23})
        stats = calculate_stats(report, "day")
        self.assertEqual((stats.visit_count, stats.unique_guests, stats.total_seconds), (2, 1, 2700))
        self.assertEqual(remote.recovered_totals, original)

    def test_excel_includes_recovered_lifetime_totals_without_inventing_visit_rows(self):
        remote = self.recovered_history()
        report = report_snapshot(remote, now=NOW, include_photos=True,
                                 photo_transform=prepare_photo_thumbnail)
        workbook = load_workbook(BytesIO(build_guest_workbook(report)), data_only=False)
        sheet = workbook["Гости"]
        values = dict(zip((cell.value for cell in sheet[5]), (cell.value for cell in sheet[6])))
        self.assertEqual(values["Завершённых посещений"], 5)
        self.assertEqual(values["Время завершённых посещений"], timedelta(seconds=12600))
        self.assertEqual(values["Всего времени"], timedelta(seconds=13500))
        visit_rows = list(workbook["Посещения"].iter_rows(min_row=6, values_only=True))
        self.assertEqual(len(visit_rows), 3)
        self.assertEqual({str(row[5]) for row in visit_rows}, {"21", "22", "23"})

    def test_explicit_zero_recovery_preserves_exact_totals_and_is_not_treated_as_missing_data(self):
        remote = self.recovered_history(count=0, seconds=0)
        report = report_snapshot(remote, now=NOW)
        self.assertEqual(report.rows[0].summary.completed_count, 2)
        self.assertEqual(report.rows[0].summary.completed_seconds, 5400)
        self.assertEqual(report.rows[0].summary.total_seconds, 6300)
        self.assertEqual(remote.recovered_totals[0]["completed_count"], 0)
        self.assertEqual(remote.recovered_totals[0]["completed_seconds"], 0)
        self.assertEqual(calculate_stats(report, "day").total_seconds, 2700)

    def test_invalid_recovered_totals_abort_report_instead_of_silently_omitting_history(self):
        valid = self.recovered_history().recovered_totals[0]
        for baselines in ({}, [valid, valid], [{**valid, "guest_id": 999}],
                          [{**valid, "completed_count": -1}], [{**valid, "completed_seconds": True}],
                          [{**valid, "source_message_id": ""}], [{**valid, "observed_at": "2026-11-09"}]):
            with self.subTest(baselines=baselines):
                remote = FakePostgres((self.missing,), recovered_totals=baselines)
                with self.assertRaises(ReportError) as caught:
                    report_snapshot(remote, now=NOW)
                self.assertNotIn("chat:100:42", str(caught.exception))


class ExpiringAfterFirstSession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery) and any(
                isinstance(call, AnswerCallbackQuery) for call in self.calls):
            raise TelegramBadRequest(method=method, message="query is too old")
        return await super().make_request(bot, method, timeout=timeout)


class PostgresBotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.remote = FakePostgres((recovered_guest(),))
        self.session = ExpiringAfterFirstSession()
        self.bot = Bot(TOKEN, session=self.session)
        self.dp = main.build_dispatcher(Config(TOKEN, frozenset({EMPLOYEE}), Path("unused.sqlite3"),
                                              storage_backend="postgres"), self.remote)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.dp.fsm.events_isolation.close()
        await self.bot.session.close()

    async def callback(self, data, *, user_id=EMPLOYEE, group=False):
        self.update_id += 1
        query = CallbackQuery(id=f"pg-{self.update_id}", from_user=User(id=user_id, first_name="Staff", is_bot=False),
            chat_instance="test", data=data,
            message=Message(message_id=10, date=NOW, chat=Chat(id=-900 if group else user_id,
                type="group" if group else "private"),
                from_user=User(id=self.bot.id, first_name="Bot", is_bot=True), text="Карточка"))
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, callback_query=query))

    async def message(self, text):
        self.update_id += 1
        incoming = Message(message_id=self.update_id, date=NOW, chat=Chat(id=EMPLOYEE, type="private"),
                           from_user=User(id=EMPLOYEE, first_name="Staff", is_bot=False), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=incoming))

    def texts(self):
        return "\n".join(call.text for call in self.session.calls if isinstance(call, SendMessage))

    async def test_callback_ack_precedes_access_and_mutation_then_missing_photo_card_has_buttons(self):
        observed = []
        def observe(method):
            acknowledgements = [call for call in self.session.calls if isinstance(call, AnswerCallbackQuery)]
            self.assertEqual(len(acknowledgements), 1, f"{method} happened before a neutral callback ACK")
            self.assertIsNone(acknowledgements[0].text)
            observed.append(method)
        self.remote.observe = observe
        await self.callback("start:7:9")
        self.assertEqual(observed[:3], ["has_employee_access", "remember_staff", "start_visit"])
        self.assertIn("get_visit_summary", observed)
        self.assertIn("Время пошло.", self.texts())
        self.assertIn("Фото пока не восстановлено", self.texts())
        self.assertFalse(any(isinstance(call, SendPhoto) for call in self.session.calls))
        buttons = [button.callback_data for call in self.session.calls
                   for row in getattr(getattr(call, "reply_markup", None), "inline_keyboard", [])
                   for button in row]
        self.assertIn("stop:7:11", buttons)
        self.assertTrue(any(value.startswith("edit:7:") for value in buttons))
        self.assertIsNone(main._ACKNOWLEDGED_CLOUD_QUERY.get())

    async def test_remote_mutation_failure_is_sanitized_and_never_claims_success(self):
        self.remote.failure = "start_visit"
        await self.callback("start:7:9")
        self.assertEqual(sum(isinstance(call, AnswerCallbackQuery) for call in self.session.calls), 1)
        self.assertIn("Не удалось подтвердить операцию", self.texts())
        self.assertNotIn("Время пошло", self.texts())
        self.assertNotIn("NEVER_PRINT_PASSWORD", self.texts())
        self.assertFalse(self.remote.visits)
        self.assertIsNone(main._ACKNOWLEDGED_CLOUD_QUERY.get())

    async def test_denied_private_callback_acknowledges_without_reading_card_or_profile(self):
        await self.callback("view:7", user_id=999)
        self.assertIn("Нет доступа.", self.texts())
        self.assertNotIn("get_guest", self.remote.calls)
        self.assertNotIn("remember_staff", self.remote.calls)
        self.assertFalse(any(isinstance(call, SendPhoto) for call in self.session.calls))

    async def test_add_flow_cannot_skip_required_photo_for_new_cards(self):
        await self.message(main.ADD)
        await self.message("Без фото")
        state = await self.dp.fsm.get_context(bot=self.bot, chat_id=EMPLOYEE, user_id=EMPLOYEE).get_state()
        self.assertEqual(state, main.AddGuest.photo.state)
        self.assertIn("Пришли фотографию как фото", self.texts())
        self.assertEqual(len(self.remote.guests), 1)
        self.assertFalse(any(isinstance(call, SendPhoto) for call in self.session.calls))

    async def test_card_explains_positive_recovered_totals_without_displaying_private_provenance(self):
        self.remote.recovered_totals = [dict(guest_id=7, completed_count=3, completed_seconds=7200,
            observed_at=NOW.isoformat(), source_message_id="private-chat:42")]
        await self.callback("view:7")
        self.assertIn("Часть итогов восстановлена без точных дат посещений.", self.texts())
        self.assertNotIn("private-chat:42", self.texts())
        self.assertFalse(any(isinstance(call, SendPhoto) for call in self.session.calls))

    async def test_card_does_not_describe_confirmed_zero_totals_as_undated_history(self):
        self.remote.recovered_totals = [dict(guest_id=7, completed_count=0, completed_seconds=0,
            observed_at=NOW.isoformat(), source_message_id="private-chat:42")]
        await self.callback("view:7")
        self.assertNotIn("Часть итогов восстановлена", self.texts())
        self.assertIn("Фото пока не восстановлено", self.texts())


if __name__ == "__main__":
    unittest.main()
