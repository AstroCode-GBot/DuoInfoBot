import os
import pytz

# Telegram Bot Setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# Multiple Admin IDs handling (e.g. "6434652846,5684263622")
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
# Single Primary Admin fallback (Crash protirodher jonno)
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else None

# Group Logs & Support
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")

# Database & External APIs
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "duo_info_bot")
DUO_API_URL = os.getenv("DUO_API_URL", "https://duoinfo.onrender.com/api/duo")

# Bot Economics & Timezone
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Dhaka")
TIMEZONE = pytz.timezone(TIMEZONE_STR)
REQUEST_COST = int(os.getenv("REQUEST_COST", "1"))
DAILY_FREE_REQUESTS = int(os.getenv("DAILY_FREE_REQUESTS", "1"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "5"))

# Web Server Port
PORT = int(os.getenv("PORT", "8080"))


def validate_config():
    """Environment variables validation function."""
    if not BOT_TOKEN:
        raise ValueError("CRITICAL ERROR: BOT_TOKEN is missing in Environment Variables!")
    
    if not ADMIN_IDS:
        raise ValueError("CRITICAL ERROR: ADMIN_ID must be valid numeric Telegram user ID(s).")
    
    if not MONGO_URI:
        raise ValueError("CRITICAL ERROR: MONGO_URI is missing in Environment Variables!")

    print("Configuration loaded & validated successfully!")
