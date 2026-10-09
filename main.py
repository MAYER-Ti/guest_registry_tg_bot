"""Private Telegram guest notebook shared by allowlisted staff."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from io import BytesIO

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

from config import Config, load_config
from storage import (
    DuplicatePhoneError, Guest, StaleGuestError, Store, VisitStateError,
    VisitSummary, normalize_phone,
)

ADD = "➕ Добавить гостя"
FIND = "🔎 Найти гостя"
CANCEL = "Отмена"
ENTRY_OPEN = "🟢 Вход открыт"
ENTRY_CLOSED = "🔴 Вход закрыт"
NO_REASON = "Без причины"
ENTRY_LABELS = {"open": ENTRY_OPEN, "closed": ENTRY_CLOSED}
PAGE_SIZE = 8
VISIT_PAGE_SIZE = 5
MOSCOW = timezone(timedelta(hours=3))
MAX_PHOTO = 10 * 1024 * 1024
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ADD), KeyboardButton(text=FIND)]],
    resize_keyboard=True,
)
CANCEL_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CANCEL)]], resize_keyboard=True,
)
COMMENT_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Без комментария")], [KeyboardButton(text=CANCEL)]],
    resize_keyboard=True,
)
ENTRY_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ENTRY_OPEN), KeyboardButton(text=ENTRY_CLOSED)],
              [KeyboardButton(text=CANCEL)]], resize_keyboard=True,
)
REASON_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=NO_REASON)], [KeyboardButton(text=CANCEL)]],
    resize_keyboard=True,
)


class AddGuest(StatesGroup):
    photo = State()
    phone = State()
    name = State()
    comment = State()
    entry_status = State()
    entry_reason = State()
    confirm = State()


class FindGuest(StatesGroup):
    query = State()


class EditGuest(StatesGroup):
    value = State()
    entry_reason = State()


class DeleteGuest(StatesGroup):
    confirm = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
        for row in rows
    ])


def card_actions(guest: Guest, summary: VisitSummary) -> InlineKeyboardMarkup:
    key = f"{guest.id}:{guest.version}"
    timer = ("⏹ Стоп", f"stop:{guest.id}:{summary.active.id}") if summary.active else ("▶️ Старт", f"start:{key}")
    return keyboard([
        [timer, ("🔄 Обновить время", f"view:{guest.id}")],
        [("🕒 История посещений", f"history:{guest.id}:0")],
        [("✏️ Изменить", f"edit:{key}"), ("🗑 Удалить", f"delete:{key}")],
    ])


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} ч. {minutes:02d} мин. {seconds:02d} сек."


def format_visit_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M:%S")


def visit_details(summary: VisitSummary) -> str:
    lines = ["🕒 Время посещений"]
    if summary.active:
        lines.extend([
            "🟢 Сейчас в гостях",
            f"Начало: {format_visit_time(summary.active.started_at)} МСК",
            f"Текущий визит: {format_duration(summary.current_seconds)}",
        ])
    else:
        lines.append("⚪ Сейчас не в гостях")
    suffix = " (с текущим визитом)" if summary.active else ""
    lines.extend([
        f"Всего: {format_duration(summary.total_seconds)}{suffix}",
        f"Завершённых визитов: {summary.completed_count}",
    ])
    return "\n".join(lines)


def clean_name(value: str) -> str:
    value = " ".join(value.split())
    if not value or len(value) > 100:
        raise ValueError("Имя должно содержать от 1 до 100 символов.")
    return value


def clean_comment(value: str) -> str:
    value = value.strip()
    if len(value) > 3000:
        raise ValueError("Комментарий слишком длинный. Максимум — 3000 символов.")
    return "" if value in ("-", "Без комментария") else value


def clean_entry_status(value: str) -> str:
    for status, label in ENTRY_LABELS.items():
        if value.strip() in (label, label[2:]):
            return status
    raise ValueError("Выбери «Вход открыт» или «Вход закрыт» кнопкой.")


def clean_entry_reason(value: str) -> str:
    value = value.strip()
    if len(value) > 3000:
        raise ValueError("Причина слишком длинная. Максимум — 3000 символов.")
    return "" if value in ("-", NO_REASON) else value


def text_chunks(value: str, max_units: int = 4000) -> list[str]:
    chunks, current, units = [], [], 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if current and units + width > max_units:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


class StaffOnly(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        private = isinstance(message, Message) and message.chat.type == "private"
        if private and user and isinstance(event, Message):
            command = ((event.text or "").split(maxsplit=1) or [""])[0].split("@")[0]
            if command == "/myid":
                await event.answer(f"Твой Telegram ID: {user.id}", protect_content=True)
                return
        if not private or not user or user.id not in self.allowed:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа.", show_alert=True)
            elif private:
                await event.answer("Доступ только для сотрудников. Твой ID: " + str(user.id))
            return
        return await handler(event, data)


async def read_photo(message: Message, bot: Bot) -> tuple[bytes, str]:
    if not message.photo:
        raise ValueError("Пришли фотографию как фото, а не как файл.")
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_PHOTO:
        raise ValueError("Фотография слишком большая. Максимум — 10 МБ.")
    destination = BytesIO()
    await bot.download(photo, destination=destination)
    raw = destination.getvalue()
    if not raw or len(raw) > MAX_PHOTO:
        raise ValueError("Не удалось принять фото. Пришли фотографию до 10 МБ.")
    return raw, photo.file_id


async def send_card(
    message: Message, *, name: str, phone: str, comment: str, photo: bytes,
    photo_file_id: str = "", title: str = "Карточка гостя",
    reply_markup: InlineKeyboardMarkup | None = None,
    details: str = "",
    entry_status: str = "open", entry_reason: str = "",
) -> None:
    header = (f"{title}\n\nИмя: {name}\nТелефон: {phone}\n"
              f"Статус: {ENTRY_LABELS[entry_status]}")
    summary = f"{header}\nПричина: {entry_reason or '—'}"
    if details:
        summary += "\n\n" + details
    full = f"{summary}\n\nКомментарий: {comment or '—'}"
    long_text = len(full.encode("utf-16-le")) // 2 > 1024
    chunks = []
    caption = full
    if long_text:
        caption = summary
        if len(summary.encode("utf-16-le")) // 2 > 1024:
            caption = header
            chunks.extend(text_chunks(f"Причина:\n{entry_reason or '—'}"))
            if details:
                chunks.extend(text_chunks(details))
        chunks.extend(text_chunks(f"Комментарий:\n{comment or '—'}"))
    # Every photo has a local backup; file_id is only a sending optimization.
    try:
        await message.answer_photo(
            photo=photo_file_id or BufferedInputFile(photo, filename="guest.jpg"),
            caption=caption, reply_markup=None if long_text else reply_markup,
            protect_content=True,
        )
    except TelegramBadRequest:
        if not photo_file_id:
            raise
        await message.answer_photo(
            photo=BufferedInputFile(photo, filename="guest.jpg"), caption=caption,
            reply_markup=None if long_text else reply_markup, protect_content=True,
        )
    if long_text:
        for index, chunk in enumerate(chunks):
            await message.answer(
                chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None,
                protect_content=True,
            )


async def show_guest(message: Message, guest: Guest, store: Store) -> None:
    summary = await asyncio.to_thread(store.get_visit_summary, guest.id)
    await send_card(
        message, name=guest.name, phone=guest.phone, comment=guest.comment,
        photo=guest.photo, photo_file_id=guest.photo_file_id,
        title=f"Карточка гостя №{guest.id}", reply_markup=card_actions(guest, summary),
        details=visit_details(summary),
        entry_status=guest.entry_status, entry_reason=guest.entry_reason,
    )


def build_dispatcher(config: Config, store: Store) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    router = Router()
    guard = StaffOnly(config.allowed_user_ids)
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Общая база гостей. Выбери действие.\n"
            "В карточке можно изменить фото, телефон, имя, комментарий, статус входа и причину.\n"
            "Гость пришёл — нажми «Старт», ушёл — «Стоп». Время всех визитов суммируется.\n"
            "Прервать действие: /cancel.", reply_markup=MENU,
        )

    @router.message(Command("cancel"))
    @router.message(F.text == CANCEL)
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=MENU)

    @router.message(F.text == ADD)
    async def add_start(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(AddGuest.photo)
        await message.answer("Пришли фотографию гостя.", reply_markup=CANCEL_MENU)

    @router.message(F.text == FIND)
    async def find_start(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(FindGuest.query)
        await message.answer(
            "Напиши имя или номер телефона. Можно часть имени или последние цифры номера.",
            reply_markup=CANCEL_MENU,
        )

    @router.message(AddGuest.photo)
    async def add_photo(message: Message, state: FSMContext, bot: Bot):
        try:
            photo, file_id = await read_photo(message, bot)
        except ValueError as error:
            await message.answer(str(error))
            return
        except (TelegramBadRequest, TelegramNetworkError):
            await message.answer("Не удалось скачать фото. Попробуй прислать его ещё раз.")
            return
        await state.update_data(photo=photo, photo_file_id=file_id)
        await state.set_state(AddGuest.phone)
        await message.answer("Введи номер телефона гостя или пришли его контакт.")

    @router.message(AddGuest.phone)
    async def add_phone(message: Message, state: FSMContext):
        value = message.contact.phone_number if message.contact else (message.text or "")
        try:
            key = normalize_phone(value)
        except ValueError as error:
            await message.answer(str(error))
            return
        existing = await asyncio.to_thread(store.find_phone, key)
        if existing:
            await message.answer(
                "Этот номер уже есть в базе. Введи другой номер или открой существующую карточку.",
                reply_markup=keyboard([[("Открыть карточку", f"view:{existing.id}")]]),
            )
            return
        await state.update_data(phone="+" + key)
        await state.set_state(AddGuest.name)
        await message.answer("Как зовут гостя? Можно добавить фамилию.")

    @router.message(AddGuest.name)
    async def add_name(message: Message, state: FSMContext):
        try:
            name = clean_name(message.text or "")
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(name=name)
        await state.set_state(AddGuest.comment)
        await message.answer("Добавь комментарий к гостю.", reply_markup=COMMENT_MENU)

    @router.message(AddGuest.comment)
    async def add_comment(message: Message, state: FSMContext):
        if message.text is None:
            await message.answer("Пришли комментарий текстом или нажми «Без комментария».")
            return
        try:
            comment = clean_comment(message.text)
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(comment=comment)
        await state.set_state(AddGuest.entry_status)
        await message.answer("Выбери статус входа для гостя.", reply_markup=ENTRY_MENU)

    @router.message(AddGuest.entry_status)
    async def add_entry_status(message: Message, state: FSMContext):
        try:
            status = clean_entry_status(message.text or "")
        except ValueError as error:
            await message.answer(str(error), reply_markup=ENTRY_MENU)
            return
        await state.update_data(entry_status=status)
        await state.set_state(AddGuest.entry_reason)
        await message.answer("Укажи причину статуса или нажми «Без причины».", reply_markup=REASON_MENU)

    @router.message(AddGuest.entry_reason)
    async def add_entry_reason(message: Message, state: FSMContext):
        if message.text is None:
            await message.answer("Пришли причину текстом или нажми «Без причины».")
            return
        try:
            reason = clean_entry_reason(message.text)
        except ValueError as error:
            await message.answer(str(error))
            return
        nonce = secrets.token_hex(4)
        await state.update_data(entry_reason=reason, nonce=nonce)
        await state.set_state(AddGuest.confirm)
        draft = await state.get_data()
        await message.answer("Проверь карточку перед сохранением.", reply_markup=CANCEL_MENU)
        await send_card(
            message, name=draft["name"], phone=draft["phone"], comment=draft["comment"],
            photo=draft["photo"], photo_file_id=draft["photo_file_id"], title="Новая карточка",
            entry_status=draft["entry_status"], entry_reason=reason,
            reply_markup=keyboard([[("✅ Сохранить", f"save:{nonce}"), ("Отмена", "cancel")]]),
        )

    @router.callback_query(F.data == "cancel")
    async def cancel_button(query: CallbackQuery, state: FSMContext):
        await query.answer()
        await state.clear()
        await query.message.answer("Действие отменено.", reply_markup=MENU)

    @router.callback_query(F.data.startswith("save:"))
    async def save(query: CallbackQuery, state: FSMContext):
        draft = await state.get_data()
        if await state.get_state() != AddGuest.confirm.state or query.data != f"save:{draft.get('nonce')}":
            await query.answer("Этот черновик уже закрыт. Начни добавление заново.", show_alert=True)
            return
        await query.answer()
        try:
            guest = await asyncio.to_thread(
                store.add_guest, name=draft["name"], phone=draft["phone"], comment=draft["comment"],
                photo=draft["photo"], photo_file_id=draft["photo_file_id"], actor_id=query.from_user.id,
                entry_status=draft["entry_status"], entry_reason=draft["entry_reason"],
            )
        except DuplicatePhoneError:
            await state.clear()
            await query.message.answer("Другой сотрудник уже добавил этот номер. Найди карточку через поиск.", reply_markup=MENU)
            return
        await state.clear()
        await query.message.answer("Гость сохранён в общей базе.", reply_markup=MENU)
        await show_guest(query.message, guest, store)

    @router.message(AddGuest.confirm)
    async def confirmation_hint(message: Message):
        await message.answer("Нажми «Сохранить» под карточкой или «Отмена».")

    async def search_page(message: Message, state: FSMContext, offset: int = 0):
        data = await state.get_data()
        query = data["search"]
        found = await asyncio.to_thread(store.search_guests, query, PAGE_SIZE, offset)
        total = await asyncio.to_thread(store.count_search, query)
        if not found:
            await message.answer("Гость не найден. Попробуй другое имя или номер.")
            return
        if total == 1 and offset == 0:
            guest = await asyncio.to_thread(store.get_guest, found[0].id)
            if guest:
                await state.clear()
                await message.answer("Гость найден.", reply_markup=MENU)
                await show_guest(message, guest, store)
            else:
                await message.answer("Карточка только что удалена. Попробуй новый поиск.")
            return
        rows = [[(f"{g.name[:45]} · {g.phone}", f"view:{g.id}")] for g in found]
        navigation = []
        nonce = data["search_nonce"]
        if offset:
            navigation.append(("← Назад", f"page:{nonce}:{max(0, offset - PAGE_SIZE)}"))
        if offset + PAGE_SIZE < total:
            navigation.append(("Далее →", f"page:{nonce}:{offset + PAGE_SIZE}"))
        if navigation:
            rows.append(navigation)
        await message.answer(
            f"Найдено: {total}. Выбери гостя ({offset + 1}–{offset + len(found)}).",
            reply_markup=keyboard(rows), protect_content=True,
        )

    @router.message(FindGuest.query)
    async def find(message: Message, state: FSMContext):
        value = message.contact.phone_number if message.contact else (message.text or "")
        value = value.strip()
        if not value or len(value) > 100:
            await message.answer("Введи имя или номер: от 1 до 100 символов.")
            return
        await state.update_data(search=value, search_nonce=secrets.token_hex(4))
        await search_page(message, state)

    @router.callback_query(F.data.startswith("page:"))
    async def page(query: CallbackQuery, state: FSMContext):
        parts = query.data.split(":")
        data = await state.get_data()
        if len(parts) != 3 or parts[1] != data.get("search_nonce") or not parts[2].isdigit():
            await query.answer("Этот поиск уже закрыт. Выполни новый поиск.", show_alert=True)
            return
        await query.answer()
        await search_page(query.message, state, int(parts[2]))

    @router.callback_query(F.data.startswith("view:"))
    async def view(query: CallbackQuery, state: FSMContext):
        try:
            guest_id = int(query.data.split(":")[1])
        except (ValueError, IndexError):
            await query.answer("Карточка недоступна.", show_alert=True)
            return
        guest = await asyncio.to_thread(store.get_guest, guest_id)
        await query.answer()
        await state.clear()
        if guest:
            await query.message.answer("Карточка гостя:", reply_markup=MENU)
            await show_guest(query.message, guest, store)
        else:
            await query.message.answer("Карточка уже удалена.", reply_markup=MENU)

    @router.callback_query(F.data.startswith("start:"))
    async def start_timer(query: CallbackQuery, state: FSMContext):
        parts = query.data.split(":")
        if len(parts) != 3 or not all(part.isdigit() and int(part) > 0 for part in parts[1:]):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        try:
            await asyncio.to_thread(store.start_visit, int(parts[1]), int(parts[2]), query.from_user.id)
        except (StaleGuestError, VisitStateError) as error:
            await query.answer(str(error) + " Открой карточку заново.", show_alert=True)
            return
        await query.answer("Время пошло.")
        await state.clear()
        guest = await asyncio.to_thread(store.get_guest, int(parts[1]))
        if guest:
            await query.message.answer("▶️ Посещение начато. Когда гость уйдёт, нажми «Стоп».", reply_markup=MENU)
            await show_guest(query.message, guest, store)
        else:
            await query.message.answer("Карточка уже удалена.", reply_markup=MENU)

    @router.callback_query(F.data.startswith("stop:"))
    async def stop_timer(query: CallbackQuery, state: FSMContext):
        parts = query.data.split(":")
        if len(parts) != 3 or not all(part.isdigit() and int(part) > 0 for part in parts[1:]):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        try:
            visit = await asyncio.to_thread(store.stop_visit, int(parts[1]), int(parts[2]), query.from_user.id)
        except (StaleGuestError, VisitStateError, ValueError) as error:
            await query.answer(str(error) + " Обнови карточку.", show_alert=True)
            return
        await query.answer("Посещение сохранено.")
        await state.clear()
        guest = await asyncio.to_thread(store.get_guest, int(parts[1]))
        if guest:
            await query.message.answer(
                "⏹ Посещение завершено: " + format_duration(visit.duration_seconds()) + ".\nВремя добавлено к общему.",
                reply_markup=MENU,
            )
            await show_guest(query.message, guest, store)
        else:
            await query.message.answer("Карточка уже удалена.", reply_markup=MENU)

    @router.callback_query(F.data.startswith("history:"))
    async def visit_history(query: CallbackQuery, state: FSMContext):
        parts = query.data.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) <= 0 or not parts[2].isdigit():
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        guest_id, offset = int(parts[1]), int(parts[2])
        guest = await asyncio.to_thread(store.get_guest, guest_id)
        if not guest:
            await query.answer("Карточка уже удалена.", show_alert=True)
            return
        visits = await asyncio.to_thread(store.list_visits, guest_id, VISIT_PAGE_SIZE + 1, offset)
        await query.answer()
        await state.clear()
        lines = [f"История посещений: {guest.name}", "Время указано по Москве (МСК)."]
        now = datetime.now(timezone.utc)
        for visit in visits[:VISIT_PAGE_SIZE]:
            end = format_visit_time(visit.stopped_at) if visit.stopped_at else "идёт сейчас"
            lines.append(
                f"\n{format_visit_time(visit.started_at)} → {end}\n"
                f"Длительность: {format_duration(visit.duration_seconds(now))}"
            )
        if not visits:
            lines.append("\nПосещений пока нет." if offset == 0 else "\nНа этой странице посещений нет.")
        rows, navigation = [], []
        if offset:
            navigation.append(("← Назад", f"history:{guest_id}:{max(0, offset - VISIT_PAGE_SIZE)}"))
        if len(visits) > VISIT_PAGE_SIZE:
            navigation.append(("Далее →", f"history:{guest_id}:{offset + VISIT_PAGE_SIZE}"))
        if navigation:
            rows.append(navigation)
        rows.append([("К карточке", f"view:{guest_id}")])
        await query.message.answer("\n".join(lines), reply_markup=keyboard(rows), protect_content=True)

    async def current_guest(query: CallbackQuery, parts: list[str]) -> Guest | None:
        try:
            guest_id, version = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return None
        guest = await asyncio.to_thread(store.get_guest, guest_id)
        if not guest or guest.version != version:
            await query.answer("Карточка изменена или удалена. Открой её заново через поиск.", show_alert=True)
            return None
        return guest

    @router.callback_query(F.data.startswith("edit:"))
    async def edit(query: CallbackQuery, state: FSMContext):
        guest = await current_guest(query, query.data.split(":"))
        if guest is None:
            return
        await query.answer()
        await state.clear()
        key = f"{guest.id}:{guest.version}"
        await query.message.answer("Что изменить?", reply_markup=keyboard([
            [("Фото", f"field:photo:{key}"), ("Телефон", f"field:phone:{key}")],
            [("Имя", f"field:name:{key}"), ("Комментарий", f"field:comment:{key}")],
            [("Статус входа", f"field:entry_status:{key}"), ("Причина", f"field:entry_reason:{key}")],
            [("Отмена", "cancel")],
        ]))

    @router.callback_query(F.data.startswith("field:"))
    async def field(query: CallbackQuery, state: FSMContext):
        parts = query.data.split(":")
        if len(parts) != 4 or parts[1] not in {"photo", "phone", "name", "comment", "entry_status", "entry_reason"}:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        guest = await current_guest(query, parts)
        if guest is None:
            return
        await query.answer()
        await state.clear()
        await state.set_state(EditGuest.value)
        await state.update_data(guest_id=guest.id, version=guest.version, field=parts[1])
        prompts = {
            "photo": "Пришли новую фотографию.", "phone": "Введи новый телефон или пришли контакт.",
            "name": "Введи новое имя.", "comment": "Введи новый комментарий. Чтобы очистить, отправь «-».",
            "entry_status": "Выбери новый статус входа. Затем укажи причину — оба поля сохранятся вместе.",
            "entry_reason": "Введи новую причину. Чтобы очистить, нажми «Без причины».",
        }
        markup = ENTRY_MENU if parts[1] == "entry_status" else REASON_MENU if parts[1] == "entry_reason" else CANCEL_MENU
        await query.message.answer(prompts[parts[1]], reply_markup=markup)

    async def persist_edit(message: Message, state: FSMContext, fields: dict):
        data = await state.get_data()
        try:
            guest = await asyncio.to_thread(
                store.update_guest, data["guest_id"], data["version"], message.from_user.id, **fields,
            )
        except DuplicatePhoneError:
            await message.answer("Этот телефон уже занят другой карточкой. Введи другой номер.")
            return
        except StaleGuestError:
            await state.clear()
            await message.answer("Другой сотрудник изменил или удалил карточку. Найди её заново.", reply_markup=MENU)
            return
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await message.answer("Карточка обновлена.", reply_markup=MENU)
        await show_guest(message, guest, store)

    @router.message(EditGuest.value)
    async def edit_value(message: Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        field_name = data["field"]
        try:
            if field_name == "photo":
                photo, file_id = await read_photo(message, bot)
                fields = {"photo": photo, "photo_file_id": file_id}
            elif field_name == "phone":
                value = message.contact.phone_number if message.contact else (message.text or "")
                fields = {"phone": "+" + normalize_phone(value)}
            elif field_name == "name":
                fields = {"name": clean_name(message.text or "")}
            elif field_name == "entry_status":
                status = clean_entry_status(message.text or "")
                await state.update_data(entry_status=status)
                await state.set_state(EditGuest.entry_reason)
                await message.answer("Укажи причину нового статуса или нажми «Без причины».", reply_markup=REASON_MENU)
                return
            elif field_name == "entry_reason":
                if message.text is None:
                    raise ValueError("Пришли причину текстом или нажми «Без причины».")
                fields = {"entry_reason": clean_entry_reason(message.text)}
            else:
                if message.text is None:
                    raise ValueError("Пришли комментарий текстом.")
                fields = {"comment": clean_comment(message.text)}
        except ValueError as error:
            await message.answer(str(error))
            return
        except (TelegramBadRequest, TelegramNetworkError):
            await message.answer("Не удалось принять фото. Попробуй ещё раз.")
            return
        await persist_edit(message, state, fields)

    @router.message(EditGuest.entry_reason)
    async def edit_status_reason(message: Message, state: FSMContext):
        if message.text is None:
            await message.answer("Пришли причину текстом или нажми «Без причины».")
            return
        try:
            reason = clean_entry_reason(message.text)
        except ValueError as error:
            await message.answer(str(error))
            return
        data = await state.get_data()
        await persist_edit(message, state, {"entry_status": data["entry_status"], "entry_reason": reason})

    @router.callback_query(F.data.startswith("delete:"))
    async def ask_delete(query: CallbackQuery, state: FSMContext):
        guest = await current_guest(query, query.data.split(":"))
        if guest is None:
            return
        await query.answer()
        await state.clear()
        nonce = secrets.token_hex(4)
        await state.set_state(DeleteGuest.confirm)
        await state.update_data(delete_id=guest.id, delete_version=guest.version, delete_nonce=nonce)
        await query.message.answer(
            f"Удалить карточку «{guest.name}» ({guest.phone}) из общей базы?\n"
            "История посещений и текущий таймер тоже будут удалены.",
            reply_markup=keyboard([[("Да, удалить", f"remove:{guest.id}:{guest.version}:{nonce}"), ("Отмена", "cancel")]]),
            protect_content=True,
        )

    @router.callback_query(F.data.startswith("remove:"))
    async def remove(query: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        expected = f"remove:{data.get('delete_id')}:{data.get('delete_version')}:{data.get('delete_nonce')}"
        if await state.get_state() != DeleteGuest.confirm.state or query.data != expected:
            await query.answer("Подтверждение уже закрыто. Открой карточку заново.", show_alert=True)
            return
        guest = await current_guest(query, query.data.split(":")[1:3])
        if guest is None:
            return
        await query.answer()
        try:
            await asyncio.to_thread(store.delete_guest, guest.id, guest.version)
        except StaleGuestError:
            await query.message.answer("Карточка изменилась. Найди её заново.", reply_markup=MENU)
            return
        await state.clear()
        await query.message.answer("Карточка удалена из общей базы.", reply_markup=MENU)

    @router.callback_query()
    async def unknown_button(query: CallbackQuery):
        await query.answer("Эта кнопка устарела. Открой меню командой /start.", show_alert=True)

    @router.message()
    async def unknown_message(message: Message):
        await message.answer("Выбери «Добавить гостя» или «Найти гостя».", reply_markup=MENU)

    dispatcher.include_router(router)
    return dispatcher


async def main() -> None:
    config = load_config()
    store = Store(config.db_path)
    dispatcher = build_dispatcher(config, store)
    async with Bot(config.token) as bot:
        identity = await bot.get_me()
        logging.getLogger(__name__).info("Telegram connected: @%s (id=%s)", identity.username, identity.id)
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
