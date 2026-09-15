import os
import pytz

# Telegram Bot Setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# Multiple Admin IDs handling (e.g. "6434652846,5684263622")
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
# Single Primary Admin fallback
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else None

# Telegram DB & Logs Storage Group
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")

# External APIs
DUO_API_URL = os.getenv("DUO_API_URL", "https://duoinfo.onrender.com/api/duo")

# Bot Economics & Timezone
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Dhaka")
TIMEZONE = pytz.timezone(TIMEZONE_STR)
REQUEST_COST = int(os.getenv("REQUEST_COST", "1"))
DAILY_FREE_REQUESTS = int(os.getenv("DAILY_FREE_REQUESTS", "1"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "5"))

# Web Server Port
PORT = int(os.getenv("PORT", "8080"))

# Emoji Identifiers Map (Custom Emoji Fallbacks)
PEM = {
    "target": "🎯",
    "money": "💰",
    "gift": "🎁",
    "user": "👤",
    "view": "📊",
    "msg": "💬",
    "crown": "👑",
    "no": "❌",
    "join": "📢",
    "check": "✅",
    "warn": "⚠️",
    "group": "🤝",
    "rocket": "🚀",
    "hi": "👋",
    "wait": "⏳",
    "ok": "✅",
    "add": "➕",
    "rem": "➖",
    "bell": "📢",
    "key": "🔐"
}

def emoji(name: str, fallback: str = "") -> str:
    """Returns the mapped emoji or fallback symbol."""
    return PEM.get(name, fallback)

def validate_config():
    """Environment variables validation function."""
    if not BOT_TOKEN:
        raise ValueError("CRITICAL ERROR: BOT_TOKEN is missing in Environment Variables!")
    
    if not ADMIN_IDS:
        raise ValueError("CRITICAL ERROR: ADMIN_ID must be valid numeric Telegram user ID(s).")
    
    if not LOG_GROUP_ID:
        print("WARNING: LOG_GROUP_ID is not set! Telegram DB backup will not persist across reboots.")

    print("Configuration loaded & validated successfully!")
