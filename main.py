import csv
import html
import io
import json
import logging
import math
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from flask import Flask, jsonify
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

import config

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Config Validation ---
config.validate_config()

# --- Global In-Memory Caches & States ---
USER_STATES: Dict[int, str] = {}
STATE_DATA: Dict[int, Dict[str, Any]] = {}
RATE_LIMITS: Dict[int, float] = {}
API_CACHE: Dict[str, Tuple[float, dict]] = {}  # uid -> (timestamp, response_data)

# --- Flask Server for Render Health Check ---
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return "Duo Bot is running", 200

def run_flask():
    app.run(host="0.0.0.0", port=config.PORT)

# --- Database Setup (MongoDB Atlas) ---
try:
    mongo_client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[config.MONGO_DB_NAME]
    
    # Setup Collections
    db_users = db["users"]
    db_requests = db["requests"]
    db_referrals = db["referrals"]
    db_settings = db["settings"]
    db_admins = db["admins"]
    db_groups = db["groups"]
    db_transactions = db["transactions"]

    # Setup Indexes
    db_users.create_index([("telegram_id", ASCENDING)], unique=True)
    db_requests.create_index([("user_id", ASCENDING)])
    db_requests.create_index([("uid", ASCENDING)])
    db_referrals.create_index([("referrer_id", ASCENDING)])
    db_referrals.create_index([("referred_id", ASCENDING)], unique=True)
    db_groups.create_index([("chat_id", ASCENDING)], unique=True)
    db_admins.create_index([("telegram_id", ASCENDING)], unique=True)

    # Initialize Global Settings in DB if missing
    if not db_settings.find_one({"key": "global_config"}):
        db_settings.insert_one({
            "key": "global_config",
            "request_cost": config.REQUEST_COST,
            "daily_free_requests": config.DAILY_FREE_REQUESTS,
            "referral_reward": config.REFERRAL_REWARD,
            "force_sub_channel": config.FORCE_SUB_CHANNEL,
            "support_url": config.SUPPORT_URL
        })

    logger.info("Successfully connected to MongoDB Atlas and verified indexes.")
except Exception as e:
    logger.critical(f"Failed to connect to MongoDB Atlas: {e}")
    sys.exit(1)

# --- Dynamic Settings Helper ---
def get_db_settings() -> dict:
    conf = db_settings.find_one({"key": "global_config"})
    if not conf:
        return {
            "request_cost": config.REQUEST_COST,
            "daily_free_requests": config.DAILY_FREE_REQUESTS,
            "referral_reward": config.REFERRAL_REWARD,
            "force_sub_channel": config.FORCE_SUB_CHANNEL,
            "support_url": config.SUPPORT_URL
        }
    return conf

# --- Telegram API Client ---
session = requests.Session()
BASE_URL = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

def api_call(method: str, payload: dict = None, files: dict = None) -> Optional[dict]:
    url = f"{BASE_URL}/{method}"
    try:
        response = session.post(url, data=payload, files=files, timeout=20)
        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 3)
            logger.warning(f"Telegram Rate Limit (429). Sleeping for {retry_after}s...")
            time.sleep(retry_after)
            return api_call(method, payload, files)
        
        res_json = response.json()
        if not res_json.get("ok"):
            logger.error(f"Telegram API Error [{method}]: {res_json.get('description')}")
            return None
        return res_json.get("result")
    except Exception as e:
        logger.error(f"HTTP Error calling Telegram API [{method}]: {e}")
        return None

def json_dumps(data: Any) -> str:
    return json.dumps(data)

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
    files = {"document": (filename, file_data, "text/csv")}
    payload = {"chat_id": chat_id, "caption": caption}
    return api_call("sendDocument", payload, files=files)

def esc(text: Any) -> str:
    if text is None:
        return ""
    return html.escape(str(text))

# --- Date/Time Helpers ---
def get_local_now() -> datetime:
    tz = pytz.timezone(config.TIMEZONE_STR)
    return datetime.now(tz)

def get_today_str() -> str:
    return get_local_now().strftime("%Y-%m-%d")

# --- Access Control & DB User Sync ---
def is_admin(user_id: int) -> bool:
    if user_id in getattr(config, 'ADMIN_IDS', []):
        return True
    if str(user_id) == str(getattr(config, 'ADMIN_ID', None)):
        return True
    return bool(db_admins.find_one({"telegram_id": user_id}))

def sync_user(user: dict) -> dict:
    user_id = user["id"]
    today = get_today_str()
    existing = db_users.find_one({"telegram_id": user_id})
    
    if not existing:
        new_user = {
            "telegram_id": user_id,
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
            "joined_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        }
        db_users.insert_one(new_user)
        return new_user

    # Reset daily free requests if new date
    if existing.get("daily_free_date") != today:
        db_users.update_one(
            {"telegram_id": user_id},
            {"$set": {"daily_free_date": today, "daily_free_used": 0}}
        )
        existing["daily_free_date"] = today
        existing["daily_free_used"] = 0

    # Sync username/first_name changes
    if existing.get("username") != user.get("username", "") or existing.get("first_name") != user.get("first_name", ""):
        db_users.update_one(
            {"telegram_id": user_id},
            {"$set": {"username": user.get("username", ""), "first_name": user.get("first_name", "")}}
        )

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
            [{"text": "📢 Join Channel", "url": url}],
            [{"text": "✅ I've Joined", "callback_data": "check_sub"}]
        ]
    }

# --- Button UI Builder ---
def get_main_keyboard(user_id: int) -> dict:
    rows = [
        [
            {"text": "🔎 Check Duo", "callback_data": "check_duo"},
            {"text": "💰 Coins", "callback_data": "coins"}
        ],
        [
            {"text": "🎁 Referral", "callback_data": "referral"},
            {"text": "👤 Profile", "callback_data": "profile"}
        ],
        [
            {"text": "📊 History", "callback_data": "history_0"},
            {"text": "💬 Support", "callback_data": "support"}
        ]
    ]
    if is_admin(user_id):
        rows.append([{"text": "👑 Admin Panel", "callback_data": "admin_panel"}])
    return {"inline_keyboard": rows}

def get_back_keyboard(target: str = "main_menu") -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔙 Back", "callback_data": target}]
        ]
    }

# --- Logging Group Helper ---
def log_to_group(text: str):
    if config.LOG_GROUP_ID:
        send_message(int(config.LOG_GROUP_ID), text)

# --- Core Business Logic: Duo API ---
def fetch_duo_info(uid: str) -> Tuple[bool, Optional[dict], str]:
    # Check cache (30s TTL)
    now = time.time()
    if uid in API_CACHE:
        cache_time, cache_data = API_CACHE[uid]
        if now - cache_time < 30:
            return True, cache_data, "cache"

    try:
        resp = requests.get(config.DUO_API_URL, params={"uid": uid}, timeout=15)
        if resp.status_code != 200:
            return False, None, "API returns error or is offline."
        
        data = resp.json()
        if not data or "PlayerName" not in data or not data.get("PlayerName"):
            return False, None, "No Duo information found for this UID."

        API_CACHE[uid] = (now, data)
        return True, data, "live"
    except requests.exceptions.Timeout:
        return False, None, "Duo API request timed out."
    except Exception as e:
        logger.error(f"Duo API Request Exception: {e}")
        return False, None, "Failed to connect to Duo API."

# --- Duo Processing Core Handler ---
def process_duo_request(user_id: int, chat_id: int, uid: str, loading_msg_id: Optional[int] = None):
    if not uid.isdigit() or len(uid) < 5 or len(uid) > 15:
        msg = "⚠️ <b>Invalid UID.</b>\nPlease send a valid numeric UID."
        if loading_msg_id:
            edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
        else:
            send_message(chat_id, msg)
        return

    db_u = db_users.find_one({"telegram_id": user_id})
    if not db_u:
        return

    if db_u.get("banned", False):
        msg = "🚫 You are currently banned from using this bot."
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
            msg = f"⚠️ <b>Insufficient Coins!</b>\n\nThis request costs <b>{req_cost} Coin(s)</b>.\nYour Balance: <b>{db_u.get('coins', 0)} Coins</b>.\n\nEarn free coins by inviting friends using the Referral option."
            if loading_msg_id:
                edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
            else:
                send_message(chat_id, msg)
            return

    # Call Duo API
    success, data, err_desc = fetch_duo_info(uid)

    if not success:
        db_requests.insert_one({
            "user_id": user_id,
            "uid": uid,
            "chat_id": chat_id,
            "chat_type": "private" if chat_id == user_id else "group",
            "status": "failed",
            "cost": 0,
            "used_free": False,
            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        })
        db_users.update_one({"telegram_id": user_id}, {"$inc": {"total_requests": 1, "failed_requests": 1}})

        log_to_group(f"⚠️ <b>Duo Request Failed</b>\n\n👤 User: <code>{user_id}</code>\n🎯 UID: <code>{uid}</code>\n❌ Reason: {err_desc}")

        msg = f"❌ <b>Request Failed</b>\n\n{err_desc}"
        if loading_msg_id:
            edit_message(chat_id, loading_msg_id, msg, reply_markup=get_back_keyboard())
        else:
            send_message(chat_id, msg)
        return

    # Success: Deduct payment
    cost_text = "Free"
    if is_free:
        db_users.update_one(
            {"telegram_id": user_id},
            {"$inc": {"daily_free_used": 1, "total_requests": 1, "successful_requests": 1}, "$set": {"daily_free_date": today}}
        )
    else:
        db_users.update_one(
            {"telegram_id": user_id},
            {"$inc": {"coins": -req_cost, "total_requests": 1, "successful_requests": 1}}
        )
        cost_text = f"{req_cost} Coin"
        db_transactions.insert_one({
            "user_id": user_id,
            "type": "spend",
            "amount": req_cost,
            "description": f"Duo check for UID {uid}",
            "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
        })

    db_requests.insert_one({
        "user_id": user_id,
        "uid": uid,
        "chat_id": chat_id,
        "chat_type": "private" if chat_id == user_id else "group",
        "status": "success",
        "cost": 0 if is_free else req_cost,
        "used_free": is_free,
        "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
    })

    # Format Output Result
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
        f"🔎 <b>Duo Information</b>\n\n"
        f"👤 <b>Player</b>\n"
        f"Name: <code>{p_name}</code>\n"
        f"UID: <code>{p_uid}</code>\n\n"
        f"🤝 <b>Duo Partner</b>\n"
        f"Name: <code>{d_name}</code>\n"
        f"UID: <code>{d_uid}</code>\n\n"
        f"⭐ Duo Level: <b>{d_level}</b>\n"
        f"📅 Duo Days: <b>{d_days}</b>\n"
        f"🏆 Duo Score: <b>{d_score}</b>\n"
        f"🟢 Status: <b>{d_status}</b>\n"
        f"🗓 Created: <b>{d_created}</b>\n\n"
        f"💰 Request: <b>{cost_text}</b>"
    )

    if loading_msg_id:
        edit_message(chat_id, loading_msg_id, res_msg, reply_markup=get_back_keyboard())
    else:
        send_message(chat_id, res_msg)

    log_to_group(f"🔎 <b>Duo Request Success</b>\n\n👤 User ID: <code>{user_id}</code>\n🎯 UID: <code>{uid}</code>\n📍 Chat ID: <code>{chat_id}</code>\n💰 Cost: <b>{cost_text}</b>")

# --- Command & Interaction Handlers ---
def handle_start(user: dict, chat_id: int, args: str = ""):
    db_u = sync_user(user)
    user_id = user["id"]

    # Check Referral Parameter
    if args.startswith("ref_") and db_u.get("referrer_id") is None:
        try:
            ref_id = int(args.replace("ref_", "").strip())
            if ref_id != user_id and db_users.find_one({"telegram_id": ref_id}):
                settings = get_db_settings()
                ref_reward = settings.get("referral_reward", 5)

                db_users.update_one({"telegram_id": user_id}, {"$set": {"referrer_id": ref_id}})
                db_users.update_one(
                    {"telegram_id": ref_id},
                    {"$inc": {"coins": ref_reward, "referral_count": 1, "referral_earnings": ref_reward}}
                )
                db_referrals.insert_one({
                    "referrer_id": ref_id,
                    "referred_id": user_id,
                    "reward": ref_reward,
                    "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
                })
                db_transactions.insert_one({
                    "user_id": ref_id,
                    "type": "referral",
                    "amount": ref_reward,
                    "description": f"Referral reward for inviting {user_id}",
                    "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
                })
                
                send_message(ref_id, f"🎁 <b>New Referral!</b>\n\nSomeone joined using your referral link. You earned <b>{ref_reward} Coins</b>!")
                log_to_group(f"🎁 <b>Referral Success</b>\n\nReferrer: <code>{ref_id}</code>\nReferred: <code>{user_id}</code>\nReward: <b>{ref_reward} Coins</b>")
        except Exception as e:
            logger.error(f"Referral Error: {e}")

    if not check_force_sub(user_id):
        send_message(chat_id, "⚠️ <b>Please join our official channel to use this bot.</b>", reply_markup=get_force_sub_keyboard())
        return

    welcome_text = (
        f"👋 <b>Welcome to Duo Info Bot</b>\n\n"
        f"🔎 Check Duo information instantly\n"
        f"⚡ Fast API response\n"
        f"🎁 Daily free request\n"
        f"💰 Referral rewards\n\n"
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
        send_message(chat_id, "⚠️ <b>Please join our official channel to use this bot.</b>", reply_markup=get_force_sub_keyboard())
        return

    answer_callback(call_id)

    if user_id in USER_STATES and not data.startswith("admin_"):
        USER_STATES.pop(user_id, None)

    if data == "main_menu":
        welcome_text = (
            f"👋 <b>Welcome to Duo Info Bot</b>\n\n"
            f"🔎 Check Duo information instantly\n"
            f"⚡ Fast API response\n"
            f"🎁 Daily free request\n"
            f"💰 Referral rewards\n\n"
            f"Choose an option below."
        )
        edit_message(chat_id, message_id, welcome_text, reply_markup=get_main_keyboard(user_id))

    elif data == "check_duo":
        USER_STATES[user_id] = "AWAITING_UID"
        kb = {
            "inline_keyboard": [
                [{"text": "❌ Cancel", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, "🎯 <b>Send the Player UID</b>\n\nPlease type and send the Free Fire / Duo numeric UID below:", reply_markup=kb)

    elif data == "profile":
        t_id = db_u.get("telegram_id")
        u_name = f"@{db_u.get('username')}" if db_u.get("username") else "None"
        coins = db_u.get("coins", 0)
        refs = db_u.get("referral_count", 0)
        tot_req = db_u.get("total_requests", 0)
        joined = db_u.get("joined_at", "N/A")

        settings = get_db_settings()
        daily_limit = settings.get("daily_free_requests", 1)
        daily_used = db_u.get("daily_free_used", 0)
        free_left = max(0, daily_limit - daily_used)

        prof_text = (
            f"👤 <b>Your Profile</b>\n\n"
            f"🆔 Telegram ID: <code>{t_id}</code>\n"
            f"👤 Username: {u_name}\n"
            f"💰 Coins Balance: <b>{coins}</b>\n"
            f"🎁 Referrals: <b>{refs}</b>\n"
            f"🔎 Total Requests: <b>{tot_req}</b>\n"
            f"🆓 Free Requests Today: <b>{free_left} / {daily_limit}</b>\n"
            f"📅 Joined: <b>{joined}</b>"
        )
        edit_message(chat_id, message_id, prof_text, reply_markup=get_back_keyboard())

    elif data == "coins":
        settings = get_db_settings()
        req_c = settings.get("request_cost", 1)
        free_req = settings.get("daily_free_requests", 1)

        coins_text = (
            f"💰 <b>Your Coins</b>\n\n"
            f"💎 Balance: <b>{db_u.get('coins', 0)} Coins</b>\n\n"
            f"🆓 Daily Free Requests: <b>{free_req}</b>\n"
            f"💰 Request Cost: <b>{req_c} Coin(s)</b>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "🎁 Earn Coins", "callback_data": "referral"}],
                [{"text": "📊 Transactions", "callback_data": "transactions_0"}],
                [{"text": "🔙 Back", "callback_data": "main_menu"}]
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
            f"🎁 <b>Referral Program</b>\n\n"
            f"Invite your friends and earn <b>{reward} Coins</b> per referral!\n\n"
            f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
            f"👥 Total Referrals: <b>{db_u.get('referral_count', 0)}</b>\n"
            f"💰 Coins Earned: <b>{db_u.get('referral_earnings', 0)}</b>"
        )
        share_url = f"https://t.me/share/url?url={ref_link}&text=Check%20your%20Free%20Fire%20Duo%20Info%20instantly!"
        kb = {
            "inline_keyboard": [
                [{"text": "📤 Share Link", "url": share_url}],
                [{"text": "🔙 Back", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, ref_text, reply_markup=kb)

    elif data.startswith("history_"):
        page = int(data.split("_")[1])
        limit = 5
        skip = page * limit

        total_reqs = db_requests.count_documents({"user_id": user_id})
        reqs = list(db_requests.find({"user_id": user_id}).sort("_id", DESCENDING).skip(skip).limit(limit))

        if not reqs:
            edit_message(chat_id, message_id, "📊 <b>Request History</b>\n\nNo request history found.", reply_markup=get_back_keyboard())
            return

        hist_text = f"📊 <b>Request History (Page {page + 1})</b>\n\n"
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
        rows.append([{"text": "🔙 Back", "callback_data": "main_menu"}])

        edit_message(chat_id, message_id, hist_text, reply_markup={"inline_keyboard": rows})

    elif data.startswith("transactions_"):
        page = int(data.split("_")[1])
        limit = 5
        skip = page * limit

        total_tx = db_transactions.count_documents({"user_id": user_id})
        txs = list(db_transactions.find({"user_id": user_id}).sort("_id", DESCENDING).skip(skip).limit(limit))

        if not txs:
            edit_message(chat_id, message_id, "💰 <b>Transactions</b>\n\nNo transactions found.", reply_markup=get_back_keyboard("coins"))
            return

        tx_text = f"💰 <b>Transaction History (Page {page + 1})</b>\n\n"
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
        rows.append([{"text": "🔙 Back", "callback_data": "coins"}])

        edit_message(chat_id, message_id, tx_text, reply_markup={"inline_keyboard": rows})

    elif data == "support":
        settings = get_db_settings()
        sup_url = settings.get("support_url", config.SUPPORT_URL)
        sup_text = "💬 <b>Support</b>\n\nNeed help or facing issues with the bot? Contact our support team below."
        kb = {
            "inline_keyboard": [
                [{"text": "💬 Contact Support", "url": sup_url}],
                [{"text": "🔙 Back", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, sup_text, reply_markup=kb)

    elif data.startswith("admin_"):
        if not is_admin(user_id):
            answer_callback(call_id, "❌ Not authorized.", show_alert=True)
            return
        handle_admin_callback(user_id, chat_id, message_id, data)

# --- Admin Panel Callback Handler ---
def handle_admin_callback(user_id: int, chat_id: int, message_id: int, data: str):
    if data == "admin_panel":
        admin_text = "👑 <b>Admin Panel</b>\n\nSelect an administrative option:"
        kb = {
            "inline_keyboard": [
                [
                    {"text": "👥 Users", "callback_data": "admin_users_0"},
                    {"text": "📊 Statistics", "callback_data": "admin_stats"}
                ],
                [
                    {"text": "💰 Coin Settings", "callback_data": "admin_coin_settings"},
                    {"text": "🎁 Referral Settings", "callback_data": "admin_ref_settings"}
                ],
                [
                    {"text": "📢 Broadcast", "callback_data": "admin_broadcast_prompt"},
                    {"text": "📢 Force Sub", "callback_data": "admin_forcesub_prompt"}
                ],
                [
                    {"text": "📤 Export Users CSV", "callback_data": "admin_export_users"},
                    {"text": "📤 Export Requests CSV", "callback_data": "admin_export_reqs"}
                ],
                [
                    {"text": "🔐 Manage Admins", "callback_data": "admin_manage_admins"}
                ],
                [{"text": "🔙 Exit Admin", "callback_data": "main_menu"}]
            ]
        }
        edit_message(chat_id, message_id, admin_text, reply_markup=kb)

    elif data == "admin_stats":
        tot_users = db_users.count_documents({})
        active_users = db_users.count_documents({"banned": False})
        tot_groups = db_groups.count_documents({})

        tot_reqs = db_requests.count_documents({})
        succ_reqs = db_requests.count_documents({"status": "success"})
        fail_reqs = db_requests.count_documents({"status": "failed"})

        pipeline_earned = [{"$match": {"type": {"$in": ["referral", "admin_add"]}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
        pipeline_spent = [{"$match": {"type": "spend"}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]

        earned_res = list(db_transactions.aggregate(pipeline_earned))
        spent_res = list(db_transactions.aggregate(pipeline_spent))

        coins_dist = earned_res[0]["total"] if earned_res else 0
        coins_spent = spent_res[0]["total"] if spent_res else 0
        tot_refs = db_referrals.count_documents({})

        stats_text = (
            f"📊 <b>Bot Statistics</b>\n\n"
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

        tot_u = db_users.count_documents({})
        users_list = list(db_users.find({}).sort("_id", DESCENDING).skip(skip).limit(limit))

        text = f"👥 <b>User Management (Page {page + 1})</b>\n\n"
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

        kb_rows.append([{"text": "🔙 Back", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": kb_rows})

    elif data.startswith("admin_manage_u_"):
        target_uid = int(data.split("_")[3])
        u = db_users.find_one({"telegram_id": target_uid})
        if not u:
            edit_message(chat_id, message_id, "User not found.", reply_markup=get_back_keyboard("admin_users_0"))
            return

        ban_btn = (
            {"text": "✅ Unban User", "callback_data": f"admin_unban_{target_uid}"}
            if u.get("banned")
            else {"text": "🚫 Ban User", "callback_data": f"admin_ban_{target_uid}"}
        )

        u_text = (
            f"👤 <b>User Management</b>\n\n"
            f"Name: <b>{esc(u.get('first_name'))}</b>\n"
            f"🆔 Telegram ID: <code>{u.get('telegram_id')}</code>\n"
            f"🔗 Username: @{esc(u.get('username'))}\n"
            f"💰 Coins: <b>{u.get('coins', 0)}</b>\n"
            f"🎁 Referrals: <b>{u.get('referral_count', 0)}</b>\n"
            f"🔎 Requests: <b>{u.get('total_requests', 0)}</b>\n"
            f"Status: <b>{'Banned' if u.get('banned') else 'Active'}</b>\n"
            f"📅 Joined: <b>{u.get('joined_at')}</b>"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "➕ Add Coins", "callback_data": f"admin_addcoins_{target_uid}"},
                    {"text": "➖ Remove Coins", "callback_data": f"admin_remcoins_{target_uid}"}
                ],
                [ban_btn],
                [{"text": "🔙 Back", "callback_data": "admin_users_0"}]
            ]
        }
        edit_message(chat_id, message_id, u_text, reply_markup=kb)

    elif data.startswith("admin_ban_"):
        target_uid = int(data.split("_")[2])
        db_users.update_one({"telegram_id": target_uid}, {"$set": {"banned": True}})
        handle_admin_callback(user_id, chat_id, message_id, f"admin_manage_u_{target_uid}")

    elif data.startswith("admin_unban_"):
        target_uid = int(data.split("_")[2])
        db_users.update_one({"telegram_id": target_uid}, {"$set": {"banned": False}})
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
            f"💰 <b>Coin Settings</b>\n\n"
            f"💰 Current Request Cost: <b>{cost} Coin(s)</b>\n"
            f"🆓 Daily Free Requests: <b>{free_req}</b>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "Change Request Cost", "callback_data": "admin_set_cost"}],
                [{"text": "Change Daily Free Limit", "callback_data": "admin_set_free"}],
                [{"text": "🔙 Back", "callback_data": "admin_panel"}]
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
        text = f"🎁 <b>Referral Settings</b>\n\nCurrent Referral Reward: <b>{reward} Coins</b>"
        kb = {
            "inline_keyboard": [
                [{"text": "Change Reward Amount", "callback_data": "admin_set_ref_reward"}],
                [{"text": "🔙 Back", "callback_data": "admin_panel"}]
            ]
        }
        edit_message(chat_id, message_id, text, reply_markup=kb)

    elif data == "admin_set_ref_reward":
        USER_STATES[user_id] = "AWAITING_NEW_REF_REWARD"
        edit_message(chat_id, message_id, "Send the new referral reward amount (integer):", reply_markup=get_back_keyboard("admin_ref_settings"))

    elif data == "admin_broadcast_prompt":
        USER_STATES[user_id] = "AWAITING_BROADCAST_TEXT"
        edit_message(chat_id, message_id, "📢 Send the message you want to broadcast to all users:", reply_markup=get_back_keyboard("admin_panel"))

    elif data == "admin_forcesub_prompt":
        settings = get_db_settings()
        curr = settings.get("force_sub_channel", "None")
        USER_STATES[user_id] = "AWAITING_FORCESUB_CHANNEL"
        edit_message(chat_id, message_id, f"Current Force Sub Channel: <code>{esc(curr)}</code>\n\nSend new channel username (e.g. <code>@mychannel</code>) or send <code>off</code> to disable:", reply_markup=get_back_keyboard("admin_panel"))

    elif data == "admin_export_users":
        edit_message(chat_id, message_id, "Generating Users CSV...")
        users = list(db_users.find({}))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Telegram ID", "Username", "First Name", "Coins", "Referrals", "Total Requests", "Joined At"])
        for u in users:
            writer.writerow([u.get("telegram_id"), u.get("username"), u.get("first_name"), u.get("coins"), u.get("referral_count"), u.get("total_requests"), u.get("joined_at")])

        send_document(chat_id, output.getvalue().encode('utf-8'), "users.csv", caption="Users Export")

    elif data == "admin_export_reqs":
        edit_message(chat_id, message_id, "Generating Requests CSV...")
        reqs = list(db_requests.find({}))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "UID", "Status", "Cost", "Used Free", "Created At"])
        for r in reqs:
            writer.writerow([r.get("user_id"), r.get("uid"), r.get("status"), r.get("cost"), r.get("used_free"), r.get("created_at")])

        send_document(chat_id, output.getvalue().encode('utf-8'), "requests.csv", caption="Requests Export")

    elif data == "admin_manage_admins":
        admins = list(db_admins.find({}))
        admin_text = f"🔐 <b>Admin Management</b>\n\n<b>System Admins:</b>\n"
        kb_rows = []
        for a in admins:
            aid = a.get("telegram_id")
            admin_text += f"• <code>{aid}</code>\n"
            kb_rows.append([{"text": f"Remove {aid}", "callback_data": f"admin_remove_admin_{aid}"}])

        kb_rows.append([{"text": "➕ Add Admin", "callback_data": "admin_add_admin_prompt"}])
        kb_rows.append([{"text": "🔙 Back", "callback_data": "admin_panel"}])
        edit_message(chat_id, message_id, admin_text, reply_markup={"inline_keyboard": kb_rows})

    elif data == "admin_add_admin_prompt":
        USER_STATES[user_id] = "AWAITING_ADD_ADMIN_ID"
        edit_message(chat_id, message_id, "Send the Telegram User ID to make Admin:", reply_markup=get_back_keyboard("admin_manage_admins"))

    elif data.startswith("admin_remove_admin_"):
        aid = int(data.split("_")[3])
        db_admins.delete_one({"telegram_id": aid})
        handle_admin_callback(user_id, chat_id, message_id, "admin_manage_admins")

# --- Message Processing Engine ---
def handle_text_message(msg: dict):
    from_user = msg["from"]
    user_id = from_user["id"]
    chat = msg["chat"]
    chat_id = chat["id"]
    chat_type = chat["type"]
    text = msg.get("text", "").strip()

    # Track Groups
    if chat_type in ["group", "supergroup"]:
        db_groups.update_one(
            {"chat_id": chat_id},
            {"$set": {"title": chat.get("title", "Group"), "updated_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")}},
            upsert=True
        )

    # Sync user record
    db_u = sync_user(from_user)

    if db_u.get("banned", False) and chat_type == "private":
        send_message(chat_id, "🚫 You are currently banned from using this bot.")
        return

    # Handle Group Commands
    if chat_type in ["group", "supergroup"]:
        if text.startswith("/duo"):
            parts = text.split()
            if len(parts) < 2:
                send_message(chat_id, "⚠️ Usage: <code>/duo UID</code>")
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
        send_message(chat_id, "⏳ Please wait a moment before sending another request.")
        return
    RATE_LIMITS[user_id] = now

    # Handle Command Shortcuts
    if text.startswith("/start"):
        parts = text.split()
        args = parts[1] if len(parts) > 1 else ""
        handle_start(from_user, chat_id, args)
        return
    elif text == "/help":
        send_message(chat_id, "🔎 Use the buttons below or type <code>/duo UID</code> to check duo info.")
        return
    elif text == "/id":
        send_message(chat_id, f"🆔 Your Telegram ID: <code>{user_id}</code>")
        return
    elif text == "/admin" and is_admin(user_id):
        m = send_message(chat_id, "Loading Admin Panel...")
        if m:
            handle_admin_callback(user_id, chat_id, m["message_id"], "admin_panel")
        return

    # Handle Interactive FSM States
    state = USER_STATES.get(user_id)

    if state == "AWAITING_UID":
        USER_STATES.pop(user_id, None)
        loading = send_message(chat_id, "⏳ Checking Duo information...")
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
            db_users.update_one({"telegram_id": target_uid}, {"$inc": {"coins": amt}})
            db_transactions.insert_one({
                "user_id": target_uid,
                "type": "admin_add",
                "amount": amt,
                "description": f"Added by admin {user_id}",
                "created_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S")
            })
            send_message(chat_id, f"Successfully added <b>{amt} Coins</b> to user <code>{target_uid}</code>.")

        elif state.startswith("AWAITING_REM_COINS_"):
            target_uid = int(state.split("_")[3])
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric coin amount.")
                return
            amt = int(text)
            db_users.update_one({"telegram_id": target_uid}, {"$inc": {"coins": -amt}})
            send_message(chat_id, f"Successfully removed <b>{amt} Coins</b> from user <code>{target_uid}</code>.")

        elif state == "AWAITING_NEW_COST":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            db_settings.update_one({"key": "global_config"}, {"$set": {"request_cost": int(text)}})
            send_message(chat_id, f"Request cost updated to <b>{text} Coins</b>.")

        elif state == "AWAITING_NEW_FREE":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            db_settings.update_one({"key": "global_config"}, {"$set": {"daily_free_requests": int(text)}})
            send_message(chat_id, f"Daily free requests limit updated to <b>{text}</b>.")

        elif state == "AWAITING_NEW_REF_REWARD":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric amount.")
                return
            db_settings.update_one({"key": "global_config"}, {"$set": {"referral_reward": int(text)}})
            send_message(chat_id, f"Referral reward updated to <b>{text} Coins</b>.")

        elif state == "AWAITING_FORCESUB_CHANNEL":
            USER_STATES.pop(user_id, None)
            val = "" if text.lower() == "off" else text
            db_settings.update_one({"key": "global_config"}, {"$set": {"force_sub_channel": val}})
            send_message(chat_id, f"Force sub channel updated to: <code>{val if val else 'Disabled'}</code>")

        elif state == "AWAITING_ADD_ADMIN_ID":
            USER_STATES.pop(user_id, None)
            if not text.isdigit():
                send_message(chat_id, "Please enter a valid numeric Telegram ID.")
                return
            aid = int(text)
            db_admins.update_one({"telegram_id": aid}, {"$set": {"added_by": user_id}}, upsert=True)
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
            send_message(chat_id, f"<b>Broadcast Preview:</b>\n\n{text}\n\nAre you sure you want to send this to all users?", reply_markup=kb)
        return

    # Default Fallback for direct text in private chat
    if text.isdigit():
        loading = send_message(chat_id, "⏳ Checking Duo information...")
        loading_id = loading["message_id"] if loading else None
        process_duo_request(user_id, chat_id, text, loading_id)
    else:
        send_message(chat_id, "🔎 Send a numeric UID or choose an option from /start menu.")

# Special Admin Confirmation Callback Handler
def handle_admin_broadcast_confirm(user_id: int, chat_id: int):
    data_dict = STATE_DATA.get(user_id, {})
    b_text = data_dict.get("broadcast_text")
    if not b_text:
        send_message(chat_id, "Broadcast session expired.")
        return

    USER_STATES.pop(user_id, None)
    STATE_DATA.pop(user_id, None)

    send_message(chat_id, "🚀 Starting broadcast...")
    users = list(db_users.find({}, {"telegram_id": 1}))

    success = 0
    failed = 0

    for u in users:
        target_id = u["telegram_id"]
        res = send_message(target_id, b_text)
        if res:
            success += 1
        else:
            failed += 1
        time.sleep(0.05)

    send_message(chat_id, f"✅ <b>Broadcast Completed!</b>\n\n✅ Sent: <b>{success}</b>\n❌ Failed: <b>{failed}</b>")

# --- Telegram Polling Engine ---
def start_polling():
    api_call("deleteWebhook", {"drop_pending_updates": True})
    logger.info("Previous Webhooks cleared. Starting Telegram Long Polling Loop...")

    offset = 0
    while True:
        try:
            updates = api_call("getUpdates", {"offset": offset, "timeout": 20})
            if updates and isinstance(updates, list):
                for update in updates:
                    offset = update["update_id"] + 1

                    if "message" in update:
                        handle_text_message(update["message"])
                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        if cb.get("data") == "admin_confirm_broadcast":
                            answer_callback(cb["id"])
                            handle_admin_broadcast_confirm(cb["from"]["id"], cb["message"]["chat"]["id"])
                        else:
                            handle_callback(cb)

        except Exception as e:
            logger.error(f"Error in Long Polling Loop: {e}")
            time.sleep(3)

# --- Entry Point ---
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask Web Server started on port {config.PORT}")

    start_polling()
