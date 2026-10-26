from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from bot.format import person_name


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: int | None = None,
    max_age: int = 86_400,
) -> dict[str, str] | None:
    """Проверяет подпись Telegram Mini App.

    Секрет — HMAC-SHA256 от токена бота с ключом WebAppData.
    Так описано в https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    Дополнительно отклоняем данные старше max_age секунд.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", ""))
    except ValueError:
        return None
    current = now if now is not None else int(time.time())
    if auth_date > current + 60 or current - auth_date > max_age:
        return None
    return parsed


def user_from_init_data(parsed: dict[str, str]) -> tuple[int, str, str | None] | None:
    raw_user = parsed.get("user")
    if not raw_user:
        return None
    try:
        user = json.loads(raw_user)
        user_id = int(user["id"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if user_id <= 0:
        return None
    username = user.get("username") or None
    return user_id, person_name(user.get("first_name"), user.get("last_name")), username
