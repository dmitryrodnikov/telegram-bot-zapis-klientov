from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Service, Settings
from bot.db import Database
from bot.format import compact, esc, format_day, format_range, parse_compact, person_name
from bot.notify import admin_booking_text, notify_admins
from bot.slots import open_days

log = logging.getLogger("booking")


def make_router(settings: Settings, db: Database, bot) -> Router:
    router = Router()

    def services_markup() -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardBuilder()
        for service in settings.services:
            keyboard.button(text=service.title, callback_data=f"svc:{service.id}")
        keyboard.button(text="Мои записи", callback_data="my")
        if settings.webapp_url.startswith("https://"):
            keyboard.button(text="Мини-приложение", web_app=WebAppInfo(url=settings.webapp_url))
        keyboard.adjust(1)
        return keyboard.as_markup()

    def start_text(user_id: int) -> str:
        lines = [
            "<b>Запись на услуги</b>",
            "",
            "Выберите услугу, затем день и свободное время. На одно время записывается один человек.",
            "",
        ]
        for service in settings.services:
            lines.append(f"• {esc(service.title)} — {service.duration_min} мин, {esc(service.price)}")
        if not settings.admin_ids or user_id in settings.admin_ids:
            lines.append("")
            lines.append(f"Ваш Telegram id: <code>{user_id}</code>")
            if not settings.admin_ids:
                lines.append("Добавьте его в ADMIN_IDS в файле .env и перезапустите бота.")
        return "\n".join(lines)

    def service_caption(service: Service) -> str:
        description = f"\n{esc(service.description)}" if service.description else ""
        return (
            f"<b>{esc(service.title)}</b>{description}\n"
            f"{service.duration_min} мин · {esc(service.price)}"
        )

    async def answer(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
        try:
            await callback.answer(text, show_alert=alert)
        except TelegramBadRequest:
            return

    async def edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
        message = callback.message
        if message is None or not hasattr(message, "edit_text"):
            return
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            await message.answer(text, reply_markup=markup)

    def my_view(user_id: int, prefix: str = "") -> tuple[str, InlineKeyboardMarkup]:
        now = datetime.now(settings.timezone)
        upcoming = [
            booking
            for booking in db.bookings_for(user_id)
            if booking.starts_at + timedelta(minutes=booking.duration_min) > now
        ]
        keyboard = InlineKeyboardBuilder()
        if not upcoming:
            text = "<b>Мои записи</b>\n\nАктивных записей нет."
            if prefix:
                text = f"{prefix}\n\n{text}"
        else:
            lines = ["<b>Мои записи</b>", ""]
            if prefix:
                lines = [prefix, "", *lines]
            for booking in upcoming[:20]:
                service = settings.service(booking.service_id)
                title = service.title if service else booking.service_id
                lines.append(f"• {esc(title)} — {format_range(booking.starts_at, booking.duration_min)}")
                keyboard.button(
                    text=f"Отменить {booking.starts_at:%d.%m %H:%M}",
                    callback_data=f"cancel:{booking.id}",
                )
            text = "\n".join(lines).strip()
        keyboard.button(text="К услугам", callback_data="menu")
        keyboard.adjust(1)
        return text, keyboard.as_markup()

    def today_text() -> str:
        today = datetime.now(settings.timezone).date()
        bookings = [
            booking
            for booking in db.active()
            if booking.starts_at.astimezone(settings.timezone).date() == today
        ]
        if not bookings:
            return "<b>Записи на сегодня</b>\n\nПока пусто."
        lines = ["<b>Записи на сегодня</b>", ""]
        for booking in bookings[:30]:
            service = settings.service(booking.service_id)
            title = service.title if service else booking.service_id
            who = esc(booking.user_name)
            if booking.username:
                who += f" (@{esc(booking.username)})"
            lines.append(
                f"• {booking.starts_at:%H:%M} — {esc(title)}, {who}"
            )
        if len(bookings) > 30:
            lines.append(f"\nИ ещё {len(bookings) - 30}.")
        return "\n".join(lines)

    async def show_service_days(callback: CallbackQuery, service: Service) -> None:
        now = datetime.now(settings.timezone)
        days = open_days(settings, service, db.busy_intervals(), now)
        keyboard = InlineKeyboardBuilder()
        for day, slots in days[:14]:
            keyboard.button(
                text=f"{format_day(day)} · {len(slots)}",
                callback_data=f"day:{service.id}:{day.strftime('%Y%m%d')}",
            )
        keyboard.button(text="Назад", callback_data="menu")
        keyboard.adjust(1)
        if days:
            text = f"{service_caption(service)}\n\nВыберите день. Число справа — сколько окон свободно."
        else:
            text = (
                f"{service_caption(service)}\n\n"
                "В ближайшие дни свободного времени нет. Проверьте часы работы, перерыв и выходные в config.json."
            )
        await edit(callback, text, keyboard.as_markup())

    async def show_times(callback: CallbackQuery, service: Service, day) -> None:
        now = datetime.now(settings.timezone)
        slots = []
        for open_day, day_slots in open_days(settings, service, db.busy_intervals(), now):
            if open_day == day:
                slots = day_slots
                break
        keyboard = InlineKeyboardBuilder()
        for slot in slots[:60]:
            keyboard.button(text=slot.strftime("%H:%M"), callback_data=f"time:{service.id}:{compact(slot)}")
        keyboard.adjust(3)
        keyboard.row(InlineKeyboardButton(text="Назад", callback_data=f"svc:{service.id}"))
        if slots:
            text = f"{service_caption(service)}\n\n{format_day(day)}. Выберите время начала."
        else:
            text = f"{service_caption(service)}\n\nНа {format_day(day)} свободного времени уже нет."
        await edit(callback, text, keyboard.as_markup())

    def confirm_view(service: Service, start: datetime) -> tuple[str, InlineKeyboardMarkup]:
        text = (
            "<b>Проверьте запись</b>\n\n"
            f"{esc(service.title)}\n"
            f"{format_range(start, service.duration_min)}\n"
            f"{esc(service.price)}\n\n"
            "После подтверждения это время нельзя будет занять второму человеку."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подтвердить", callback_data=f"ok:{service.id}:{compact(start)}")],
                [InlineKeyboardButton(text="Другое время", callback_data=f"day:{service.id}:{start.strftime('%Y%m%d')}")],
            ]
        )
        return text, markup

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        log.info("start id=%s username=%s", user.id, user.username)
        await message.answer(start_text(user.id), reply_markup=services_markup())

    @router.message(Command("my"))
    async def on_my(message: Message) -> None:
        if message.from_user is None:
            return
        text, markup = my_view(message.from_user.id)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        if message.from_user is None:
            return
        if message.from_user.id not in settings.admin_ids:
            await message.answer("Список на сегодня видит администратор из ADMIN_IDS.")
            return
        await message.answer(today_text())

    @router.callback_query(F.data == "menu")
    async def on_menu(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            return
        await answer(callback)
        await edit(callback, start_text(callback.from_user.id), services_markup())

    @router.callback_query(F.data == "my")
    async def on_my_button(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            return
        await answer(callback)
        text, markup = my_view(callback.from_user.id)
        await edit(callback, text, markup)

    @router.callback_query(F.data.startswith("svc:"))
    async def on_service(callback: CallbackQuery) -> None:
        service = settings.service(callback.data.split(":", 1)[1])
        if service is None:
            await answer(callback, "Такой услуги уже нет", alert=True)
            return
        await answer(callback)
        await show_service_days(callback, service)

    @router.callback_query(F.data.startswith("day:"))
    async def on_day(callback: CallbackQuery) -> None:
        try:
            _, service_id, ymd = callback.data.split(":")
            day = datetime.strptime(ymd, "%Y%m%d").date()
        except ValueError:
            await answer(callback, "Кнопка устарела. Нажмите /start", alert=True)
            return
        service = settings.service(service_id)
        if service is None:
            await answer(callback, "Такой услуги уже нет", alert=True)
            return
        await answer(callback)
        await show_times(callback, service, day)

    @router.callback_query(F.data.startswith("time:"))
    async def on_time(callback: CallbackQuery) -> None:
        try:
            _, service_id, raw_time = callback.data.split(":")
            start = parse_compact(raw_time, settings.timezone)
        except ValueError:
            await answer(callback, "Кнопка устарела. Нажмите /start", alert=True)
            return
        service = settings.service(service_id)
        if service is None:
            await answer(callback, "Такой услуги уже нет", alert=True)
            return
        await answer(callback)
        text, markup = confirm_view(service, start)
        await edit(callback, text, markup)

    @router.callback_query(F.data.startswith("ok:"))
    async def on_confirm(callback: CallbackQuery) -> None:
        user = callback.from_user
        if user is None:
            return
        try:
            _, service_id, raw_time = callback.data.split(":")
            start = parse_compact(raw_time, settings.timezone)
        except ValueError:
            await answer(callback, "Кнопка устарела. Нажмите /start", alert=True)
            return
        service = settings.service(service_id)
        if service is None:
            await answer(callback, "Такой услуги уже нет", alert=True)
            return
        try:
            booking, code = db.create_booking(
                settings,
                service,
                start,
                user.id,
                person_name(user.first_name, user.last_name),
                user.username,
            )
        except Exception:
            log.exception("Не удалось сохранить запись")
            await answer(callback, "Не получилось сохранить запись. Попробуйте ещё раз.", alert=True)
            return
        if booking is None:
            await answer(
                callback,
                "Это время уже заняли." if code == "taken" else "Это время больше недоступно.",
                alert=True,
            )
            await show_times(callback, service, start.date())
            return
        await answer(callback, "Вы записаны")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отменить запись", callback_data=f"cancel:{booking.id}")],
                [InlineKeyboardButton(text="К услугам", callback_data="menu")],
            ]
        )
        await edit(
            callback,
            (
                "<b>Вы записаны</b>\n\n"
                f"{esc(service.title)}\n"
                f"{format_range(booking.starts_at, booking.duration_min)}\n"
                f"{esc(service.price)}\n\n"
                f"Напоминание придёт примерно за {settings.remind_before_min} мин, пока бот запущен."
            ),
            markup,
        )
        await notify_admins(bot, settings, admin_booking_text(service.title, booking))

    @router.callback_query(F.data.startswith("cancel:"))
    async def on_cancel(callback: CallbackQuery) -> None:
        user = callback.from_user
        if user is None:
            return
        try:
            booking_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await answer(callback, "Кнопка устарела", alert=True)
            return
        cancelled = db.cancel(booking_id, user.id)
        if cancelled is None:
            await answer(callback, "Запись уже отменена или принадлежит другому человеку", alert=True)
            return
        await answer(callback, "Запись отменена")
        text, markup = my_view(user.id, prefix="Запись отменена.")
        await edit(callback, text, markup)
        service = settings.service(cancelled.service_id)
        title = service.title if service else cancelled.service_id
        await notify_admins(bot, settings, admin_booking_text(title, cancelled, cancelled=True))

    return router
