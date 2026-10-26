from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def esc(text: str) -> str:
    return escape(text, quote=False)


def person_name(first: str | None, last: str | None) -> str:
    parts = [part.strip() for part in (first or "", last or "") if part and part.strip()]
    return " ".join(parts)[:80].replace("\n", " ") or "Клиент"


def format_day(day) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def format_range(start: datetime, duration_min: int) -> str:
    end = start + timedelta(minutes=duration_min)
    return f"{format_day(start)}, {start:%H:%M}–{end:%H:%M}"


def compact(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M")


def parse_compact(value: str, timezone: ZoneInfo) -> datetime:
    parsed = datetime.strptime(value, "%Y%m%d%H%M")
    return parsed.replace(tzinfo=timezone)
