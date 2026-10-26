from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo

from bot.config import load_settings
from bot.db import Database
from bot.format import esc, format_range
from bot.handlers import make_router
from bot.web import create_app

log = logging.getLogger("booking")


async def reminder_loop(bot: Bot, settings, db: Database) -> None:
    lead = timedelta(minutes=settings.remind_before_min)
    while True:
        try:
            now = datetime.now(settings.timezone)
            for booking in db.reminders_due(now, lead):
                await _send_reminder(bot, settings, db, booking)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка цикла напоминаний")
        await asyncio.sleep(30)


async def _send_reminder(bot: Bot, settings, db: Database, booking) -> None:
    if booking.user_id <= 0:
        db.mark_reminded(booking.id)
        return
    service = settings.service(booking.service_id)
    title = service.title if service else booking.service_id
    text = (
        "<b>Напоминание о записи</b>\n\n"
        f"{esc(title)}\n"
        f"{format_range(booking.starts_at, booking.duration_min)}\n\n"
        "Если планы изменились, отмените запись кнопкой ниже."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отменить запись", callback_data=f"cancel:{booking.id}")]]
    )
    try:
        await bot.send_message(booking.user_id, text, reply_markup=markup)
    except TelegramForbiddenError:
        db.mark_reminded(booking.id)
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            db.mark_reminded(booking.id)
        else:
            log.warning("Напоминание для %s не отправилось", booking.user_id, exc_info=True)
    except Exception:
        log.warning("Напоминание для %s не отправилось", booking.user_id, exc_info=True)
    else:
        db.mark_reminded(booking.id)


async def run() -> None:
    settings = load_settings()
    db = Database(settings.db_path)
    db.init()
    bot = Bot(token=settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception as exc:
        await bot.session.close()
        details = str(exc).replace(settings.token, "...")
        raise SystemExit(f"Telegram не принял токен. Проверьте BOT_TOKEN в .env. {details}") from exc

    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, db, bot))
    app = create_app(settings, db, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        await bot.session.close()
        raise SystemExit(
            f"Не удалось занять {settings.host}:{settings.port}. Измените PORT в .env. {exc}"
        ) from exc

    if settings.webapp_url.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Записаться", web_app=WebAppInfo(url=settings.webapp_url))
            )
        except Exception:
            log.warning("Не удалось поставить кнопку меню. Проверьте WEBAPP_URL.", exc_info=True)
    elif settings.webapp_url:
        log.warning("Для мини-приложения внутри Telegram WEBAPP_URL должен начинаться с https://")

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Записаться"),
            BotCommand(command="my", description="Мои записи"),
            BotCommand(command="today", description="Записи на сегодня"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=True)
    host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
    log.info("Бот @%s запущен. Мини-приложение в браузере: http://%s:%s", me.username, host, settings.port)
    task = asyncio.create_task(reminder_loop(bot, settings, db))
    try:
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runner.cleanup()


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10, 3.11, 3.12, 3.13 или 3.14.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
