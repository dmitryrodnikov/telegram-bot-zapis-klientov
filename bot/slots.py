from __future__ import annotations

from datetime import datetime, timedelta, time
from typing import Sequence

from bot.config import Service, Settings


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def free_slots(
    settings: Settings,
    day,
    service: Service,
    busy: Sequence[tuple[datetime, datetime]],
    now: datetime,
) -> list[datetime]:
    """Окна одного специалиста: услуга занимает duration_min и не пересекает занятое или перерыв."""
    if day.weekday() not in settings.weekdays or day.isoformat() in settings.closed_dates:
        return []
    if now.tzinfo is None:
        raise ValueError("now должно быть с часовым поясом")
    timezone = settings.timezone
    now = now.astimezone(timezone)
    opening = datetime.combine(day, parse_hhmm(settings.work_start), tzinfo=timezone)
    closing = datetime.combine(day, parse_hhmm(settings.work_end), tzinfo=timezone)
    duration = timedelta(minutes=service.duration_min)
    step = timedelta(minutes=settings.step_min)
    notice = timedelta(minutes=settings.min_notice_min)
    breaks = [
        (
            datetime.combine(day, parse_hhmm(start), tzinfo=timezone),
            datetime.combine(day, parse_hhmm(end), tzinfo=timezone),
        )
        for start, end in settings.breaks
    ]
    slots: list[datetime] = []
    cursor = opening
    while cursor + duration <= closing:
        end = cursor + duration
        taken = any(overlaps(cursor, end, busy_start, busy_end) for busy_start, busy_end in busy)
        in_break = any(overlaps(cursor, end, break_start, break_end) for break_start, break_end in breaks)
        if cursor >= now + notice and not taken and not in_break:
            slots.append(cursor)
        cursor += step
    return slots


def open_days(settings: Settings, service: Service, busy: Sequence[tuple[datetime, datetime]], now: datetime):
    today = now.astimezone(settings.timezone).date()
    found = []
    for offset in range(settings.horizon_days):
        day = today + timedelta(days=offset)
        slots = free_slots(settings, day, service, busy, now)
        if slots:
            found.append((day, slots))
    return found
