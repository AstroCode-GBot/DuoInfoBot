import os
import sys

# --- Environment Variables & Defaults ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
import os

# Multiple admin IDs handling (e.g., "6434652846,5684263622")
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

# Secondary check for primary admin
PRIMARY_ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else None

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "duo_info_bot")

DUO_API_URL = os.getenv("DUO_API_URL", "https://duoinfo.onrender.com/api/duo")
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")  # Optional
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/telegram")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")  # Optional, e.g. "@mychannel" or "-100xxxx"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Dhaka")

try:
    REQUEST_COST = int(os.getenv("REQUEST_COST", "1"))
    DAILY_FREE_REQUESTS = int(os.getenv("DAILY_FREE_REQUESTS", "1"))
    REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "5"))
except ValueError:
    REQUEST_COST = 1
    DAILY_FREE_REQUESTS = 1
    REFERRAL_REWARD = 5

PORT = int(os.getenv("PORT", "8080"))

# --- Startup Environment Validation ---
def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_ID:
        missing.append("ADMIN_ID")
    if not MONGO_URI:
        missing.append("MONGO_URI")

    if missing:
        print(f"CRITICAL ERROR: Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    try:
        int(ADMIN_ID)
    except ValueError:
        print("CRITICAL ERROR: ADMIN_ID must be a valid numeric Telegram user ID.", file=sys.stderr)
        sys.exit(1)

# --- Premium Emoji Map ---
PEM = {
    "ok": "6111726981760423576",
    "no": "6311965254817423602",
    "warn": "6314510813214284503",
    "admin": "5353032893096567467",
    "user": "5352861489541714456",
    "money": "6111799373434197692",
    "gift": "6109561463544747928",
    "msg": "5337302974806922068",
    "cookie": "6314402846326397461",
    "rocket": "5352597830089347330",
    "target": "6109432142079466939",
    "pin": "6111410240807245099",
    "hi": "5353027129250453493",
    "add": "6188038822509418008",
    "rem": "6188343249791358585",
    "view": "6188447235244562676",
    "key": "6233302765582424442",
    "wait": "6233136657722251031",
    "earn": "6186175669991379791",
    "bell": "6260082445018731350",
    "crown": "6183661284467152997",
    "gear": "6260111633616475361",
    "group": "6233527959307688784",
    "check": "6188038822509418008",
    "join": "5352597830089347330"
}

def emoji(name: str, fallback: str = "•") -> str:
    emoji_id = PEM.get(name)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback
