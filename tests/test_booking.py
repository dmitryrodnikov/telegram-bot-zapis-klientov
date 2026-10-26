from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from aiogram.utils.web_app import check_webapp_signature

from bot.auth import validate_init_data
import bot.handlers  # noqa: F401
import bot.__main__  # noqa: F401
from bot.config import ROOT, settings_from_sources
from bot.db import Database
from bot.slots import free_slots
from bot.web import create_app


TOKEN = "123456:TESTTOKEN"
EXAMPLE = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def future_monday(now: datetime):
    day = now.date() + timedelta(days=1)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


def make_settings(root: Path, **env):
    base = {"BOT_TOKEN": TOKEN, "ALLOW_DEMO": "1", "DB_PATH": str(root / "bookings.sqlite")}
    base.update(env)
    return settings_from_sources(env=base, config=EXAMPLE, root=root)


def signed_init(user: dict, auth_date: int, token: str = TOKEN) -> str:
    payload = {
        "auth_date": str(auth_date),
        "query_id": "AAE",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload)


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(Path(tempfile.mkdtemp()))
        self.tz = self.settings.timezone
        self.day = datetime(2026, 11, 2, tzinfo=self.tz).date()
        self.now = datetime(2026, 11, 2, 8, 0, tzinfo=self.tz)
        self.haircut = self.settings.service("haircut")
        self.consult = self.settings.service("consult")

    def hours(self, service, busy=()):
        slots = free_slots(self.settings, self.day, service, busy, self.now)
        return {(slot.hour, slot.minute) for slot in slots}

    def test_break_removes_overlapping_slots_only(self):
        hours = self.hours(self.haircut)
        self.assertIn((10, 0), hours)
        self.assertIn((13, 0), hours)
        self.assertNotIn((13, 30), hours)
        self.assertNotIn((14, 0), hours)
        self.assertIn((15, 0), hours)

    def test_booking_blocks_overlap_across_services(self):
        start = datetime(2026, 11, 2, 10, 0, tzinfo=self.tz)
        busy = [(start, start + timedelta(minutes=60))]
        self.assertNotIn((10, 30), self.hours(self.consult, busy))
        self.assertIn((11, 0), self.hours(self.consult, busy))

    def test_closed_weekday_has_no_slots(self):
        weekend = datetime(2026, 11, 1, 8, 0, tzinfo=self.tz)
        settings = settings_from_sources(
            env={"BOT_TOKEN": TOKEN, "DB_PATH": "bookings.sqlite"},
            config={**EXAMPLE, "weekdays": [0, 1, 2, 3, 4]},
            root=self.settings.db_path.parent,
        )
        slots = free_slots(settings, weekend.date(), self.haircut, [], weekend)
        self.assertEqual(slots, [])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))
        self.db = Database(self.settings.db_path)
        self.db.init()
        self.addCleanup(self.db.close)
        now = datetime.now(self.settings.timezone)
        self.day = future_monday(now)
        self.haircut = self.settings.service("haircut")
        self.consult = self.settings.service("consult")
        slots = free_slots(self.settings, self.day, self.haircut, [], now)
        self.at = lambda hour, minute: next(slot for slot in slots if slot.hour == hour and slot.minute == minute)

    def book(self, service, start, user_id=5, name="Анна"):
        return self.db.create_booking(self.settings, service, start, user_id, name, "anna")

    def test_second_client_cannot_take_overlapping_time(self):
        first, code = self.book(self.haircut, self.at(10, 0))
        self.assertEqual(code, "ok")
        self.assertIsNotNone(first)
        second, code = self.book(self.consult, self.at(10, 30), user_id=6, name="Борис")
        self.assertIsNone(second)
        self.assertEqual(code, "taken")
        later, code = self.book(self.consult, self.at(11, 0), user_id=6, name="Борис")
        self.assertEqual(code, "ok")
        self.assertEqual(later.user_name, "Борис")

    def test_cancel_frees_the_slot_and_ignores_another_user(self):
        booking, _ = self.book(self.haircut, self.at(12, 0))
        self.assertIsNone(self.db.cancel(booking.id, user_id=6))
        cancelled = self.db.cancel(booking.id, user_id=5)
        self.assertEqual(cancelled.status, "cancelled")
        again, code = self.book(self.haircut, self.at(12, 0), user_id=6, name="Борис")
        self.assertEqual(code, "ok")
        self.assertEqual(again.user_id, 6)

    def test_time_outside_grid_is_closed(self):
        odd = datetime.combine(self.day, datetime.min.time(), tzinfo=self.settings.timezone).replace(hour=3)
        booking, code = self.book(self.haircut, odd)
        self.assertIsNone(booking)
        self.assertEqual(code, "closed")

    def test_reminder_is_due_only_inside_the_window(self):
        soon = (datetime.now(self.settings.timezone) + timedelta(minutes=20)).replace(second=0, microsecond=0)
        self.db.conn.execute(
            """
            INSERT INTO bookings (
                service_id, starts_at, duration_min, user_id, user_name,
                username, status, remind_sent, created_at
            ) VALUES ('consult', ?, 30, 5, 'Анна', NULL, 'booked', 0, '2026-10-26T00:00:00+03:00')
            """,
            (soon.isoformat(timespec="minutes"),),
        )
        due = self.db.reminders_due(datetime.now(self.settings.timezone), timedelta(minutes=60))
        self.assertEqual([item.user_name for item in due], ["Анна"])
        past = (datetime.now(self.settings.timezone) - timedelta(hours=2)).replace(second=0, microsecond=0)
        self.db.conn.execute(
            """
            INSERT INTO bookings (
                service_id, starts_at, duration_min, user_id, user_name,
                username, status, remind_sent, created_at
            ) VALUES ('consult', ?, 30, 5, 'Прошлая', NULL, 'booked', 0, '2026-10-26T00:00:00+03:00')
            """,
            (past.isoformat(timespec="minutes"),),
        )
        due_again = self.db.reminders_due(datetime.now(self.settings.timezone), timedelta(minutes=60))
        self.assertEqual([item.user_name for item in due_again], ["Анна"])


class AuthTests(unittest.TestCase):
    def test_signature_matches_aiogram_and_rejects_tampering(self):
        now = 1_700_000_000
        init_data = signed_init({"id": 42, "first_name": "Анна", "username": "anna"}, now - 30)
        parsed = validate_init_data(init_data, TOKEN, now=now)
        self.assertIsNotNone(parsed)
        self.assertTrue(check_webapp_signature(TOKEN, init_data))
        self.assertEqual(json.loads(parsed["user"])["first_name"], "Анна")
        broken = init_data[:-1] + ("0" if init_data[-1] != "0" else "1")
        self.assertIsNone(validate_init_data(broken, TOKEN, now=now))
        self.assertFalse(check_webapp_signature(TOKEN, broken))

    def test_old_signature_is_rejected_even_if_hash_is_valid(self):
        now = 1_700_000_000
        init_data = signed_init({"id": 42, "first_name": "Анна"}, now - 90_000)
        self.assertTrue(check_webapp_signature(TOKEN, init_data))
        self.assertIsNone(validate_init_data(init_data, TOKEN, now=now))


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        settings = make_settings(Path(tempfile.mkdtemp()))
        self.assertEqual([item.id for item in settings.services], ["haircut", "consult"])
        self.assertEqual(settings.breaks, (("14:00", "15:00"),))

    def test_service_id_must_be_latin(self):
        config = json.loads(json.dumps(EXAMPLE))
        config["services"][0]["id"] = "стрижка"
        with self.assertRaises(ValueError):
            settings_from_sources(
                env={"BOT_TOKEN": TOKEN},
                config=config,
                root=ROOT,
            )


class DummyBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class ApiTests(unittest.TestCase):
    def test_mini_app_books_once_and_keeps_users_apart(self):
        asyncio.run(self._scenario())

    async def _scenario(self):
        tmp = tempfile.TemporaryDirectory()
        settings = make_settings(Path(tmp.name))
        db = Database(settings.db_path)
        db.init()
        bot = DummyBot()
        client = TestClient(TestServer(create_app(settings, db, bot)))
        await client.start_server()
        try:
            index = await client.get("/")
            self.assertEqual(index.status, 200)
            html = await index.text()
            self.assertIn("noindex", html)
            script = await client.get("/static/app.js")
            self.assertEqual(script.status, 200)

            services = await (await client.get("/api/services")).json()
            self.assertEqual(services["services"][0]["id"], "haircut")
            days = await (await client.get("/api/days?service_id=haircut")).json()
            self.assertTrue(days["days"])
            day = days["days"][-1]["date"]
            slots = await (await client.get(f"/api/slots?service_id=haircut&day={day}")).json()
            starts_at = slots["slots"][0]["starts_at"]

            first = await client.post("/api/book", json={"service_id": "haircut", "starts_at": starts_at})
            self.assertEqual(first.status, 200)
            second = await client.post("/api/book", json={"service_id": "consult", "starts_at": starts_at})
            self.assertEqual(second.status, 409)

            now = int(datetime.now().timestamp())
            init_data = signed_init({"id": 77, "first_name": "Анна", "username": "anna"}, now)
            stolen = await client.post(
                "/api/cancel",
                json={"id": 1, "init_data": init_data},
            )
            self.assertEqual(stolen.status, 404)
            mine = await (await client.post("/api/my", json={})).json()
            self.assertEqual(mine["bookings"][0]["title"], "Стрижка")

            bad = await client.post("/api/book", json={"service_id": "consult", "starts_at": starts_at, "init_data": "hash=nope"})
            self.assertEqual(bad.status, 401)
        finally:
            await client.close()
            db.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
