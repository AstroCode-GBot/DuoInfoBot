from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

CUSTOM_EMOJIS = {
    "bkash": {"fallback": "💸", "custom_id": "6237941149873478197"},
    "nagad": {"fallback": "💸", "custom_id": "6235269688805301565"},
    "admin": {"fallback": "👑", "custom_id": "6156655419168138186"},
    "channel": {"fallback": "📣", "custom_id": "6242454421767198453"},
    "verif": {"fallback": "✅", "custom_id": "6325429603628228289"},
    "back": {"fallback": "↩️", "custom_id": "5257980739940533250"},
    "wait": {"fallback": "⏳", "custom_id": "6084396322444544568"},
    "error": {"fallback": "❌", "custom_id": "6258199741809565124"},
    "welcome": {"fallback": "👋", "custom_id": "6158868864923869550"},
    "back_menu": {"fallback": "🔙", "custom_id": "5877410604225924969"},
    "locked": {"fallback": "🔒", "custom_id": "5197288647275071607"},
    "unlocked": {"fallback": "🔓", "custom_id": "6084902127858092382"},
    "link": {"fallback": "🔗", "custom_id": "6312140764361006715"},
    "id": {"fallback": "🆔", "custom_id": "6030656587830399914"},
    "stats": {"fallback": "📊", "custom_id": "5884161133174067365"},
    "users": {"fallback": "👥", "custom_id": "5258513401784573443"},
}
PREMIUM_EMOJIS = {
    "ok": "✅", "no": "❌", "warn": "⚠️", "admin": "📊", "user": "👤",
    "money": "💸", "gift": "🎁", "msg": "💬", "cookie": "🍪", "rocket": "🚀",
    "target": "🎯", "pin": "📌", "hi": "👋", "add": "➕", "rem": "➖", "view": "👀",
    "key": "🔑", "wait": "⏳", "earn": "💰", "bell": "🔔", "crown": "👑", "gear": "⚙️",
    "group": "👥", "check": "✅", "join": "🔗",
}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def cemoji(key: str) -> str:
    item = CUSTOM_EMOJIS.get(key)
    if not item:
        return PREMIUM_EMOJIS.get(key, "")
    cid = str(item.get("custom_id", ""))
    fallback = item.get("fallback", "")
    # Telegram HTML custom emoji syntax; a valid fallback remains if parsing/rendering is unavailable.
    if cid.isdigit() and 1 <= len(cid) <= 64:
        return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>'
    return fallback


@dataclass(frozen=True)
class Settings:
    request_cost: int = 2
    daily_free_requests: int = 1
    referral_reward: int = 10
    referral_min_activity: int = 0
    rate_limit_seconds: float = 3.0
    group_rate_limit_seconds: float = 3.0
    cache_ttl: int = 60
    maintenance_mode: bool = False
    referral_enabled: bool = True
    daily_free_enabled: bool = True
    group_enabled: bool = True
    payment_enabled: bool = True
    uid_min_len: int = 5
    uid_max_len: int = 20
    api_retries: int = 2
    api_connect_timeout: float = 5.0
    api_read_timeout: float = 10.0
    timezone: str = "Asia/Dhaka"


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    admin_log_chat_id: int | None
    api_base_url: str
    bot_username: str
    support_username: str
    support_chat: str
    timezone: str
    database_path: str
    settings_defaults: Settings

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required in the environment")
        raw_admins = os.getenv("ADMIN_IDS", "")
        admins: set[int] = set()
        for part in raw_admins.replace(";", ",").split(","):
            part = part.strip()
            if part:
                try:
                    admins.add(int(part))
                except ValueError:
                    pass
        if not admins:
            raise RuntimeError("ADMIN_IDS must contain at least one numeric Telegram user ID")
        log_id = os.getenv("ADMIN_LOG_CHAT_ID", "").strip()
        try:
            log_chat = int(log_id) if log_id else None
        except ValueError:
            log_chat = None
        timezone = os.getenv("TIMEZONE", "Asia/Dhaka").strip() or "Asia/Dhaka"
        s = Settings(
            request_cost=_int("DEFAULT_REQUEST_COST", 2),
            daily_free_requests=_int("DEFAULT_DAILY_FREE_REQUESTS", 1),
            referral_reward=_int("DEFAULT_REFERRAL_REWARD", 10),
            referral_min_activity=_int("DEFAULT_REFERRAL_MIN_ACTIVITY", 0),
            rate_limit_seconds=float(os.getenv("DEFAULT_RATE_LIMIT", "3")),
            group_rate_limit_seconds=float(os.getenv("DEFAULT_GROUP_RATE_LIMIT", "3")),
            cache_ttl=_int("DEFAULT_CACHE_TTL", 60),
            maintenance_mode=_bool("DEFAULT_MAINTENANCE", False),
            referral_enabled=_bool("DEFAULT_REFERRAL_ENABLED", True),
            daily_free_enabled=_bool("DEFAULT_DAILY_FREE_ENABLED", True),
            group_enabled=_bool("DEFAULT_GROUP_ENABLED", True),
            payment_enabled=_bool("DEFAULT_PAYMENT_ENABLED", True),
            uid_min_len=_int("UID_MIN_LEN", 5),
            uid_max_len=_int("UID_MAX_LEN", 20),
            api_retries=_int("API_RETRIES", 2),
            api_connect_timeout=float(os.getenv("API_CONNECT_TIMEOUT", "5")),
            api_read_timeout=float(os.getenv("API_READ_TIMEOUT", "10")),
            timezone=timezone,
        )
        return cls(
            bot_token=token,
            admin_ids=frozenset(admins),
            admin_log_chat_id=log_chat,
            api_base_url=os.getenv("API_BASE_URL", "https://duoinfo.onrender.com/api/duo").strip(),
            bot_username=os.getenv("BOT_USERNAME", "FFDuoInfoBot").lstrip("@").strip(),
            support_username=os.getenv("SUPPORT_USERNAME", "").strip(),
            support_chat=os.getenv("SUPPORT_CHAT", "").strip(),
            timezone=timezone,
            database_path=os.getenv("DATABASE_PATH", "duo_bot.sqlite3").strip() or "duo_bot.sqlite3",
            settings_defaults=s,
        )
