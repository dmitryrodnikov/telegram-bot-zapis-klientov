from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from bot.auth import user_from_init_data, validate_init_data
from bot.config import Settings
from bot.db import Database
from bot.format import format_day, format_range
from bot.notify import admin_booking_text, notify_admins, notify_client
from bot.slots import free_slots, open_days

log = logging.getLogger("booking")
WEBAPP = Path(__file__).resolve().parent.parent / "webapp"
SETTINGS_KEY: web.AppKey[Settings] = web.AppKey("settings")
DB_KEY: web.AppKey[Database] = web.AppKey("db")
BOT_KEY: web.AppKey[object] = web.AppKey("bot")


def create_app(settings: Settings, db: Database, bot) -> web.Application:
    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[DB_KEY] = db
    app[BOT_KEY] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/static/{name}", static_file)
    app.router.add_get("/api/meta", meta)
    app.router.add_get("/api/services", services)
    app.router.add_get("/api/days", days)
    app.router.add_get("/api/slots", slots)
    app.router.add_post("/api/book", book)
    app.router.add_post("/api/my", my_bookings)
    app.router.add_post("/api/cancel", cancel)
    return app


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=dumps)


def error(status: int, message: str) -> web.Response:
    return json_response({"error": message}, status=status)


async def index(_request: web.Request) -> web.Response:
    response = web.FileResponse(WEBAPP / "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


async def static_file(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in {"app.js", "styles.css"}:
        raise web.HTTPNotFound()
    response = web.FileResponse(WEBAPP / name)
    response.headers["Cache-Control"] = "no-cache"
    return response


async def health(_request: web.Request) -> web.Response:
    return json_response({"ok": True})


async def meta(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    return json_response({"demo": settings.allow_demo, "timezone": str(settings.timezone)})


async def services(request: web.Request) -> web.Response:
    settings: Settings = request.app[SETTINGS_KEY]
    return json_response(
        {
            "services": [
                {
                    "id": service.id,
                    "title": service.title,
                    "duration_min": service.duration_min,
                    "price": service.price,
                    "description": service.description,
                }
                for service in settings.services
            ]
        }
    )


async def days(request: web.Request) -> web.Response:
    settings, service = _service_from_query(request)
    if isinstance(service, web.Response):
        return service
    db: Database = request.app[DB_KEY]
    now = datetime.now(settings.timezone)
    payload = [
        {"date": day.isoformat(), "label": format_day(day), "free": len(day_slots)}
        for day, day_slots in open_days(settings, service, db.busy_intervals(), now)
    ]
    return json_response({"days": payload})


async def slots(request: web.Request) -> web.Response:
    settings, service = _service_from_query(request)
    if isinstance(service, web.Response):
        return service
    try:
        day = datetime.strptime(request.query.get("day", ""), "%Y-%m-%d").date()
    except ValueError:
        return error(400, "Укажите день в формате ГГГГ-ММ-ДД")
    db: Database = request.app[DB_KEY]
    now = datetime.now(settings.timezone)
    payload = []
    for slot in free_slots(settings, day, service, db.busy_intervals(), now):
        end = slot + timedelta(minutes=service.duration_min)
        payload.append(
            {
                "starts_at": slot.isoformat(timespec="minutes"),
                "label": f"{slot:%H:%M}–{end:%H:%M}",
            }
        )
    return json_response({"slots": payload})


async def book(request: web.Request) -> web.Response:
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    settings: Settings = request.app[SETTINGS_KEY]
    db: Database = request.app[DB_KEY]
    try:
        user_id, user_name, username = _identify(settings, str(body.get("init_data") or ""))
    except PermissionError:
        return error(401, "Откройте запись из Telegram: подпись запроса не сошлась.")
    service = settings.service(str(body.get("service_id") or ""))
    if service is None:
        return error(404, "Услуга не найдена")
    try:
        starts_at = datetime.fromisoformat(str(body.get("starts_at") or ""))
    except ValueError:
        return error(400, "Некорректное время")
    try:
        booking, code = db.create_booking(settings, service, starts_at, user_id, user_name, username)
    except Exception:
        log.exception("Не удалось сохранить запись из мини-приложения")
        return error(500, "Не получилось сохранить запись")
    if booking is None:
        if code == "taken":
            return error(409, "Это время уже занято. Выберите другое.")
        return error(400, "Это время недоступно. Выберите другое.")
    await notify_client(request.app[BOT_KEY], settings, service, booking)
    await notify_admins(request.app[BOT_KEY], settings, admin_booking_text(service.title, booking))
    return json_response({"booking": _booking_json(settings, booking)})


async def my_bookings(request: web.Request) -> web.Response:
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    settings: Settings = request.app[SETTINGS_KEY]
    db: Database = request.app[DB_KEY]
    try:
        user_id, _, _ = _identify(settings, str(body.get("init_data") or ""))
    except PermissionError:
        return error(401, "Откройте запись из Telegram: подпись запроса не сошлась.")
    now = datetime.now(settings.timezone)
    upcoming = [
        booking
        for booking in db.bookings_for(user_id)
        if booking.starts_at + timedelta(minutes=booking.duration_min) > now
    ]
    return json_response({"bookings": [_booking_json(settings, booking) for booking in upcoming]})


async def cancel(request: web.Request) -> web.Response:
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    settings: Settings = request.app[SETTINGS_KEY]
    db: Database = request.app[DB_KEY]
    try:
        user_id, _, _ = _identify(settings, str(body.get("init_data") or ""))
    except PermissionError:
        return error(401, "Откройте запись из Telegram: подпись запроса не сошлась.")
    try:
        booking_id = int(body.get("id"))
    except (TypeError, ValueError):
        return error(400, "Не указана запись")
    cancelled = db.cancel(booking_id, user_id)
    if cancelled is None:
        return error(404, "Запись не найдена")
    service = settings.service(cancelled.service_id)
    title = service.title if service else cancelled.service_id
    await notify_admins(request.app[BOT_KEY], settings, admin_booking_text(title, cancelled, cancelled=True))
    return json_response({"ok": True})


def _service_from_query(request: web.Request):
    settings: Settings = request.app[SETTINGS_KEY]
    service = settings.service(request.query.get("service_id", ""))
    if service is None:
        return settings, error(404, "Услуга не найдена")
    return settings, service


async def _body(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return error(400, "Нужен JSON")
    if not isinstance(body, dict):
        return error(400, "Нужен JSON-объект")
    return body


def _identify(settings: Settings, init_data: str) -> tuple[int, str, str | None]:
    if init_data:
        parsed = validate_init_data(init_data, settings.token)
        if parsed is None:
            raise PermissionError
        user = user_from_init_data(parsed)
        if user is None:
            raise PermissionError
        return user
    if settings.allow_demo:
        return 0, "Локальная проверка", None
    raise PermissionError


def _booking_json(settings: Settings, booking) -> dict:
    service = settings.service(booking.service_id)
    return {
        "id": booking.id,
        "service_id": booking.service_id,
        "title": service.title if service else booking.service_id,
        "price": service.price if service else "",
        "starts_at": booking.starts_at.isoformat(timespec="minutes"),
        "when": format_range(booking.starts_at, booking.duration_min),
    }
