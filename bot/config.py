from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
SERVICE_ID = re.compile(r"^[a-z0-9_]{1,32}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Service:
    id: str
    title: str
    duration_min: int
    price: str
    description: str


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    webapp_url: str
    host: str
    port: int
    allow_demo: bool
    db_path: Path
    timezone: ZoneInfo
    horizon_days: int
    work_start: str
    work_end: str
    step_min: int
    remind_before_min: int
    min_notice_min: int
    weekdays: frozenset[int]
    closed_dates: frozenset[str]
    breaks: tuple[tuple[str, str], ...]
    services: tuple[Service, ...]

    def service(self, service_id: str) -> Service | None:
        for item in self.services:
            if item.id == service_id:
                return item
        return None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    config_path = _resolve(ROOT, os.environ.get("CONFIG_PATH", "config.json"))
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Нет файла настроек: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json не читается как JSON: {exc}") from exc
    try:
        return settings_from_sources(env=os.environ, config=config, root=ROOT)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def settings_from_sources(*, env: Mapping[str, str], config: dict, root: Path) -> Settings:
    if not isinstance(config, dict):
        raise ValueError("config.json должен быть объектом с услугами и часами работы")
    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise ValueError("В .env нет BOT_TOKEN. Скопируйте токен из @BotFather.")
    timezone_name = str(config.get("timezone") or "Europe/Moscow")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс: {timezone_name}") from exc

    work_start = _hhmm(config.get("work_start"), "work_start")
    work_end = _hhmm(config.get("work_end"), "work_end")
    if work_start >= work_end:
        raise ValueError("work_start должен быть раньше work_end")

    services = _services(config.get("services"))
    step_min = _bounded_int(config.get("step_min", 30), "step_min", 5, 240)
    horizon_days = _bounded_int(config.get("horizon_days", 7), "horizon_days", 1, 60)
    remind_before_min = _bounded_int(config.get("remind_before_min", 60), "remind_before_min", 5, 7 * 24 * 60)
    min_notice_min = _bounded_int(config.get("min_notice_min", 30), "min_notice_min", 0, 7 * 24 * 60)
    weekdays = _weekdays(config.get("weekdays", list(range(7))))
    closed_dates = _closed_dates(config.get("closed_dates", []))
    breaks = _breaks(config.get("breaks", []))
    port = _bounded_int(env.get("PORT", "8080"), "PORT", 1, 65535)
    webapp_url = (env.get("WEBAPP_URL") or "").strip().rstrip("/")
    if webapp_url and not (webapp_url.startswith("https://") or webapp_url.startswith("http://")):
        raise ValueError("WEBAPP_URL должен начинаться с https:// или http://")

    return Settings(
        token=token,
        admin_ids=_admin_ids(env.get("ADMIN_IDS", "")),
        webapp_url=webapp_url,
        host=(env.get("HOST") or "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        allow_demo=_flag(env.get("ALLOW_DEMO"), default=False),
        db_path=_resolve(root, env.get("DB_PATH", "data/bookings.sqlite")),
        timezone=timezone,
        horizon_days=horizon_days,
        work_start=work_start,
        work_end=work_end,
        step_min=step_min,
        remind_before_min=remind_before_min,
        min_notice_min=min_notice_min,
        weekdays=weekdays,
        closed_dates=closed_dates,
        breaks=breaks,
        services=services,
    )


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _flag(value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _admin_ids(value: str) -> frozenset[int]:
    ids: list[int] = []
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValueError(f"ADMIN_IDS содержит не число: {item}")
        ids.append(int(item))
    return frozenset(ids)


def _hhmm(value, field: str) -> str:
    text = str(value or "")
    if not HHMM.match(text):
        raise ValueError(f"{field} запишите как ЧЧ:ММ, например 10:00")
    return text


def _bounded_int(value, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} должно быть числом") from exc
    if not low <= number <= high:
        raise ValueError(f"{field} должно быть от {low} до {high}")
    return number


def _weekdays(value) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("weekdays — список дней, где 0 это понедельник, а 6 воскресенье")
    days: list[int] = []
    for item in value:
        day = _bounded_int(item, "weekdays", 0, 6)
        days.append(day)
    return frozenset(days)


def _closed_dates(value) -> frozenset[str]:
    if not isinstance(value, list):
        raise ValueError("closed_dates должен быть списком дат ГГГГ-ММ-ДД")
    dates: list[str] = []
    for item in value:
        text = str(item)
        if not ISO_DATE.match(text):
            raise ValueError(f"closed_dates содержит не дату: {text}")
        dates.append(text)
    return frozenset(dates)


def _breaks(value) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("breaks должен быть списком перерывов")
    breaks: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Каждый перерыв — объект со start и end")
        start = _hhmm(item.get("start"), "breaks.start")
        end = _hhmm(item.get("end"), "breaks.end")
        if start >= end:
            raise ValueError("Перерыв должен начинаться раньше, чем заканчивается")
        breaks.append((start, end))
    return tuple(breaks)


def _services(value) -> tuple[Service, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("В config.json нужна хотя бы одна услуга")
    services: list[Service] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Услуга должна быть объектом")
        service_id = str(item.get("id") or "")
        if not SERVICE_ID.match(service_id):
            raise ValueError(
                f"id услуги «{service_id}» можно составить только из латиницы, цифр и _; до 32 символов"
            )
        if service_id in seen:
            raise ValueError(f"Услуга {service_id} повторяется")
        seen.add(service_id)
        title = str(item.get("title") or "").strip()
        price = str(item.get("price") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title or len(title) > 40:
            raise ValueError(f"У услуги {service_id} название должно быть от 1 до 40 символов")
        if not price or len(price) > 40:
            raise ValueError(f"У услуги {service_id} заполните price, до 40 символов")
        if len(description) > 300:
            raise ValueError(f"Описание услуги {service_id} длиннее 300 символов")
        duration_min = _bounded_int(item.get("duration_min"), f"{service_id}.duration_min", 5, 720)
        services.append(
            Service(
                id=service_id,
                title=title,
                duration_min=duration_min,
                price=price,
                description=description,
            )
        )
    return tuple(services)
