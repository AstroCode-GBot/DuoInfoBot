import csv
import html
import io
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask

# ============================================================
# EMBEDDED CONFIGURATION
# No separate config.py is required. All values come from
# Render Environment Variables.
# ============================================================

class _Config:
    def __init__(self):
        self.BOT_TOKEN = os.getenv("BOT_TOKEN")
        self.BOT_USERNAME = os.getenv("BOT_USERNAME", "")

        admin_raw = os.getenv("ADMIN_ID", "")
        self.ADMIN_IDS = [
            int(x.strip()) for x in admin_raw.split(",")
            if x.strip().isdigit()
        ]
        self.ADMIN_ID = self.ADMIN_IDS[0] if self.ADMIN_IDS else None

        self.LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")
        self.FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "")
        self.SUPPORT_URL = os.getenv("SUPPORT_URL", "")

        self.DUO_API_URL = os.getenv(
            "DUO_API_URL",
            "https://duoinfo.onrender.com/api/duo"
        )

        self.TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Dhaka")
        try:
            import pytz
            self.TIMEZONE = pytz.timezone(self.TIMEZONE_STR)
        except Exception:
            self.TIMEZONE_STR = "Asia/Dhaka"
            self.TIMEZONE = pytz.timezone("Asia/Dhaka")

        self.REQUEST_COST = self._int_env("REQUEST_COST", 1, minimum=0)
        self.DAILY_FREE_REQUESTS = self._int_env("DAILY_FREE_REQUESTS", 1, minimum=0)
        self.REFERRAL_REWARD = self._int_env("REFERRAL_REWARD", 5, minimum=0)
        self.PORT = self._int_env("PORT", 8080, minimum=1)

        self.PEM = {
            "target": "🎯", "money": "💰", "gift": "🎁",
            "user": "👤", "view": "📊", "msg": "💬",
            "crown": "👑", "no": "❌", "join": "📢",
            "check": "✅", "warn": "⚠️", "group": "🤝",
            "rocket": "🚀", "hi": "👋", "wait": "⏳",
            "ok": "✅", "add": "➕", "rem": "➖",
            "bell": "📢", "key": "🔐"
        }

    @staticmethod
    def _int_env(name, default, minimum=None):
        raw = os.getenv(name, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def emoji(self, name: str, fallback: str = "") -> str:
        return self.PEM.get(name, fallback)

    def validate_config(self):
        if not self.BOT_TOKEN:
            raise ValueError(
                "CRITICAL ERROR: BOT_TOKEN is missing in Environment Variables!"
            )
        if not self.ADMIN_IDS:
            raise ValueError(
                "CRITICAL ERROR: ADMIN_ID must be valid numeric Telegram user ID(s)."
            )
        if not self.LOG_GROUP_ID:
            print(
                "WARNING: LOG_GROUP_ID is not set! "
                "Telegram DB backup will not persist across reboots."
            )
        print("Configuration loaded & validated successfully!")

config = _Config()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Config Validation ---
config.validate_config()

# --- Telegram API Client ---
session = requests.Session()
BASE_URL = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

def json_dumps(data: Any) -> str:
    return json.dumps(data)

def esc(text: Any) -> str:
    if text is None:
        return ""
    return html.escape(str(text))

class TelegramConflictError(RuntimeError):
    """Raised when another process is already polling the same bot token."""
    pass


def api_call(
    method: str,
    payload: dict = None,
    files: dict = None,
    *,
    raise_on_conflict: bool = False,
    max_retries: int = 3
) -> Optional[dict]:
    url = f"{BASE_URL}/{method}"
    http_timeout = 45 if method == "getUpdates" else (
        35 if method in {"getFile", "getChatMember"} else 25
    )

    for attempt in range(max_retries + 1):
        try:
            response = session.post(
                url,
                data=payload,
                files=files,
                timeout=http_timeout
            )

            if response.status_code == 429:
                try:
                    retry_after = int(
                        response.json().get("parameters", {}).get("retry_after", 3)
                    )
                except (ValueError, TypeError, json.JSONDecodeError):
                    retry_after = 3

                if attempt >= max_retries:
                    logger.error(f"Telegram Rate Limit (429) exhausted for {method}.")
                    return None

                retry_after = max(1, min(retry_after, 60))
                logger.warning(
                    f"Telegram Rate Limit (429) on {method}. "
                    f"Sleeping for {retry_after}s..."
                )
                time.sleep(retry_after)
                continue

            if response.status_code == 409:
                try:
                    description = response.json().get(
                        "description",
                        "Conflict: another getUpdates request is active."
                    )
                except (ValueError, TypeError, json.JSONDecodeError):
                    description = "Conflict: another getUpdates request is active."

                if method == "getUpdates" and raise_on_conflict:
                    raise TelegramConflictError(description)

                logger.error(f"Telegram API Conflict [{method}]: {description}")
                return None

            try:
                res_json = response.json()
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.error(
                    f"Invalid JSON response from Telegram [{method}] "
                    f"(HTTP {response.status_code}): {exc}"
                )
                return None

            if not res_json.get("ok"):
                logger.error(
                    f"Telegram API Error [{method}]: {res_json.get('description')}"
                )
                return None

            return res_json.get("result")

        except requests.exceptions.ReadTimeout:
            if method == "getUpdates":
                return []
            if attempt < max_retries:
                time.sleep(1)
                continue
            logger.error(f"HTTP Timeout calling Telegram API [{method}]")
            return None

        except requests.exceptions.RequestException as exc:
            if attempt < max_retries:
                logger.warning(
                    f"Telegram HTTP error [{method}] "
                    f"attempt {attempt + 1}/{max_retries + 1}: {exc}"
                )
                time.sleep(min(2 ** attempt, 5))
                continue
            logger.error(f"HTTP Error calling Telegram API [{method}]: {exc}")
            return None

        except TelegramConflictError:
            raise

        except Exception as exc:
            logger.exception(f"Unexpected error calling Telegram API [{method}]: {exc}")
            return None

    return None


def send_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML") -> Optional[dict]:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json_dumps(reply_markup)
    return api_call("sendMessage", payload)

def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML") -> Optional[dict]:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json_dumps(reply_markup)
    return api_call("editMessageText", payload)

def delete_message(chat_id: int, message_id: int) -> bool:
    return bool(api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

def answer_callback(callback_query_id: str, text: str = None, show_alert: bool = False) -> bool:
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    return bool(api_call("answerCallbackQuery", payload))

def send_document(chat_id: int, file_data: bytes, filename: str, caption: str = "") -> Optional[dict]:
    files = {"document": (filename, file_data, "application/json" if filename.endswith('.json') else "text/csv")}
    payload = {"chat_id": chat_id, "caption": caption}
    return api_call("sendDocument", payload, files=files)

# --- TELEGRAM-BASED DATABASE SYSTEM ---
def default_db() -> Dict[str, Any]:
    return {
        "users": {},
        "requests": [],
        "referrals": [],
        "settings": {
            "request_cost": config.REQUEST_COST,
            "daily_free_requests": config.DAILY_FREE_REQUESTS,
            "referral_reward": config.REFERRAL_REWARD,
            "force_sub_channel": config.FORCE_SUB_CHANNEL,
            "support_url": config.SUPPORT_URL
        },
        "admins": [],
        "groups": {},
        "transactions": []
    }


DB_DATA: Dict[str, Any] = default_db()
LAST_SAVE_TIME = 0


def normalize_db(raw: Any) -> Dict[str, Any]:
    """Keep older Telegram backups compatible with the current DB schema."""
    base = default_db()
    if not isinstance(raw, dict):
        return base

    for key in ("users", "requests", "referrals", "admins", "groups", "transactions"):
        value = raw.get(key)
        expected_type = list if key in {
            "requests", "referrals", "admins", "transactions"
        } else dict
        if isinstance(value, expected_type):
            base[key] = value

    raw_settings = raw.get("settings")
    if isinstance(raw_settings, dict):
        for key in base["settings"]:
            if key in raw_settings:
                base["settings"][key] = raw_settings[key]

    return base


def load_db_from_telegram():
    global DB_DATA

    if not config.LOG_GROUP_ID:
        logger.warning(
            "LOG_GROUP_ID not configured! Telegram Database Persistence disabled."
        )
        return

    logger.info("Fetching latest Database Backup from Telegram Channel/Group...")

    try:
        res = api_call("getChat", {"chat_id": config.LOG_GROUP_ID})

        if not res:
            logger.warning(
                "Could not access LOG_GROUP_ID. Make sure the bot is a member/admin "
                "of the configured group/channel and the chat ID is correct."
            )
            return

        pinned = res.get("pinned_message") or {}
        doc = pinned.get("document") or {}
        file_id = doc.get("file_id")

        if not file_id:
            logger.info(
                "No pinned database_backup.json document found. "
                "Starting with the current/default DB."
            )
            return

        file_info = api_call("getFile", {"file_id": file_id})
        file_path = file_info.get("file_path") if file_info else None
        if not file_path:
            logger.warning("Telegram returned no file_path for the pinned DB backup.")
            return

        file_url = (
            f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_path}"
        )
        dl_res = requests.get(file_url, timeout=35)
        dl_res.raise_for_status()

        restored = json.loads(dl_res.text)
        DB_DATA = normalize_db(restored)
        logger.info("Successfully loaded database backup from Telegram pinned message!")

    except requests.exceptions.RequestException as exc:
        logger.error(f"Failed to download DB backup from Telegram: {exc}")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error(f"Invalid DB backup JSON: {exc}")
    except Exception as exc:
        logger.exception(f"Failed to restore DB from Telegram: {exc}")

def save_db_to_telegram(force=False):
    global LAST_SAVE_TIME
    now = time.time()
    if not force and (now - LAST_SAVE_TIME < 5):
        return

    LAST_SAVE_TIME = now
    if not config.LOG_GROUP_ID:
        return

    try:
        json_bytes = json.dumps(DB_DATA, indent=2).encode('utf-8')
        res = send_document(
            int(config.LOG_GROUP_ID), 
            json_bytes, 
            "database_backup.json", 
            caption=f"📦 <b>Automated DB Backup</b>\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if res and "message_id" in res:
            api_call("pinChatMessage", {
                "chat_id": config.LOG_GROUP_ID,
                "message_id": res["message_id"],
                "disable_notification": True
            })
    except Exception as e:
        logger.error(f"Failed to auto-backup DB to Telegram: {e}")

# Initialize DB Load
load_db_from_telegram()

# --- Global In-Memory Caches & States ---
USER_STATES: Dict[int, str] = {}
STATE_DATA: Dict[int, Dict[str, Any]] = {}
RATE_LIMITS: Dict[int, float] = {}
API_CACHE: Dict[str, Tuple[float, dict]] = {}

# --- Flask Server for Render Health Check ---
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return "Duo Bot is running", 200

def run_flask():
    app.run(host="0.0.0.0", port=config.PORT, threaded=True, use_reloader=False)

# --- Dynamic Settings Helper ---
def get_db_settings() -> dict:
    return DB_DATA.get("settings", {
        "request_cost": config.REQUEST_COST,
        "daily_free_requests": config.DAILY_FREE_REQUESTS,
        "referral_reward": config.REFERRAL_REWARD,
        "force_sub_channel": config.FORCE_SUB_CHANNEL,
        "support_url": config.SUPPORT_URL
    })

# --- Date/Time Helpers ---
def get_local_now() -> datetime:
    import pytz
    return datetime.now(config.TIMEZONE)

def get_today_str() -> str:
    return get_local_now().strftime("%Y-%m-%d")

# --- Access Control & DB User Sync ---
def is_admin(user_id: int) -> bool:
    if str(user_id) == str(config.ADMIN_ID):
        return True
    return user_id in DB_DATA.get("admins", [])

def sync_user(user: dict) -> dict:
    user_id = str(user["id"])
    today = get_today_str()
    users = DB_DATA["users"]
    
    if user_id not in users:
        new_user = {
            "telegram_id": user["id"],
            "username": user.get("username", ""),
            "first_name": user.get("first_name", "User"),
            "coins": 0,
            "daily_free_used": 0,
            "daily_free_date": today,
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "referrer_id": None,
            "referral_count": 0,
            "referral_earnings": 0,
            "banned": False,
            "phone_number": None,
            "joined_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        }
        users[user_id] = new_user
        save_db_to_telegram()
        return new_user

    existing = users[user_id]

    if existing.get("daily_free_date") != today:
        existing["daily_free_date"] = today
        existing["daily_free_used"] = 0
        save_db_to_telegram()

    if existing.get("username") != user.get("username", "") or existing.get("first_name") != user.get("first_name", ""):
        existing["username"] = user.get("username", "")
        existing["first_name"] = user.get("first_name", "")
        save_db_to_telegram()

    return existing

# --- Force Subscription Checker ---
def check_force_sub(user_id: int) -> bool:
    settings = get_db_settings()
    channel = settings.get("force_sub_channel")
    if not channel or not channel.strip():
        return True
    
    res = api_call("getChatMember", {"chat_id": channel.strip(), "user_id": user_id})
    if not res:
        return False
    
    status = res.get("status")
    return status in ["creator", "administrator", "member"]

def get_force_sub_keyboard() -> dict:
    settings = get_db_settings()
    channel = settings.get("force_sub_channel", "").strip()
    if channel.startswith("@"):
        url = f"https://t.me/{channel[1:]}"
    else:
        url = "https://t.me/"

    return {
        "inline_keyboard": [
            [{"text": f"{config.emoji('join', '📢')} Join Channel", "url": url}],
            [{"text": f"{config.emoji('check', '✅')} I Have Joined", "callback_data": "check_sub"}]
        ]
    }

# --- Reply Keyboard (Bot UI Bottom Buttons) ---
def get_bottom_reply_keyboard() -> dict:
    return {
        "keyboard": [
            [
                {
                    "text": "SHARE NUMBER",
                    "request_contact": True
                }
            ],
            [
                {
                    "text": "🛡️ চুক্তিপত্র / Policy"
                }
            ]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# --- Button UI Builder ---
def get_main_keyboard(user_id: int) -> dict:
    rows = [
        [
            {"text": f"{config.emoji('target', '🔎')} Check Duo", "callback_data": "check_duo"},
            {"text": f"{config.emoji('money', '💰')} Coins", "callback_data": "coins"}
        ],
        [
            {"text": f"{config.emoji('gift', '🎁')} Referral", "callback_data": "referral"},
            {"text": f"{config.emoji('user', '👤')} Profile", "callback_data": "profile"}
        ],
        [
            {"text": f"{config.emoji('view', '📊')} History", "callback_data": "history_0"},
            {"text": f"{config.emoji('msg', '💬')} Support", "callback_data": "support"}
        ]
    ]
    if is_admin(user_id):
        rows.append([{"text": f"{config.emoji('crown', '👑')} Admin Panel", "callback_data": "admin_panel"}])
    return {"inline_keyboard": rows}

def get_back_keyboard(target: str = "main_menu") -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": target}]
        ]
    }

# --- Logging Group Helper ---
def log_to_group(text: str):
    if config.LOG_GROUP_ID:
        send_message(int(config.LOG_GROUP_ID), text)

# --- Core Business Logic: Duo API ---
def fetch_duo_info(uid: str) -> Tuple[bool, Optional[dict], str]:
    now = time.time()
    cached = API_CACHE.get(uid)
    if cached:
        cache_time, cache_data = cached
        if now - cache_time < 30:
            return True, cache_data, "cache"

    url = str(config.DUO_API_URL).strip()
    if not url:
        return False, None, "Duo API URL is not configured."

    try:
        resp = requests.get(
            url,
            params={"uid": str(uid)},
            headers={"Accept": "application/json", "User-Agent": "DuoInfoBot/2.0"},
            timeout=(10, 30),
        )

        logger.info("Duo API: %s?uid=%s -> HTTP %s", url, uid, resp.status_code)

        if not (200 <= resp.status_code < 300):
            body = (resp.text or "").strip().replace("\n", " ")[:300]
            logger.error("Duo API HTTP error %s: %s", resp.status_code, body)
            return False, None, f"Duo API returned HTTP {resp.status_code}."

        if not resp.text or not resp.text.strip():
            return False, None, "Duo API returned an empty response."

        try:
            data = resp.json()
        except ValueError:
            body = resp.text.strip()[:300]
            logger.error("Duo API returned non-JSON response: %s", body)
            return False, None, "Duo API returned invalid JSON."

        if isinstance(data, dict):
            for key in ("data", "result", "response"):
                nested = data.get(key)
                if isinstance(nested, dict) and (
                    "PlayerName" in nested or "PlayerUID" in nested or "DuoPartnerName" in nested
                ):
                    data = nested
                    break
        else:
            logger.error("Duo API JSON type was %s, expected object", type(data).__name__)
            return False, None, "Duo API returned an unexpected response format."

        if not data:
            return False, None, "No Duo information found for this UID."

        known_keys = {"PlayerName", "PlayerUID", "DuoPartnerName"}
        if not any(k in data for k in known_keys):
            logger.error("Unexpected Duo API JSON keys: %s", list(data.keys())[:30])
            return False, None, "Duo API returned an unexpected data format."

        API_CACHE[uid] = (now, data)
        return True, data, "live"

    except requests.exceptions.ConnectTimeout:
        logger.error("Duo API connect timeout: %s", url)
        return False, None, "Could not connect to Duo API."
    except requests.exceptions.ReadTimeout:
        logger.error("Duo API read timeout: %s", url)
        return False, None, "Duo API took too long to respond."
    except requests.exceptions.ConnectionError as e:
        logger.error("Duo API connection error: %s", e)
        return False, None, "Could not connect to Duo API."
    except requests.exceptions.RequestException as e:
        logger.error("Duo API request error: %s", e)
        return False, None, "Duo API request failed."
    except Exception as e:
        logger.exception("Unexpected Duo API error: %s", e)
        return False, None, "Unexpected Duo API error."

# --- Duo Processing Core Handler ---
def process_duo_request(user_id: int, chat_id: int, uid: str, loading_msg_id: Optional[int] = None):
    if not uid.isdigit() or len(uid) < 5 or len(uid) > 15:
        msg = f"{config.emoji('warn', '⚠️')} <b>Invalid UID.</b>\nPlease send a valid numeric UID."
        if loading_msg_id:
            edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
        else:
            send_message(chat_id, msg)
        return

    db_u = DB_DATA["users"].get(str(user_id))
    if not db_u:
        return

    if db_u.get("banned", False):
        msg = f"{config.emoji('no', '🚫')} You are currently banned from using this bot."
        if loading_msg_id:
            edit_message(chat_id, loading_msg_id, msg)
        else:
            send_message(chat_id, msg)
        return

    settings = get_db_settings()
    req_cost = settings.get("request_cost", 1)
    daily_free_limit = settings.get("daily_free_requests", 1)

    today = get_today_str()
    if db_u.get("daily_free_date") != today:
        daily_used = 0
    else:
        daily_used = db_u.get("daily_free_used", 0)

    is_free = False
    if daily_used < daily_free_limit:
        is_free = True
    else:
        if db_u.get("coins", 0) < req_cost:
            msg = f"{config.emoji('warn', '⚠️')} <b>Insufficient Coins!</b>\n\nThis request costs <b>{req_cost} Coin(s)</b>.\nYour Balance: <b>{db_u.get('coins', 0)} Coins</b>.\n\nEarn free coins by inviting friends using the Referral option."
            if loading_msg_id:
                edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
            else:
                send_message(chat_id, msg)
            return

    # Call Duo API
    success, data, err_desc = fetch_duo_info(uid)

    if not success:
        DB_DATA["requests"].append({
            "user_id": user_id,
            "uid": uid,
            "chat_id": chat_id,
            "chat_type": "private" if chat_id == user_id else "group",
            "status": "failed",
            "cost": 0,
            "used_free": False,
            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        })
        db_u["total_requests"] = db_u.get("total_requests", 0) + 1
        db_u["failed_requests"] = db_u.get("failed_requests", 0) + 1
        save_db_to_telegram()

        log_to_group(f"{config.emoji('warn', '⚠️')} <b>Duo Request Failed</b>\n\n👤 User: <code>{user_id}</code>\n🎯 UID: <code>{uid}</code>\n❌ Reason: {err_desc}")

        msg = f"{config.emoji('no', '❌')} <b>Request Failed</b>\n\n{err_desc}"
        if loading_msg_id:
            edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
        else:
            send_message(chat_id, msg)
        return

    # Success
    cost_text = "Free"
    if is_free:
        db_u["daily_free_used"] = db_u.get("daily_free_used", 0) + 1
        db_u["total_requests"] = db_u.get("total_requests", 0) + 1
        db_u["successful_requests"] = db_u.get("successful_requests", 0) + 1
        db_u["daily_free_date"] = today
    else:
        db_u["coins"] = db_u.get("coins", 0) - req_cost
        db_u["total_requests"] = db_u.get("total_requests", 0) + 1
        db_u["successful_requests"] = db_u.get("successful_requests", 0) + 1
        cost_text = f"{req_cost} Coin"
        DB_DATA["transactions"].append({
            "user_id": user_id,
            "type": "spend",
            "amount": req_cost,
            "description": f"Duo check for UID {uid}",
            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        })

    DB_DATA["requests"].append({
        "user_id": user_id,
        "uid": uid,
        "chat_id": chat_id,
        "chat_type": "private" if chat_id == user_id else "group",
        "status": "success",
        "cost": 0 if is_free else req_cost,
        "used_free": is_free,
        "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
    })
    save_db_to_telegram()

    p_name = esc(data.get("PlayerName", "N/A"))
    p_uid = esc(data.get("PlayerUID", uid))
    d_name = esc(data.get("DuoPartnerName", "None"))
    d_uid = esc(data.get("DuoPartnerUID", "N/A"))
    d_level = esc(data.get("DuoLevel", "0"))
    d_days = esc(data.get("DuoDays", "0"))
    d_score = esc(data.get("DuoScore", "0"))
    d_status = esc(data.get("DuoStatus", "N/A"))
    d_created = esc(data.get("DuoCreationDate", "N/A"))

    res_msg = (
        f"{config.emoji('target', '🔎')} <b>Duo Information</b>\n\n"
        f"{config.emoji('user', '👤')} <b>Player</b>\n"
        f"Name: <code>{p_name}</code>\n"
        f"UID: <code>{p_uid}</code>\n\n"
        f"{config.emoji('group', '🤝')} <b>Duo Partner</b>\n"
        f"Name: <code>{d_name}</code>\n"
        f"UID: <code>{d_uid}</code>\n\n"
        f"⭐ Duo Level: <b>{d_level}</b>\n"
        f"📅 Duo Days: <b>{d_days}</b>\n"
        f"🏆 Duo Score: <b>{d_score}</b>\n"
        f"🟢 Status: <b>{d_status}</b>\n"
        f"🗓 Created: <b>{d_created}</b>\n\n"
        f"{config.emoji('money', '💰')} Request: <b>{cost_text}</b>"
    )

    if loading_msg_id:
        edit_message(chat_id, loading_msg_id, res_msg, reply_markup=get_back_keyboard())
    else:
        send_message(chat_id, res_msg)

    log_to_group(f"{config.emoji('target', '🔎')} <b>Duo Request Success</b>\n\n👤 User ID: <code>{user_id}</code>\n🎯 UID: <code>{uid}</code>\n📍 Chat ID: <code>{chat_id}</code>\n💰 Cost: <b>{cost_text}</b>")

# --- Command & Interaction Handlers ---
def handle_start(user: dict, chat_id: int, args: str = ""):
    db_u = sync_user(user)
    user_id = user["id"]

    if args.startswith("ref_") and db_u.get("referrer_id") is None:
        try:
            ref_id = int(args.replace("ref_", "").strip())
            ref_user = DB_DATA["users"].get(str(ref_id))
            if ref_id != user_id and ref_user:
                settings = get_db_settings()
                ref_reward = settings.get("referral_reward", 5)

                db_u["referrer_id"] = ref_id
                ref_user["coins"] = ref_user.get("coins", 0) + ref_reward
                ref_user["referral_count"] = ref_user.get("referral_count", 0) + 1
                ref_user["referral_earnings"] = ref_user.get("referral_earnings", 0) + ref_reward

                DB_DATA["referrals"].append({
                    "referrer_id": ref_id,
                    "referred_id": user_id,
                    "reward": ref_reward,
                    "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
                })
                DB_DATA["transactions"].append({
                    "user_id": ref_id,
                    "type": "referral",
                    "amount": ref_reward,
                    "description": f"Referral reward for inviting {user_id}",
                    "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
                })
                save_db_to_telegram()

                send_message(ref_id, f"{config.emoji('gift', '🎁')} <b>New Referral!</b>\n\nSomeone joined using your referral link. You earned <b>{ref_reward} Coins</b>!")
                log_to_group(f"{config.emoji('gift', '🎁')} <b>Referral Success</b>\n\nReferrer: <code>{ref_id}</code>\nReferred: <code>{user_id}</code>\nReward: <b>{ref_reward} Coins</b>")
        except Exception as e:
            logger.error(f"Referral Error: {e}")

    if not check_force_sub(user_id):
        send_message(chat_id, f"{config.emoji('warn', '⚠️')} <b>Please join our official channel to use this bot.</b>", reply_markup=get_force_sub_keyboard())
        return

    # First, send bottom Reply Keyboard (Share Number & Policy)
    send_message(
        chat_id,
        f"🔊 <b>স্বাগতম, {esc(user.get('first_name'))}!</b>\n\n"
        f"<b>Duo Info Bot</b> ব্যবহার শুরু করতে দয়া করে নিচের বাটনে চেপে আপনার ফোন নম্বর শেয়ার করুন।\n\n"
        f"⚠️ <b>আপনার নম্বর শুধুমাত্র অ্যাকাউন্ট ভেরিফিকেশনের জন্য ব্যবহার করা হবে — অন্য কোনো কাজে নয়।</b>",
        reply_markup=get_bottom_reply_keyboard()
    )

    # Then send main inline menu
    welcome_text = (
        f"{config.emoji('hi', '👋')} <b>Welcome to Duo Info Bot</b>\n\n"
        f"{config.emoji('target', '🔎')} Check Duo information instantly\n"
        f"{config.emoji('rocket', '⚡')} Fast API response\n"
        f"{config.emoji('gift', '🎁')} Daily free request\n"
        f"{config.emoji('money', '💰')} Referral rewards\n\n"
        f"Choose an option below."
    )
    send_message(chat_id, welcome_text, reply_markup=get_main_keyboard(user_id))

def handle_callback(callback: dict):
    call_id = callback["id"]
    from_user = callback["from"]
    user_id = from_user["id"]
    message = callback.get("message")
    chat_id = message["chat"]["id"] if message else user_id
    message_id = message["message_id"] if message else None
    data = callback.get("data", "")

    db_u = sync_user(from_user)

    if db_u.get("banned", False):
        answer_callback(call_id, "You are banned from using this bot.", show_alert=True)
        return

    if data == "check_sub":
        if check_force_sub(user_id):
            answer_callback(call_id, "Thank you for joining!")
            if message_id:
                delete_message(chat_id, message_id)
            handle_start(from_user, chat_id)
        else:
            answer_callback(call_id, "You have not joined the channel yet!", show_alert=True)
        return

    if not check_force_sub(user_id):
        answer_callback(call_id, "Please join our channel first!", show_alert=True)
        send_message(chat_id, f"{config.emoji('warn', '⚠️')} <b>Please join our official channel to use this bot.</b>", reply_markup=get_force_sub_keyboard())
        return

    answer_callback(call_id)

    if user_id in USER_STATES and not data.startswith("admin_"):
        USER_STATES.pop(user_id, None)

    if data == "main_menu":
        welcome_text = (
            f"{config.emoji('hi', '👋')} <b>Welcome to Duo Info Bot</b>\n\n"
            f"{config.emoji('target', '🔎')} Check Duo information instantly\n"
            f"{config.emoji('rocket', '⚡')} Fast API response\n"
            f"{config.emoji('gift', '🎁')} Daily free request\n"
            f"{config.emoji('money', '💰')} Referral rewards\n\n"
            f"Choose an option below."
        )
        edit_message(chat_id, message_id, welcome_text, reply_markup=get_main_keyboard(user_id))

    elif data == "check_duo":
        USER_STATES[user_id] = "AWAITING_UID"
        kb = {
            "inline_keyboard": [
                [{"text": f"{config.emoji('no', '❌')} Cancel", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, f"{config.emoji('target', '🎯')} <b>Send the Player UID</b>\n\nPlease type and send the Free Fire / Duo numeric UID below:", reply_markup=kb)

    elif data == "profile":
        t_id = db_u.get("telegram_id")
        u_name = f"@{esc(db_u.get('username'))}" if db_u.get("username") else "None"
        coins = db_u.get("coins", 0)
        refs = db_u.get("referral_count", 0)
        tot_req = db_u.get("total_requests", 0)
        joined = db_u.get("joined_at", "N/A")
        phone = db_u.get("phone_number", "Not Shared")

        settings = get_db_settings()
        daily_limit = settings.get("daily_free_requests", 1)
        daily_used = db_u.get("daily_free_used", 0)
        free_left = max(0, daily_limit - daily_used)

        prof_text = (
            f"{config.emoji('user', '👤')} <b>Your Profile</b>\n\n"
            f"🆔 Telegram ID: <code>{t_id}</code>\n"
            f"👤 Username: {u_name}\n"
            f"📱 Phone: <code>{phone}</code>\n"
            f"{config.emoji('money', '💰')} Coins Balance: <b>{coins}</b>\n"
            f"{config.emoji('gift', '🎁')} Referrals: <b>{refs}</b>\n"
            f"{config.emoji('target', '🔎')} Total Requests: <b>{tot_req}</b>\n"
            f"🆓 Free Requests Today: <b>{free_left} / {daily_limit}</b>\n"
            f"📅 Joined: <b>{joined}</b>"
        )
        edit_message(chat_id, message_id, prof_text, reply_markup=get_back_keyboard())

    elif data == "coins":
        settings = get_db_settings()
        req_c = settings.get("request_cost", 1)
        free_req = settings.get("daily_free_requests", 1)

        coins_text = (
            f"{config.emoji('money', '💰')} <b>Your Coins</b>\n\n"
            f"💎 Balance: <b>{db_u.get('coins', 0)} Coins</b>\n\n"
            f"🆓 Daily Free Requests: <b>{free_req}</b>\n"
            f"{config.emoji('money', '💰')} Request Cost: <b>{req_c} Coin(s)</b>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": f"{config.emoji('gift', '🎁')} Earn Coins", "callback_data": "referral"}],
                [{"text": f"{config.emoji('view', '📊')} Transactions", "callback_data": "transactions_0"}],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, coins_text, reply_markup=kb)

    elif data == "referral":
        bot_info = api_call("getMe")
        bot_uname = bot_info.get("username", "bot") if bot_info else "bot"
        ref_link = f"https://t.me/{bot_uname}?start=ref_{user_id}"

        settings = get_db_settings()
        reward = settings.get("referral_reward", 5)

        ref_text = (
            f"{config.emoji('gift', '🎁')} <b>Referral Program</b>\n\n"
            f"Invite your friends and earn <b>{reward} Coins</b> per referral!\n\n"
            f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
            f"👥 Total Referrals: <b>{db_u.get('referral_count', 0)}</b>\n"
            f"{config.emoji('money', '💰')} Coins Earned: <b>{db_u.get('referral_earnings', 0)}</b>"
        )
        share_url = f"https://t.me/share/url?url={ref_link}&text=Check%20your%20Free%20Fire%20Duo%20Info%20instantly!"
        kb = {
            "inline_keyboard": [
                [{"text": f"{config.emoji('rocket', '📤')} Share Link", "url": share_url}],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, ref_text, reply_markup=kb)

    elif data.startswith("history_"):
        page = int(data.split("_")[1])
        limit = 5
        skip = page * limit

        user_reqs = [r for r in DB_DATA["requests"] if r.get("user_id") == user_id]
        user_reqs.reverse()
        
        total_reqs = len(user_reqs)
        reqs = user_reqs[skip:skip + limit]

        if not reqs:
            edit_message(chat_id, message_id, f"{config.emoji('view', '📊')} <b>Request History</b>\n\nNo request history found.", reply_markup=get_back_keyboard())
            return

        hist_text = f"{config.emoji('view', '📊')} <b>Request History (Page {page + 1})</b>\n\n"
        for r in reqs:
            st = "🟢 Success" if r.get("status") == "success" else "❌ Failed"
            c = "Free" if r.get("used_free") else f"{r.get('cost', 0)} Coin(s)"
            hist_text += (
                f"🔎 UID: <code>{r.get('uid')}</code>\n"
                f"Status: {st} | Cost: <b>{c}</b>\n"
                f"🕒 Time: <code>{r.get('created_at')}</code>\n\n"
            )

        nav_btns = []
        if page > 0:
            nav_btns.append({"text": "⬅️ Previous", "callback_data": f"history_{page - 1}"})
        if skip + limit < total_reqs:
            nav_btns.append({"text": "Next ➡️", "callback_data": f"history_{page + 1}"})

        rows = []
        if nav_btns:
            rows.append(nav_btns)
        rows.append([{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "main_menu"}])

        edit_message(chat_id, message_id, hist_text, reply_markup={"inline_keyboard": rows})

    elif data.startswith("transactions_"):
        page = int(data.split("_")[1])
        limit = 5
        skip = page * limit

        user_txs = [t for t in DB_DATA["transactions"] if t.get("user_id") == user_id]
        user_txs.reverse()

        total_tx = len(user_txs)
        txs = user_txs[skip:skip + limit]

        if not txs:
            edit_message(chat_id, message_id, f"{config.emoji('money', '💰')} <b>Transactions</b>\n\nNo transactions found.", reply_markup=get_back_keyboard("coins"))
            return

        tx_text = f"{config.emoji('money', '💰')} <b>Transaction History (Page {page + 1})</b>\n\n"
        for t in txs:
            t_type = "➕ Earned" if t.get("type") in ["referral", "admin_add"] else "➖ Spent"
            tx_text += (
                f"{t_type}: <b>{t.get('amount')} Coins</b>\n"
                f"Note: {esc(t.get('description'))}\n"
                f"🕒 Time: <code>{t.get('created_at')}</code>\n\n"
            )

        nav_btns = []
        if page > 0:
            nav_btns.append({"text": "⬅️ Previous", "callback_data": f"transactions_{page - 1}"})
        if skip + limit < total_tx:
            nav_btns.append({"text": "Next ➡️", "callback_data": f"transactions_{page + 1}"})

        rows = []
        if nav_btns:
            rows.append(nav_btns)
        rows.append([{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "coins"}])

        edit_message(chat_id, message_id, tx_text, reply_markup={"inline_keyboard": rows})

    elif data == "support":
        settings = get_db_settings()
        sup_url = settings.get("support_url", config.SUPPORT_URL)
        sup_text = f"{config.emoji('msg', '💬')} <b>Support</b>\n\nNeed help or facing issues with the bot? Contact our support team below."
        kb = {
            "inline_keyboard": [
                [{"text": f"{config.emoji('msg', '💬')} Contact Support", "url": sup_url}],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, sup_text, reply_markup=kb)

    elif data.startswith("admin_"):
        if not is_admin(user_id):
            answer_callback(call_id, "❌ Not authorized.", show_alert=True)
            return
        handle_admin_callback(user_id, chat_id, message_id, data, call_id)

# --- Admin Panel Callback Handler ---
def handle_admin_callback(user_id: int, chat_id: int, message_id: int, data: str, call_id: Optional[str] = None):
    if data == "admin_panel":
        admin_text = f"{config.emoji('crown', '👑')} <b>Admin Panel</b>\n\nSelect an administrative option:"
        kb = {
            "inline_keyboard": [
                [
                    {"text": f"{config.emoji('user', '👥')} Users", "callback_data": "admin_users_0"},
                    {"text": f"{config.emoji('view', '📊')} Statistics", "callback_data": "admin_stats"}
                ],
                [
                    {"text": f"{config.emoji('money', '💰')} Coin Settings", "callback_data": "admin_coin_settings"},
                    {"text": f"{config.emoji('gift', '🎁')} Referral Settings", "callback_data": "admin_ref_settings"}
                ],
                [
                    {"text": f"{config.emoji('bell', '📢')} Broadcast", "callback_data": "admin_broadcast_prompt"},
                    {"text": f"{config.emoji('join', '📢')} Force Sub", "callback_data": "admin_forcesub_prompt"}
                ],
                [
                    {"text": f"{config.emoji('rocket', '📤')} Export Users CSV", "callback_data": "admin_export_users"},
                    {"text": f"{config.emoji('rocket', '📤')} Export Requests CSV", "callback_data": "admin_export_reqs"}
                ],
                [
                    {"text": f"{config.emoji('key', '🔐')} Manage Admins", "callback_data": "admin_manage_admins"}
                ],
                [{"text": f"{config.emoji('no', '🔙')} Exit Admin", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, admin_text, reply_markup=kb)

    elif data == "admin_stats":
        tot_users = len(DB_DATA["users"])
        active_users = sum(1 for u in DB_DATA["users"].values() if not u.get("banned"))
        tot_groups = len(DB_DATA["groups"])

        tot_reqs = len(DB_DATA["requests"])
        succ_reqs = sum(1 for r in DB_DATA["requests"] if r.get("status") == "success")
        fail_reqs = sum(1 for r in DB_DATA["requests"] if r.get("status") == "failed")

        coins_dist = sum(t.get("amount", 0) for t in DB_DATA["transactions"] if t.get("type") in ["referral", "admin_add"])
        coins_spent = sum(t.get("amount", 0) for t in DB_DATA["transactions"] if t.get("type") == "spend")
        tot_refs = len(DB_DATA["referrals"])

        stats_text = (
            f"{config.emoji('view', '📊')} <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: <b>{tot_users}</b>\n"
            f"🟢 Active Users: <b>{active_users}</b>\n"
            f"👥 Total Groups: <b>{tot_groups}</b>\n\n"
            f"🔎 Total Requests: <b>{tot_reqs}</b>\n"
            f"✅ Successful: <b>{succ_reqs}</b>\n"
            f"❌ Failed: <b>{fail_reqs}</b>\n\n"
            f"💰 Coins Distributed: <b>{coins_dist}</b>\n"
            f"💰 Coins Spent: <b>{coins_spent}</b>\n"
            f"🎁 Total Referrals: <b>{tot_refs}</b>"
        )
        edit_message(chat_id, message_id, stats_text, reply_markup=get_back_keyboard("admin_panel"))

    elif data.startswith("admin_users_"):
        page = int(data.split("_")[2])
        limit = 5
        skip = page * limit

        all_users = list(DB_DATA["users"].values())
        all_users.reverse()
        tot_u = len(all_users)
        users_list = all_users[skip:skip + limit]

        text = f"{config.emoji('user', '👥')} <b>User Management (Page {page + 1})</b>\n\n"
        kb_rows = []

        for u in users_list:
            u_id = u.get("telegram_id")
            ban_str = " [BANNED]" if u.get("banned") else ""
            text += (
                f"👤 <b>{esc(u.get('first_name'))}</b>{ban_str}\n"
                f"🆔 ID: <code>{u_id}</code> | @{esc(u.get('username'))}\n"
                f"💰 Coins: <b>{u.get('coins', 0)}</b> | 🎁 Refs: <b>{u.get('referral_count', 0)}</b>\n\n"
            )
            kb_rows.append([{"text": f"Manage {u_id}", "callback_data": f"admin_manage_u_{u_id}"}])

        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Prev", "callback_data": f"admin_users_{page - 1}"})
        if skip + limit < tot_u:
            nav.append({"text": "Next ➡️", "callback_data": f"admin_users_{page + 1}"})
        if nav:
            kb_rows.append(nav)

        kb_rows.append([{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": kb_rows})

    elif data.startswith("admin_manage_u_"):
        target_uid = int(data.split("_")[3])
        u = DB_DATA["users"].get(str(target_uid))
        if not u:
            edit_message(chat_id, message_id, "User not found.", reply_markup=get_back_keyboard("admin_users_0"))
            return

        ban_btn = (
            {"text": f"{config.emoji('ok', '✅')} Unban User", "callback_data": f"admin_unban_{target_uid}"}
            if u.get("banned")
            else {"text": f"{config.emoji('no', '🚫')} Ban User", "callback_data": f"admin_ban_{target_uid}"}
        )

        u_text = (
            f"👤 <b>User Management</b>\n\n"
            f"Name: <b>{esc(u.get('first_name'))}</b>\n"
            f"🆔 Telegram ID: <code>{u.get('telegram_id')}</code>\n"
            f"🔗 Username: @{esc(u.get('username'))}\n"
            f"📱 Phone: <code>{u.get('phone_number', 'N/A')}</code>\n"
            f"💰 Coins: <b>{u.get('coins', 0)}</b>\n"
            f"🎁 Referrals: <b>{u.get('referral_count', 0)}</b>\n"
            f"🔎 Requests: <b>{u.get('total_requests', 0)}</b>\n"
            f"Status: <b>{'Banned' if u.get('banned') else 'Active'}</b>\n"
            f"📅 Joined: <b>{u.get('joined_at')}</b>"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": f"{config.emoji('add', '➕')} Add Coins", "callback_data": f"admin_addcoins_{target_uid}"},
                    {"text": f"{config.emoji('rem', '➖')} Remove Coins", "callback_data": f"admin_remcoins_{target_uid}"}
                ],
                [ban_btn],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "admin_users_0"}]
            ]
        }
        edit_message(chat_id, message_id, u_text, reply_markup=kb)

    elif data.startswith("admin_ban_"):
        target_uid = int(data.split("_")[2])
        if str(target_uid) in DB_DATA["users"]:
            DB_DATA["users"][str(target_uid)]["banned"] = True
            save_db_to_telegram()
        handle_admin_callback(user_id, chat_id, message_id, f"admin_manage_u_{target_uid}")

    elif data.startswith("admin_unban_"):
        target_uid = int(data.split("_")[2])
        if str(target_uid) in DB_DATA["users"]:
            DB_DATA["users"][str(target_uid)]["banned"] = False
            save_db_to_telegram()
        handle_admin_callback(user_id, chat_id, message_id, f"admin_manage_u_{target_uid}")

    elif data.startswith("admin_addcoins_"):
        target_uid = int(data.split("_")[2])
        USER_STATES[user_id] = f"AWAITING_ADD_COINS_{target_uid}"
        edit_message(chat_id, message_id, f"Send the number of coins to <b>ADD</b> to user <code>{target_uid}</code>:", reply_markup=get_back_keyboard(f"admin_manage_u_{target_uid}"))

    elif data.startswith("admin_remcoins_"):
        target_uid = int(data.split("_")[2])
        USER_STATES[user_id] = f"AWAITING_REM_COINS_{target_uid}"
        edit_message(chat_id, message_id, f"Send the number of coins to <b>REMOVE</b> from user <code>{target_uid}</code>:", reply_markup=get_back_keyboard(f"admin_manage_u_{target_uid}"))

    elif data == "admin_coin_settings":
        settings = get_db_settings()
        cost = settings.get("request_cost", 1)
        free_req = settings.get("daily_free_requests", 1)

        text = (
            f"{config.emoji('money', '💰')} <b>Coin Settings</b>\n\n"
            f"💰 Current Request Cost: <b>{cost} Coin(s)</b>\n"
            f"🆓 Daily Free Requests: <b>{free_req}</b>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "Change Request Cost", "callback_data": "admin_set_cost"}],
                [{"text": "Change Daily Free Limit", "callback_data": "admin_set_free"}],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "admin_panel"}]
            ]
        }
        edit_message(chat_id, message_id, text, reply_markup=kb)

    elif data == "admin_set_cost":
        USER_STATES[user_id] = "AWAITING_NEW_COST"
        edit_message(chat_id, message_id, "Send the new request cost (integer):", reply_markup=get_back_keyboard("admin_coin_settings"))

    elif data == "admin_set_free":
        USER_STATES[user_id] = "AWAITING_NEW_FREE"
        edit_message(chat_id, message_id, "Send the new daily free requests limit (integer):", reply_markup=get_back_keyboard("admin_coin_settings"))

    elif data == "admin_ref_settings":
        settings = get_db_settings()
        reward = settings.get("referral_reward", 5)
        text = f"{config.emoji('gift', '🎁')} <b>Referral Settings</b>\n\nCurrent Referral Reward: <b>{reward} Coins</b>"
        kb = {
            "inline_keyboard": [
                [{"text": "Change Reward Amount", "callback_data": "admin_set_ref_reward"}],
                [{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "admin_panel"}]
            ]
        }
        edit_message(chat_id, message_id, text, reply_markup=kb)

    elif data == "admin_set_ref_reward":
        USER_STATES[user_id] = "AWAITING_NEW_REF_REWARD"
        edit_message(chat_id, message_id, "Send the new referral reward amount (integer):", reply_markup=get_back_keyboard("admin_ref_settings"))

    elif data == "admin_broadcast_prompt":
        USER_STATES[user_id] = "AWAITING_BROADCAST_TEXT"
        edit_message(chat_id, message_id, f"{config.emoji('bell', '📢')} Send the message you want to broadcast to all users:", reply_markup=get_back_keyboard("admin_panel"))

    elif data == "admin_forcesub_prompt":
        settings = get_db_settings()
        curr = settings.get("force_sub_channel", "None")
        USER_STATES[user_id] = "AWAITING_FORCESUB_CHANNEL"
        edit_message(chat_id, message_id, f"Current Force Sub Channel: <code>{esc(curr)}</code>\n\nSend new channel username (e.g. <code>@mychannel</code>) or send <code>off</code> to disable:", reply_markup=get_back_keyboard("admin_panel"))

    elif data == "admin_export_users":
        edit_message(chat_id, message_id, "Generating Users CSV...")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Telegram ID", "Username", "First Name", "Phone Number", "Coins", "Referrals", "Total Requests", "Joined At"])
        for u in DB_DATA["users"].values():
            writer.writerow([u.get("telegram_id"), u.get("username"), u.get("first_name"), u.get("phone_number", "N/A"), u.get("coins"), u.get("referral_count"), u.get("total_requests"), u.get("joined_at")])

        send_document(chat_id, output.getvalue().encode('utf-8'), "users.csv", caption="Users Export")

    elif data == "admin_export_reqs":
        edit_message(chat_id, message_id, "Generating Requests CSV...")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "UID", "Status", "Cost", "Used Free", "Created At"])
        for r in DB_DATA["requests"]:
            writer.writerow([r.get("user_id"), r.get("uid"), r.get("status"), r.get("cost"), r.get("used_free"), r.get("created_at")])

        send_document(chat_id, output.getvalue().encode('utf-8'), "requests.csv", caption="Requests Export")

    elif data == "admin_manage_admins":
        if str(user_id) != str(config.ADMIN_ID):
            return
        
        admins = DB_DATA.get("admins", [])
        admin_text = f"{config.emoji('key', '🔐')} <b>Admin Management</b>\n\n<b>Owner ID:</b> <code>{config.ADMIN_ID}</code>\n\n<b>Secondary Admins:</b>\n"
        kb_rows = []
        for aid in admins:
            admin_text += f"• <code>{aid}</code>\n"
            kb_rows.append([{"text": f"Remove {aid}", "callback_data": f"admin_remove_admin_{aid}"}])

        kb_rows.append([{"text": f"{config.emoji('add', '➕')} Add Admin", "callback_data": "admin_add_admin_prompt"}])
        kb_rows.append([{"text": f"{config.emoji('no', '🔙')} Back", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, admin_text, reply_markup={"inline_keyboard": kb_rows})

    elif data == "admin_add_admin_prompt":
        if str(user_id) != str(config.ADMIN_ID):
            return
        USER_STATES[user_id] = "AWAITING_ADD_ADMIN_ID"
        edit_message(chat_id, message_id, "Send the Telegram User ID to make Admin:", reply_markup=get_back_keyboard("admin_manage_admins"))

    elif data.startswith("admin_remove_admin_"):
        if str(user_id) != str(config.ADMIN_ID):
            return
        aid = int(data.split("_")[3])
        if aid in DB_DATA.get("admins", []):
            DB_DATA["admins"].remove(aid)
            save_db_to_telegram()
        handle_admin_callback(user_id, chat_id, message_id, "admin_manage_admins")

# --- Message Processing Engine ---
def handle_text_message(msg: dict):
    from_user = msg["from"]
    user_id = from_user["id"]
    chat = msg["chat"]
    chat_id = chat["id"]
    chat_type = chat["type"]

    # Track Groups
    if chat_type in ["group", "supergroup"]:
        DB_DATA["groups"][str(chat_id)] = {
            "title": chat.get("title", "Group"),
            "updated_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_db_to_telegram()

    db_u = sync_user(from_user)

    if db_u.get("banned", False) and chat_type == "private":
        send_message(chat_id, f"{config.emoji('no', '🚫')} You are currently banned from using this bot.")
        return

    # Handle Contact Share Event
    if "contact" in msg:
        contact_info = msg["contact"]
        phone_number = contact_info.get("phone_number")
        
        db_u["phone_number"] = phone_number
        save_db_to_telegram()

        send_message(
            chat_id, 
            f"✅ <b>ধন্যবাদ!</b> আপনার নম্বর (<code>{phone_number}</code>) সফলভাবে ভেরিফাই করা হয়েছে।"
        )
        log_to_group(f"📱 <b>Contact Shared</b>\n\n👤 User: <code>{user_id}</code>\n📞 Phone: <code>{phone_number}</code>")
        return

    text = msg.get("text", "").strip()

    # Handle Policy Button Text Click
    if "চুক্তিপত্র" in text or "Policy" in text:
        policy_text = (
            "📜 <b>আমাদের নীতি ও চুক্তিপত্র (Terms & Policy)</b>\n\n"
            "১. আপনার ফোন নম্বর ও অ্যাকাউন্ট তথ্য সম্পূর্ণ সুরক্ষিত রাখা হয়।\n"
            "২. অনিয়ম বা বটের অপব্যবহার করলে অ্যাকাউন্ট স্থায়ীভাবে ব্যান করা হতে পারে।\n"
            "৩. বটের মাধ্যমে প্রাপ্ত তথ্যের সঠিক ব্যবহারের দায়ভার ব্যবহারকারীর।"
        )
        send_message(chat_id, policy_text)
        return

    # Handle Group Commands
    if chat_type in ["group", "supergroup"]:
        if text.startswith("/duo"):
            parts = text.split()
            if len(parts) < 2:
                send_message(chat_id, f"{config.emoji('warn', '⚠️')} Usage: <code>/duo UID</code>")
                return
            uid = parts[1].strip()
            process_duo_request(user_id, chat_id, uid)
        elif text == "/id":
            send_message(chat_id, f"🆔 Group Chat ID: <code>{chat_id}</code>")
        return

    # --- Private Chat Interactions ---
    now = time.time()
    last_req = RATE_LIMITS.get(user_id, 0)
    if now - last_req < 2.0:
        send_message(chat_id, f"{config.emoji('wait', '⏳')} Please wait a moment before sending another request.")
        return
    RATE_LIMITS[user_id] = now

    if text.startswith("/start"):
        parts = text.split()
        args = parts[1] if len(parts) > 1 else ""
        handle_start(from_user, chat_id, args)
        return
    elif text == "/help":
        send_message(chat_id, f"{config.emoji('target', '🔎')} Use the buttons below or type <code>/duo UID</code> to check duo info.")
        return
    elif text == "/id":
        send_message(chat_id, f"🆔 Your Telegram ID: <code>{user_id}</code>")
        return
    elif text == "/admin" and is_admin(user_id):
        m = send_message(chat_id, "Loading Admin Panel...")
        if m:
            handle_admin_callback(user_id, chat_id, m["message_id"], "admin_panel")
        return

    state = USER_STATES.get(user_id)

    if state == "AWAITING_UID":
        USER_STATES.pop(user_id, None)
        loading = send_message(chat_id, f"{config.emoji('wait', '⏳')} Checking Duo information...")
        loading_id = loading["message_id"] if loading else None
        process_duo_request(user_id, chat_id, text, loading_id)
        return

    # --- Admin Interactive States ---
    if is_admin(user_id) and state:
        if state.startswith("AWAITING_ADD_COINS_"):
            target_uid = int(state.split("_")[3])
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric coin amount.")
                return
            amt = int(text)
            
            target_u = DB_DATA["users"].get(str(target_uid))
            if target_u:
                target_u["coins"] = target_u.get("coins", 0) + amt
                DB_DATA["transactions"].append({
                    "user_id": target_uid,
                    "type": "admin_add",
                    "amount": amt,
                    "description": f"Added by admin {user_id}",
                    "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
                })
                save_db_to_telegram()
            send_message(chat_id, f"Successfully added <b>{amt} Coins</b> to user <code>{target_uid}</code>.")

        elif state.startswith("AWAITING_REM_COINS_"):
            target_uid = int(state.split("_")[3])
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric coin amount.")
                return
            amt = int(text)
            
            target_u = DB_DATA["users"].get(str(target_uid))
            if target_u:
                target_u["coins"] = max(0, target_u.get("coins", 0) - amt)
                save_db_to_telegram()
            send_message(chat_id, f"Successfully removed <b>{amt} Coins</b> from user <code>{target_uid}</code>.")

        elif state == "AWAITING_NEW_COST":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            DB_DATA["settings"]["request_cost"] = int(text)
            save_db_to_telegram()
            send_message(chat_id, f"Request cost updated to <b>{text} Coins</b>.")

        elif state == "AWAITING_NEW_FREE":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            DB_DATA["settings"]["daily_free_requests"] = int(text)
            save_db_to_telegram()
            send_message(chat_id, f"Daily free requests limit updated to <b>{text}</b>.")

        elif state == "AWAITING_NEW_REF_REWARD":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            DB_DATA["settings"]["referral_reward"] = int(text)
            save_db_to_telegram()
            send_message(chat_id, f"Referral reward updated to <b>{text} Coins</b>.")

        elif state == "AWAITING_FORCESUB_CHANNEL":
            USER_STATES.pop(user_id, None)
            val = "" if text.lower() == "off" else text
            DB_DATA["settings"]["force_sub_channel"] = val
            save_db_to_telegram()
            send_message(chat_id, f"Force sub channel updated to: <code>{val if val else 'Disabled'}</code>")

        elif state == "AWAITING_ADD_ADMIN_ID":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric Telegram ID.")
                return
            aid = int(text)
            if aid not in DB_DATA.get("admins", []):
                DB_DATA.setdefault("admins", []).append(aid)
                save_db_to_telegram()
            send_message(chat_id, f"User <code>{aid}</code> has been added as Admin.")

        elif state == "AWAITING_BROADCAST_TEXT":
            USER_STATES.pop(user_id, None)
            STATE_DATA[user_id] = {"broadcast_text": text}
            USER_STATES[user_id] = "CONFIRM_BROADCAST"
            kb = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Confirm Send", "callback_data": "admin_confirm_broadcast"},
                        {"text": "❌ Cancel", "callback_data": "admin_panel"}
                    ]
                ]
            }
            send_message(chat_id, f"<b>Broadcast Preview:</b>\n\n{esc(text)}\n\nAre you sure you want to send this to all users?", reply_markup=kb)
        return

    # Default Fallback for direct text in private chat
    if text.isdigit():
        loading = send_message(chat_id, f"{config.emoji('wait', '⏳')} Checking Duo information...")
        loading_id = loading["message_id"] if loading else None
        process_duo_request(user_id, chat_id, text, loading_id)
    else:
        send_message(chat_id, f"{config.emoji('target', '🔎')} Send a numeric UID or choose an option from /start menu.")

# Special Admin Confirmation Callback Handler
def handle_admin_broadcast_confirm(user_id: int, chat_id: int):
    if not is_admin(user_id):
        send_message(chat_id, "❌ Not authorized.")
        return

    data_dict = STATE_DATA.get(user_id, {})
    b_text = data_dict.get("broadcast_text")
    if not b_text:
        send_message(chat_id, "Broadcast session expired.")
        return

    USER_STATES.pop(user_id, None)
    STATE_DATA.pop(user_id, None)

    send_message(chat_id, "🚀 Starting broadcast...")
    users = list(DB_DATA["users"].values())

    success = 0
    failed = 0

    for u in users:
        target_id = u["telegram_id"]
        res = send_message(target_id, esc(b_text))
        if res:
            success += 1
        else:
            failed += 1
        time.sleep(0.05)

    send_message(chat_id, f"{config.emoji('ok', '✅')} <b>Broadcast Completed!</b>\n\n✅ Sent: <b>{success}</b>\n❌ Failed: <b>{failed}</b>")

# --- Telegram Polling Engine ---
def start_polling():
    webhook_result = api_call("deleteWebhook", {"drop_pending_updates": True})
    if webhook_result is None:
        logger.warning(
            "deleteWebhook did not complete successfully. Polling will still be attempted."
        )

    logger.info("Previous Webhooks cleared. Starting Telegram Long Polling Loop...")

    offset = 0
    conflict_logged = False

    while True:
        try:
            updates = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 25},
                raise_on_conflict=True
            )

            if updates is None:
                time.sleep(2)
                continue

            if not isinstance(updates, list):
                time.sleep(1)
                continue

            conflict_logged = False

            for update in updates:
                try:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1

                    if "message" in update:
                        handle_text_message(update["message"])

                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        if cb.get("data") == "admin_confirm_broadcast":
                            answer_callback(cb["id"])
                            handle_admin_broadcast_confirm(
                                cb["from"]["id"],
                                cb.get("message", {}).get(
                                    "chat", {}
                                ).get("id", cb["from"]["id"])
                            )
                        else:
                            handle_callback(cb)

                except Exception as exc:
                    logger.exception(f"Error processing Telegram update: {exc}")

        except TelegramConflictError as exc:
            if not conflict_logged:
                logger.error(
                    "Telegram 409 Conflict: another process/service is already "
                    "calling getUpdates with this BOT_TOKEN. Stop every other "
                    "running instance."
                )
                logger.error(f"Telegram says: {exc}")
                conflict_logged = True

            time.sleep(10)

        except KeyboardInterrupt:
            logger.info("Polling stopped by KeyboardInterrupt.")
            break

        except Exception as exc:
            logger.exception(f"Error in Long Polling Loop: {exc}")
            time.sleep(3)


# --- Entry Point ---
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask Web Server started on port {config.PORT}")

    start_polling()
