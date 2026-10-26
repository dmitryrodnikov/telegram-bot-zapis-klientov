from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import Service, Settings
from bot.slots import free_slots

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    username TEXT,
    status TEXT NOT NULL,
    remind_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_status ON bookings(status, starts_at);
"""


@dataclass(frozen=True)
class Booking:
    id: int
    service_id: str
    starts_at: datetime
    duration_min: int
    user_id: int
    user_name: str
    username: str | None
    status: str
    remind_sent: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def init(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def create_booking(
        self,
        settings: Settings,
        service: Service,
        starts_at: datetime,
        user_id: int,
        user_name: str,
        username: str | None,
    ) -> tuple[Booking | None, str]:
        # BEGIN IMMEDIATE берёт блокировку сразу. Проверка окна и INSERT
        # идут в одной транзакции, поэтому два клиента не занимают
        # пересекающееся время одновременно. Уникального индекса по минуте
        # мало: 10:00–11:00 и 10:30–11:30 — разные starts_at, но один человек.
        starts_at = _normalize(starts_at, settings.timezone)
        name = (user_name or "Клиент").strip().replace("\n", " ")[:80] or "Клиент"
        login = (username or "").strip().lstrip("@")[:32] or None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            busy = self.busy_intervals()
            now = datetime.now(settings.timezone)
            offered = {
                slot.isoformat(timespec="minutes")
                for slot in free_slots(settings, starts_at.date(), service, busy, now)
            }
            key = starts_at.isoformat(timespec="minutes")
            if key not in offered:
                self.conn.rollback()
                without_busy = {
                    slot.isoformat(timespec="minutes")
                    for slot in free_slots(settings, starts_at.date(), service, [], now)
                }
                return None, "taken" if key in without_busy else "closed"
            created_at = now.isoformat(timespec="seconds")
            cursor = self.conn.execute(
                """
                INSERT INTO bookings (
                    service_id, starts_at, duration_min, user_id, user_name,
                    username, status, remind_sent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'booked', 0, ?)
                """,
                (service.id, key, service.duration_min, user_id, name, login, created_at),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        booking = self.get(cursor.lastrowid)
        if booking is None:
            return None, "closed"
        return booking, "ok"

    def cancel(self, booking_id: int, user_id: int) -> Booking | None:
        booking = self.get(booking_id)
        if booking is None or booking.user_id != user_id or booking.status != "booked":
            return None
        self.conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'booked'",
            (booking_id, user_id),
        )
        return self.get(booking_id)

    def get(self, booking_id: int) -> Booking | None:
        row = self.conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return _map(row) if row else None

    def active(self) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'booked' ORDER BY starts_at"
        ).fetchall()
        return [_map(row) for row in rows]

    def bookings_for(self, user_id: int) -> list[Booking]:
        rows = self.conn.execute(
            """
            SELECT * FROM bookings
            WHERE user_id = ? AND status = 'booked'
            ORDER BY starts_at
            """,
            (user_id,),
        ).fetchall()
        return [_map(row) for row in rows]

    def busy_intervals(self) -> list[tuple[datetime, datetime]]:
        return [
            (booking.starts_at, booking.starts_at + timedelta(minutes=booking.duration_min))
            for booking in self.active()
        ]

    def reminders_due(self, now: datetime, lead: timedelta) -> list[Booking]:
        due: list[Booking] = []
        for booking in self.active():
            if booking.remind_sent:
                continue
            if booking.starts_at <= now:
                self.mark_reminded(booking.id)
                continue
            if booking.starts_at - now <= lead:
                due.append(booking)
        return due

    def mark_reminded(self, booking_id: int) -> None:
        self.conn.execute("UPDATE bookings SET remind_sent = 1 WHERE id = ?", (booking_id,))


def _normalize(moment: datetime, timezone: ZoneInfo) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone)
    else:
        moment = moment.astimezone(timezone)
    return moment.replace(second=0, microsecond=0)


def _map(row: sqlite3.Row) -> Booking:
    return Booking(
        id=row["id"],
        service_id=row["service_id"],
        starts_at=datetime.fromisoformat(row["starts_at"]),
        duration_min=row["duration_min"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        username=row["username"],
        status=row["status"],
        remind_sent=bool(row["remind_sent"]),
    )
