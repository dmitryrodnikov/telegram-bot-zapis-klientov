from __future__ import annotations

import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Service, Settings
from bot.db import Booking
from bot.format import esc, format_range

log = logging.getLogger("booking")


def admin_booking_text(title: str, booking: Booking, *, cancelled: bool = False) -> str:
    who = esc(booking.user_name)
    if booking.username:
        who += f" (@{esc(booking.username)})"
    heading = "Запись отменена" if cancelled else "Новая запись"
    return (
        f"<b>{heading}</b>\n"
        f"{esc(title)}\n"
        f"{format_range(booking.starts_at, booking.duration_min)}\n"
        f"Клиент: {who}"
    )


def client_booking_text(service: Service, booking: Booking, remind_before_min: int) -> str:
    return (
        "<b>Вы записаны</b>\n\n"
        f"{esc(service.title)}\n"
        f"{format_range(booking.starts_at, booking.duration_min)}\n"
        f"{esc(service.price)}\n\n"
        f"Напоминание придёт примерно за {remind_before_min} мин, пока бот запущен."
    )


async def notify_admins(bot, settings: Settings, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.warning("Не удалось написать администратору %s", admin_id, exc_info=True)


async def notify_client(bot, settings: Settings, service: Service, booking: Booking) -> None:
    if booking.user_id <= 0:
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить запись", callback_data=f"cancel:{booking.id}")]
        ]
    )
    try:
        await bot.send_message(
            booking.user_id,
            client_booking_text(service, booking, settings.remind_before_min),
            reply_markup=markup,
        )
    except Exception:
        log.warning("Не удалось подтвердить запись клиенту %s", booking.user_id, exc_info=True)
