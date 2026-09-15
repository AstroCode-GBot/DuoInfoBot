from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from api import DuoAPI, validate_uid
from config import Config, cemoji
from database import Database
from keyboards import *

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("ffd")

cfg=Config.from_env()
db=Database(cfg.database_path,cfg.settings_defaults)
api=DuoAPI(cfg.api_base_url,db,cfg.settings_defaults.api_connect_timeout,cfg.settings_defaults.api_read_timeout,cfg.settings_defaults.api_retries)
bot=Bot(cfg.bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp=Dispatcher(); router=Router(); dp.include_router(router)
last_request:dict[tuple[int,int],float]=defaultdict(float)

class UidState(StatesGroup): uid=State()
class BroadcastState(StatesGroup): text=State()
class AdminAmountState(StatesGroup): value=State()
class AdminSettingState(StatesGroup): key=State(); value=State()
class SearchState(StatesGroup): query=State()
class PaymentTxState(StatesGroup): txid=State()


def is_admin(uid:int)->bool:return uid in cfg.admin_ids

def user_label(m:Message|dict):
    if isinstance(m,dict):
        return (f"@{m.get('username')}" if m.get('username') else str(m.get('first_name') or m.get('telegram_id')))
    return f"@{m.from_user.username}" if m.from_user.username else html.escape(m.from_user.full_name)

async def ensure_user(m:Message, ref:int|None=None):
    u,created=await db.upsert_user(m.from_user.id,m.from_user.username,m.from_user.first_name,m.from_user.last_name,ref)
    if created:
        log.info("new user id=%s username=%s",m.from_user.id,m.from_user.username)
        if ref:
            await db.attach_referral(m.from_user.id,ref)
    return u,created

async def log_group(text:str):
    if cfg.admin_log_chat_id:
        try: await bot.send_message(cfg.admin_log_chat_id,text)
        except Exception: log.exception("admin log delivery failed")

async def admin_guard(obj:CallbackQuery|Message)->bool:
    uid=obj.from_user.id
    if not is_admin(uid):
        if isinstance(obj,CallbackQuery):
            await obj.answer("❌ Unauthorized",show_alert=True)
        else: await obj.answer("❌ Unauthorized")
        await db.log_admin(uid,"SECURITY_UNAUTHORIZED")
        return False
    return True

async def maintenance_block(m:Message)->bool:
    if is_admin(m.from_user.id): return False
    if await db.get_bool("maintenance_mode",False):
        await m.answer("🛠 <b>Bot is currently under maintenance.</b>\nPlease try again later.")
        return True
    return False

async def rate_ok(user_id:int,chat_id:int,group:bool=False)->bool:
    key=(chat_id if group else user_id, 1 if group else 0)
    now=time.monotonic(); limit=await db.get_float("group_rate_limit_seconds" if group else "rate_limit_seconds",3)
    if now-last_request[key]<limit:return False
    last_request[key]=now; return True

async def daily_state(user_id:int)->str:
    limit=await db.get_int("daily_free_requests",1)
    if limit<=0 or not await db.get_bool("daily_free_enabled",True):return "disabled"
    day=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
    used=len(await db.request_history(user_id,limit=100,offset=0))
    # Exact daily free count is queried directly via DB-compatible history pages; most installs use a small limit.
    free_used=sum(1 for x in await db.request_history(user_id,limit=200,offset=0) if x["used_free"] and str(x["created_at"]).startswith(day.replace("-","",0)))
    # SQLite stores UTC; use the user's date as a practical boundary, while request rows are still auditable.
    if free_used>=limit:return "used"
    return "available"

async def check_and_reserve_free(user_id:int)->bool:
    limit=await db.get_int("daily_free_requests",1)
    if not await db.get_bool("daily_free_enabled",True) or limit<=0:return False
    day=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
    return await db.reserve_free_request(user_id,limit,day)

async def format_duo(data:dict)->str:
    def e(v):return html.escape(str(v if v not in (None,"") else "N/A"))
    status=e(data.get("DuoStatus","N/A")); status_icon="🟢" if status.lower()=="active" else "🔴" if status.lower() in {"inactive","expired"} else "⚪"
    created=e(data.get("DuoCreationDate","N/A")).replace(" AM"," AM").replace(" PM"," PM")
    return (f"╭━━━〔 🔗 <b>DUO INFORMATION</b> 〕━━━╮\n\n"
            f"👤 <b>Player</b>\n├ Name: {e(data.get('PlayerName'))}\n└ UID: {e(data.get('PlayerUID'))}\n\n"
            f"🤝 <b>Duo Partner</b>\n├ Name: {e(data.get('DuoPartnerName'))}\n└ UID: {e(data.get('DuoPartnerUID'))}\n\n"
            f"📊 <b>Duo Statistics</b>\n├ Level: {e(data.get('DuoLevel'))}\n├ Days: {e(data.get('DuoDays'))}\n├ Score: {e(data.get('DuoScore'))}\n└ Status: {status_icon} {status}\n\n"
            f"📅 <b>Created</b>\n└ {created}\n\n"
            f"╰━━━━━━━━━━━━━━━━━━━━╯\n⚡ Checked via Duo Info API")

async def run_duo(m:Message,uid:str,group:bool=False,group_cost:int|None=None):
    if await maintenance_block(m):return
    uid2=validate_uid(uid,cfg.settings_defaults.uid_min_len,cfg.settings_defaults.uid_max_len)
    if not uid2:
        await m.answer(f"{cemoji('error')} <b>Invalid UID</b>\nSend digits only ({cfg.settings_defaults.uid_min_len}–{cfg.settings_defaults.uid_max_len} digits).")
        return
    if not await rate_ok(m.from_user.id,m.chat.id,group):
        await m.answer("⏳ Please wait a moment before making another request."); return
    u=await db.get_user(m.from_user.id)
    if not u: u,_=await db.upsert_user(m.from_user.id,m.from_user.username,m.from_user.first_name,m.from_user.last_name)
    if u["is_banned"]:
        await m.answer("🚫 <b>Your access to this bot has been restricted.</b>"); return
    cost=group_cost if group and group_cost is not None else await db.get_int("request_cost",2)
    free=await check_and_reserve_free(m.from_user.id)
    charged=False
    if not free:
        balance=u["balance"]
        if balance<cost:
            await m.answer(f"❌ <b>Insufficient balance.</b>\nRequired: <b>{cost}</b> coins\nBalance: <b>{balance}</b> coins")
            return
        new_balance=await db.charge(m.from_user.id,cost,"Duo request",uid2)
        if new_balance is None:
            await m.answer("❌ Insufficient balance. Please try again."); return
        charged=True
    wait=await m.answer(f"{cemoji('wait')} <b>Checking Duo information...</b>")
    try:
        ttl=await db.get_int("cache_ttl",60)
        result=await api.fetch(uid2,ttl)
        await db.create_request(m.from_user.id,m.chat.id,uid2,0 if free else cost,free,True)
        await db.qualify_referral(m.from_user.id,await db.get_int("referral_reward",10),await db.get_int("referral_min_activity",0))
        text=await format_duo(result.data)
        text += f"\n\n💰 {'Free request used' if free else f'Cost: {cost} coins'}"
        await wait.edit_text(text)
        await log_group(f"🔎 <b>DUO REQUEST</b>\nUser: {html.escape(user_label(m))}\nID: <code>{m.from_user.id}</code>\nUID: <code>{uid2}</code>\nCost: {0 if free else cost}\nResult: SUCCESS\nTime: {datetime.now().isoformat(timespec='seconds')}")
    except Exception as exc:
        await db.create_request(m.from_user.id,m.chat.id,uid2,0 if free else cost,False,False,type(exc).__name__)
        if charged:
            await db.refund(m.from_user.id,cost,"Duo request refund",uid2)
        log.exception("duo request failed")
        await wait.edit_text(f"{cemoji('error')} <b>Unable to retrieve Duo information right now.</b>\nPlease try again later.")

@router.message(CommandStart())
async def start(m:Message):
    if m.chat.type!=ChatType.PRIVATE:return
    ref=None
    args=(m.text or "").split(maxsplit=1)
    if len(args)==2 and args[1].startswith("ref_"):
        raw=args[1][4:]
        if raw.isdigit():ref=int(raw)
    u,created=await ensure_user(m,ref)
    if created and ref and await db.get_bool("referral_enabled",True):
        await log_group(f"👥 <b>NEW REFERRAL</b>\nReferrer ID: <code>{ref}</code>\nNew User: {html.escape(user_label(m))}")
    if await maintenance_block(m):return
    msg=f"{cemoji('welcome')} <b>Welcome to Duo Info Bot</b>\n\n🚀 Fast & Advanced Duo Information System\n\nUse this bot to check Duo information by UID.\n\n💰 Balance: <b>{u['balance']}</b> Coins\n🎁 Daily Free Request: {'Available' if await db.get_bool('daily_free_enabled',True) else 'Disabled'}\n\n👇 <b>Choose an option:</b>"
    await m.answer(msg,reply_markup=main_kb(is_admin(m.from_user.id)))

@router.message(Command("help"))
async def help_cmd(m:Message):
    await ensure_user(m)
    await m.answer("<b>Commands</b>\n/start\n/duo UID\n/checkduo UID\n/balance\n/referral\n/stats\n/history\n/admin (admins only)",reply_markup=main_kb(is_admin(m.from_user.id)))

@router.message(Command("duo","checkduo"))
async def duo_cmd(m:Message):
    await ensure_user(m)
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)==2: await run_duo(m,parts[1].strip())
    else:
        if m.chat.type==ChatType.PRIVATE:
            await m.answer("🆔 <b>Send the Player UID</b>\nExample: <code>824648540</code>",reply_markup=cancel_kb()); await UidState.uid.set()
        else: await m.answer("Usage: <code>/duo 824648540</code>")

@router.message(UidState.uid)
async def uid_state(m:Message,state:FSMContext):
    await state.clear(); await run_duo(m,m.text or "")

@router.callback_query(F.data=="cancel")
async def cancel_cb(c:CallbackQuery,state:FSMContext):
    await state.clear(); await c.message.edit_text("❌ Cancelled.",reply_markup=main_kb(is_admin(c.from_user.id))); await c.answer()

@router.callback_query(F.data=="back")
async def back_cb(c:CallbackQuery,state:FSMContext):
    await state.clear(); await c.message.edit_text("🏠 <b>Main Menu</b>",reply_markup=main_kb(is_admin(c.from_user.id))); await c.answer()

@router.callback_query(F.data.startswith("m:"))
async def menu_cb(c:CallbackQuery,state:FSMContext):
    await state.clear(); action=c.data.split(":",1)[1]
    if await db.is_banned(c.from_user.id): await c.answer("🚫 Restricted",show_alert=True); return
    if action=="check": await c.message.edit_text("🆔 <b>Send the Player UID</b>\nExample: <code>824648540</code>",reply_markup=cancel_kb()); await state.set_state(UidState.uid)
    elif action=="balance":
        u=await db.get_user(c.from_user.id); await c.message.edit_text(f"💰 <b>Balance</b>\n\nCoins: <b>{u['balance'] if u else 0}</b>\nTotal earned: {u['total_earned'] if u else 0}\nTotal spent: {u['total_spent'] if u else 0}",reply_markup=back())
    elif action=="daily": await daily_cb(c)
    elif action=="referral": await referral_cb(c)
    elif action=="stats": await stats_cb(c)
    elif action=="history": await history_cb(c,0)
    elif action=="buy": await buy_cb(c)
    elif action=="support": await c.message.edit_text("📞 <b>Support</b>\nUse the support contact below.",reply_markup=support_kb(cfg.support_username,cfg.support_chat))
    await c.answer()

async def daily_cb(c:CallbackQuery):
    limit=await db.get_int("daily_free_requests",1); enabled=await db.get_bool("daily_free_enabled",True)
    await c.message.edit_text(f"🎁 <b>Daily Free Request</b>\n\nLimit: <b>{limit}</b>/day\nStatus: <b>{'Enabled' if enabled else 'Disabled'}</b>\n\nUse your free request before spending coins.",reply_markup=daily_kb())

@router.callback_query(F.data=="daily:use")
async def daily_use(c:CallbackQuery):
    await c.answer()
    if not await rate_ok(c.from_user.id,c.message.chat.id): await c.message.edit_text("⏳ Please wait a moment.",reply_markup=back()); return
    limit=await db.get_int("daily_free_requests",1)
    day=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
    # Do not consume the slot until a valid UID is actually submitted. The DB claim is atomic.
    u=await db.get_user(c.from_user.id)
    if not u:
        await c.message.edit_text("❌ User session expired. Send /start again.",reply_markup=back()); return
    await c.message.edit_text("🆔 <b>Send the Player UID</b>\nThis request will be free.",reply_markup=cancel_kb())
    # mark reservation by state only; the actual request records the free use. Race-safe DB reservation check happens again in normal flow.
    # The flow deliberately does not deduct coins.
    await stateful_free_prompt_set(c.from_user.id)

_free_waiting: set[int] = set()

async def stateful_free_prompt_set(uid: int):
    _free_waiting.add(uid)

async def stateful_free_prompt_clear(uid: int):
    _free_waiting.discard(uid)

async def stateful_get(uid: int) -> bool:
    return uid in _free_waiting

@router.message()
async def text_router(m:Message,state:FSMContext):
    if m.chat.type!=ChatType.PRIVATE and m.text and m.text.startswith("/"): return
    if m.contact and m.contact.user_id==m.from_user.id:
        await ensure_user(m); await db.set_phone(m.from_user.id,m.contact.phone_number); await m.answer("✅ Contact saved."); return
    if await stateful_get(m.from_user.id):
        await stateful_free_prompt_clear(m.from_user.id)
        uid=validate_uid(m.text or "",cfg.settings_defaults.uid_min_len,cfg.settings_defaults.uid_max_len)
        if not uid: await m.answer("❌ Invalid UID."); return
        day=datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
        if not await db.reserve_free_request(m.from_user.id,await db.get_int("daily_free_requests",1),day):
            await m.answer("🎁 You've already used today's free request. Use the normal Check Duo flow to spend coins."); return
        u=await db.get_user(m.from_user.id)
        if u and u["is_banned"]: await m.answer("🚫 Restricted"); return
        if not await rate_ok(m.from_user.id,m.chat.id): await m.answer("⏳ Please wait."); return
        wait=await m.answer("⏳ <b>Checking Duo information...</b>")
        try:
            res=await api.fetch(uid,await db.get_int("cache_ttl",60)); await db.create_request(m.from_user.id,m.chat.id,uid,0,True,True); await db.qualify_referral(m.from_user.id,await db.get_int("referral_reward",10),await db.get_int("referral_min_activity",0)); await wait.edit_text((await format_duo(res.data))+"\n\n💰 Free request used")
        except Exception:
            await db.create_request(m.from_user.id,m.chat.id,uid,0,True,False,"api_error")
            await db.release_free_request(m.from_user.id,day)
            await wait.edit_text("❌ <b>Unable to retrieve Duo information right now.</b>\nPlease try again later. Your free request was returned.")
        return
    if m.text:
        text=m.text.strip()
        if text=="📞 Share Contact": await m.answer("Share your contact using Telegram's button.",reply_markup=contact_kb()); return
        if text.startswith("/"): await m.answer("❓ <b>Unknown command.</b>\nPlease use the menu below.",reply_markup=main_kb(is_admin(m.from_user.id)))

async def referral_cb(c:CallbackQuery):
    s=await db.referral_stats(c.from_user.id); link=f"https://t.me/{cfg.bot_username}?start=ref_{c.from_user.id}"
    await c.message.edit_text(f"👥 <b>REFER & EARN</b>\n\nYour referrals: {s['total']}\nSuccessful referrals: {s['successful']}\nCoins earned: {s['earned']}\n\n🔗 <b>Your referral link:</b>\n<code>{html.escape(link)}</code>\n\nInvite friends and earn coins 🚀",reply_markup=back())

async def stats_cb(c:CallbackQuery):
    u=await db.get_user(c.from_user.id); today=await db.request_count_today(c.from_user.id); s=await db.referral_stats(c.from_user.id)
    await c.message.edit_text(f"📊 <b>MY STATS</b>\n\n👤 Account\n├ Telegram ID: <code>{c.from_user.id}</code>\n├ Username: {html.escape('@'+u['username']) if u and u['username'] else 'N/A'}\n├ Name: {html.escape((u['first_name'] or '')+' '+(u['last_name'] or '')).strip()}\n├ Join Date: {u['created_at'] if u else 'N/A'}\n└ Account Status: {'Banned' if u and u['is_banned'] else 'Active'}\n\n💰 Economy\n├ Balance: {u['balance'] if u else 0}\n├ Total Earned: {u['total_earned'] if u else 0}\n├ Total Spent: {u['total_spent'] if u else 0}\n└ Referral Earnings: {u['referral_earnings'] if u else 0}\n\n🔎 Usage\n└ Today: {today}\n👥 Referrals: {s['total']}",reply_markup=back())

async def history_cb(c:CallbackQuery,page:int):
    rows=await db.request_history(c.from_user.id,8,page*8)
    if not rows: text="📜 <b>History</b>\n\nNo requests found."
    else:
        chunks=["📜 <b>History</b>"]
        for r in rows: chunks.append(f"\n🆔 UID: <code>{r['uid']}</code>\n📅 {r['created_at']}\n💰 Cost: {r['cost']}\n✅ Success" if r['success'] else f"\n🆔 UID: <code>{r['uid']}</code>\n📅 {r['created_at']}\n💰 Cost: {r['cost']}\n❌ Failed")
        text="".join(chunks)
    await c.message.edit_text(text,reply_markup=history_nav(page))

@router.callback_query(F.data.startswith("hist:"))
async def hist_cb(c:CallbackQuery): await history_cb(c,int(c.data.split(":")[1])); await c.answer()

async def buy_cb(c:CallbackQuery):
    if not await db.get_bool("payment_enabled",True): await c.message.edit_text("💳 <b>Payments are currently disabled.</b>",reply_markup=back()); return
    raw=await db.setting("packages_json",'{}')
    try: packages=json.loads(raw)
    except Exception: packages={}
    await c.message.edit_text("💳 <b>Buy Coins</b>\nChoose a package:",reply_markup=payment_packages(packages))

@router.callback_query(F.data.startswith("pkg:"))
async def pkg_cb(c:CallbackQuery,state:FSMContext):
    key=c.data.split(":",1)[1]
    packages=json.loads(await db.setting("packages_json",'{}'))
    p=packages.get(key)
    if not p: await c.answer("Invalid package",show_alert=True); return
    await state.update_data(package=key,coins=int(p['coins']),price=float(p['price'])); await c.message.edit_text("Select payment method:",reply_markup=payment_methods()); await c.answer()

@router.callback_query(F.data.startswith("paym:"))
async def paym_cb(c:CallbackQuery,state:FSMContext):
    method=c.data.split(":",1)[1]; data=await state.get_data(); number=await db.setting(f"{method}_number","")
    if not data.get("package"): await c.answer("Package expired",show_alert=True); return
    await state.update_data(method=method)
    await c.message.edit_text(f"💸 <b>{method.upper()}</b>\n\nSend <b>৳{data['price']}</b> to:\n<code>{html.escape(str(number) or 'Not configured')}</code>\n\nThen send your transaction ID here.",reply_markup=cancel_kb()); await state.set_state(PaymentTxState.txid); await c.answer()

@router.message(PaymentTxState.txid)
async def payment_tx(m:Message,state:FSMContext):
    txid=(m.text or "").strip()
    if not 3<=len(txid)<=100: await m.answer("❌ Invalid transaction ID."); return
    data=await state.get_data(); await state.clear()
    pid=await db.create_payment(m.from_user.id,str(data['package']),int(data['coins']),float(data['price']),str(data['method']),txid)
    await m.answer(f"✅ <b>Payment submitted.</b>\nPayment ID: <code>{pid}</code>\nStatus: Pending review.",reply_markup=main_kb(is_admin(m.from_user.id)))
    await log_group(f"💰 <b>COIN TRANSACTION</b>\nUser: {html.escape(user_label(m))}\nPayment: <code>{pid}</code>\nAmount: ৳{data['price']}\nCoins: {data['coins']}\nMethod: {data['method']}\nStatus: PENDING")

@router.message(Command("balance"))
async def balance_cmd(m:Message): await ensure_user(m); u=await db.get_user(m.from_user.id); await m.answer(f"💰 Balance: <b>{u['balance']}</b> coins")
@router.message(Command("referral"))
async def referral_cmd(m:Message): await ensure_user(m); await referral_cb_from_message(m)
async def referral_cb_from_message(m:Message):
    s=await db.referral_stats(m.from_user.id); await m.answer(f"👥 Referrals: <b>{s['total']}</b>\n✅ Successful: <b>{s['successful']}</b>\n💰 Earned: <b>{s['earned']}</b>\n\n🔗 <code>https://t.me/{cfg.bot_username}?start=ref_{m.from_user.id}</code>")
@router.message(Command("stats"))
async def stats_cmd(m:Message): await ensure_user(m); dummy=type("C",(),{"from_user":m.from_user})(); await m.answer("Use the inline menu for full stats.",reply_markup=main_kb(is_admin(m.from_user.id)))
@router.message(Command("history"))
async def history_cmd(m:Message): await ensure_user(m); rows=await db.request_history(m.from_user.id,8,0); await m.answer("📜 <b>History</b>\n\n"+"\n".join(f"<code>{r['uid']}</code> — {'✅' if r['success'] else '❌'} — {r['cost']}" for r in rows) if rows else "📜 <b>History</b>\n\nNo requests found.")

@router.message(Command("admin"))
async def admin_cmd(m:Message):
    if await admin_guard(m): await m.answer("👑 <b>ADMIN PANEL</b>",reply_markup=admin_menu())

@router.callback_query(F.data=="admin:menu")
async def admin_menu_cb(c:CallbackQuery):
    if not await admin_guard(c):return
    await c.message.edit_text("👑 <b>ADMIN PANEL</b>",reply_markup=admin_menu()); await c.answer()

@router.callback_query(F.data.startswith("adm:"))
async def adm_cb(c:CallbackQuery,state:FSMContext):
    if not await admin_guard(c):return
    action=c.data.split(":",1)[1]
    if action=="dashboard":
        s=await db.stats(); await c.message.edit_text(f"📊 <b>BOT DASHBOARD</b>\n\n👥 Total Users: {s['users']}\n🔎 Total Requests: {s['requests']}\n✅ Successful: {s['successful']}\n💸 Coins Spent: {s['spent']}\n💰 Coins Distributed: {s['distributed']}\n\n📅 Today\n├ New Users: {s['today_users']}\n└ Requests: {s['today_requests']}",reply_markup=admin_back())
    elif action=="users": await c.message.edit_text("👥 <b>USER SEARCH</b>\nUse <code>/searchuser ID_or_username</code>.",reply_markup=admin_back())
    elif action=="economy":
        cost=await db.get_int("request_cost",2); await c.message.edit_text(f"💰 <b>ECONOMY</b>\nRequest Cost: <b>{cost}</b>",reply_markup=admin_back())
    elif action=="daily": await c.message.edit_text(f"🎁 Daily Free: <b>{await db.get_int('daily_free_requests',1)}</b>\nEnabled: <b>{await db.get_bool('daily_free_enabled',True)}</b>",reply_markup=admin_back())
    elif action=="referrals": await c.message.edit_text(f"👥 Referral Reward: <b>{await db.get_int('referral_reward',10)}</b>\nMinimum activity: <b>{await db.get_int('referral_min_activity',0)}</b>\nEnabled: <b>{await db.get_bool('referral_enabled',True)}</b>",reply_markup=admin_back())
    elif action=="settings":
        await c.message.edit_text(f"⚙️ <b>SETTINGS</b>\n\n🔎 Request Cost: {await db.get_int('request_cost',2)}\n🎁 Daily Free: {await db.get_int('daily_free_requests',1)}\n👥 Referral Reward: {await db.get_int('referral_reward',10)}\n⏳ Rate Limit: {await db.get_float('rate_limit_seconds',3)} sec\n🗄 Cache TTL: {await db.get_int('cache_ttl',60)} sec\n🛠 Maintenance: {await db.get_bool('maintenance_mode',False)}",reply_markup=admin_settings())
    elif action=="payments":
        ps=await db.pending_payments(10)
        if not ps: await c.message.edit_text("💳 No pending payments.",reply_markup=admin_back())
        else:
            p=ps[0]; await c.message.edit_text(f"💳 <b>Payment #{p['id']}</b>\nUser: {html.escape('@'+p['username'] if p['username'] else str(p['telegram_id']))}\nPackage: {p['package_name']}\nCoins: {p['coin_amount']}\nAmount: ৳{p['amount']}\nMethod: {p['method']}\nTransaction ID: <code>{html.escape(p['transaction_id'])}</code>",reply_markup=payment_review(p['id']))
    elif action=="requests":
        a=await db.request_analytics(1); a7=await db.request_analytics(7); a30=await db.request_analytics(30)
        await c.message.edit_text(f"🔎 <b>REQUEST ANALYTICS</b>\n\nToday: {a['total']} requests / {a['successful']} successful / {a['spent']} coins\n7 Days: {a7['total']} / {a7['successful']} / {a7['spent']} coins\n30 Days: {a30['total']} / {a30['successful']} / {a30['spent']} coins\nUnique users (30d): {a30['users']}",reply_markup=admin_back())
    elif action=="broadcast":
        await c.message.edit_text("📢 <b>Broadcast</b>\nUse <code>/broadcast Your message</code> to create a preview.",reply_markup=admin_back())
    elif action=="security":
        await c.message.edit_text(f"🛡 <b>SECURITY</b>\nRate limit: {await db.get_float('rate_limit_seconds',3)} sec\nGroup rate: {await db.get_float('group_rate_limit_seconds',3)} sec\nAPI retries: {cfg.settings_defaults.api_retries}",reply_markup=admin_back())
    elif action=="logs":
        await c.message.edit_text("📜 Admin actions are stored in the admin_logs table and important events are forwarded to ADMIN_LOG_CHAT_ID.",reply_markup=admin_back())
    elif action=="top":
        rows=await db.top_users(); await c.message.edit_text("🏆 <b>TOP USERS</b>\n\n"+"\n".join(f"{i+1}. @{r['username'] or 'unknown'} — {r['requests']} requests" for i,r in enumerate(rows)),reply_markup=admin_back())
    else: await c.message.edit_text(f"🛡 <b>{html.escape(action.title())}</b>\nSee dedicated commands/actions in the admin panel.",reply_markup=admin_back())
    await c.answer()

@router.callback_query(F.data.startswith("set:"))
async def setting_cb(c: CallbackQuery, state: FSMContext):
    if not await admin_guard(c):
        return
    key=c.data.split(":",1)[1]
    boolean_keys={"maintenance_mode","referral_enabled","daily_free_enabled","group_enabled","payment_enabled"}
    if key in boolean_keys:
        current=await db.get_bool(key,False)
        await db.set_setting(key,int(not current))
        await db.log_admin(c.from_user.id,"SETTING_TOGGLE",key,f"{not current}")
        await c.message.edit_text(f"⚙️ <b>{html.escape(key)}</b> = <b>{not current}</b>",reply_markup=admin_settings())
        await c.answer("Updated")
        return
    await state.set_state(AdminSettingState.value)
    await state.update_data(setting_key=key)
    current=await db.setting(key,"")
    await c.message.edit_text(f"✏️ <b>Edit setting</b>\nKey: <code>{html.escape(key)}</code>\nCurrent: <b>{html.escape(str(current))}</b>\n\nSend the new value.",reply_markup=cancel_kb())
    await c.answer()

@router.message(AdminSettingState.value)
async def setting_value(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    data=await state.get_data(); key=data.get("setting_key")
    raw=(m.text or "").strip()
    numeric={"request_cost":int,"daily_free_requests":int,"referral_reward":int,"referral_min_activity":int,"cache_ttl":int,"rate_limit_seconds":float,"group_rate_limit_seconds":float}
    try:
        value=numeric[key](raw) if key in numeric else raw
        if key in {"request_cost","daily_free_requests","referral_reward","referral_min_activity","cache_ttl"} and value < 0: raise ValueError
        if key in {"rate_limit_seconds","group_rate_limit_seconds"} and value < 0: raise ValueError
    except (KeyError,ValueError):
        await m.answer("❌ Invalid value."); return
    await db.set_setting(key,value); await db.log_admin(m.from_user.id,"SETTING_UPDATE",key,str(value)); await state.clear()
    await m.answer(f"✅ Updated <code>{html.escape(key)}</code> → <b>{html.escape(str(value))}</b>",reply_markup=admin_settings())

@router.message(Command("addcoins"))
async def addcoins_cmd(m:Message):
    if not await admin_guard(m): return
    parts=(m.text or "").split()
    if len(parts)!=3 or not parts[1].isdigit() or not parts[2].isdigit():
        await m.answer("Usage: /addcoins USER_ID AMOUNT"); return
    uid,amount=int(parts[1]),int(parts[2]); new=await db.add_coins(uid,amount,"Admin bonus",str(m.from_user.id)); await db.log_admin(m.from_user.id,"ADD_COINS",str(uid),str(amount)); await log_group(f"👑 <b>ADMIN ACTION</b>\nAdmin: <code>{m.from_user.id}</code>\nAction: Added {amount} coins\nTarget: <code>{uid}</code>\nBalance: {new}"); await m.answer(f"✅ Added {amount} coins. New balance: {new}")

@router.message(Command("removecoins"))
async def removecoins_cmd(m:Message):
    if not await admin_guard(m): return
    parts=(m.text or "").split()
    if len(parts)!=3 or not parts[1].isdigit() or not parts[2].isdigit(): await m.answer("Usage: /removecoins USER_ID AMOUNT"); return
    uid,amount=int(parts[1]),int(parts[2]); new=await db.remove_coins(uid,amount,"Admin adjustment",str(m.from_user.id))
    if new is None: await m.answer("❌ User not found or insufficient balance."); return
    await db.log_admin(m.from_user.id,"REMOVE_COINS",str(uid),str(amount)); await m.answer(f"✅ Removed {amount} coins. New balance: {new}")

@router.message(Command("ban","unban"))
async def ban_cmd(m:Message):
    if not await admin_guard(m): return
    parts=(m.text or "").split(); is_ban=(parts[0].split("@")[-1].lower()=="ban")
    if len(parts)!=2 or not parts[1].isdigit(): await m.answer(f"Usage: /{'ban' if is_ban else 'unban'} USER_ID"); return
    uid=int(parts[1]); await db.set_ban(uid,is_ban); await db.log_admin(m.from_user.id,"BAN" if is_ban else "UNBAN",str(uid)); await m.answer(f"✅ User {uid}: {'banned' if is_ban else 'unbanned'}.")

@router.message(Command("broadcast"))
async def broadcast_cmd(m:Message,state:FSMContext):
    if not await admin_guard(m): return
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)!=2: await m.answer("Usage: /broadcast Your message"); return
    await state.update_data(broadcast_text=parts[1]); await m.answer(f"📢 <b>Broadcast Preview</b>\n\n{html.escape(parts[1])}\n\nConfirm with /broadcastconfirm or cancel with /broadcastcancel.")

@router.message(Command("broadcastconfirm"))
async def broadcast_confirm(m:Message,state:FSMContext):
    if not await admin_guard(m): return
    data=await state.get_data(); text=data.get("broadcast_text")
    if not text: await m.answer("❌ No broadcast pending."); return
    await state.clear(); ids=await db.all_user_ids(); ok=fail=0
    for uid in ids:
        try:
            await bot.send_message(uid,text); ok+=1
        except (TelegramForbiddenError,TelegramBadRequest): fail+=1
        except Exception: fail+=1
        await asyncio.sleep(0.04)
    await db.log_admin(m.from_user.id,"BROADCAST","",f"success={ok},failed={fail}"); await m.answer(f"📢 Broadcast complete.\n✅ Sent: {ok}\n❌ Failed: {fail}")

@router.message(Command("broadcastcancel"))
async def broadcast_cancel(m:Message,state:FSMContext):
    if not await admin_guard(m): return
    await state.clear(); await m.answer("❌ Broadcast cancelled.")

@router.message(Command("health"))
async def health_cmd(m:Message):
    if not await admin_guard(m): return
    status="🟢 Online" if api.last_success and time.time()-api.last_success < 900 else "⚪ Unknown"
    await m.answer(f"🌐 <b>API STATUS</b>\n{status}\nLatency: {api.last_latency_ms} ms\nLast successful request: {datetime.fromtimestamp(api.last_success).isoformat(timespec='seconds') if api.last_success else 'N/A'}")

@router.message(Command("searchuser"))
async def search_user(m:Message):
    if not await admin_guard(m):return
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)!=2: await m.answer("Usage: /searchuser 123456789"); return
    rows=await db.search_users(parts[1]);
    if not rows: await m.answer("No users found."); return
    lines=[]
    for u in rows: lines.append(f"👤 <b>{html.escape('@'+u['username'] if u['username'] else u['first_name'] or 'N/A')}</b>\nID: <code>{u['telegram_id']}</code>\nBalance: {u['balance']}\nRequests: {(await db.request_count_today(u['telegram_id']))} today\nStatus: {'Banned' if u['is_banned'] else 'Active'}")
    await m.answer("\n\n".join(lines))

@router.callback_query(F.data.startswith("pay:"))
async def pay_review_cb(c:CallbackQuery):
    if not await admin_guard(c):return
    _,action,pid=c.data.split(":"); p=await db.review_payment(int(pid),c.from_user.id,action=="approve")
    if not p: await c.answer("Already reviewed",show_alert=True); return
    await db.log_admin(c.from_user.id,f"PAYMENT_{action.upper()}",pid)
    await c.message.edit_text(f"✅ Payment #{pid} marked <b>{p['status']}</b>.",reply_markup=admin_back()); await c.answer()

@router.message(F.chat.type.in_({ChatType.GROUP,ChatType.SUPERGROUP}),Command("duo","checkduo"))
async def group_duo(m:Message):
    if not await db.get_bool("group_enabled",True): await m.answer("🚫 Group usage is disabled."); return
    await ensure_user(m)
    cfg_g=await db.group_config(m.chat.id)
    if not cfg_g:
        cost=await db.get_int("request_cost",2); await db.save_group(m.chat.id,m.chat.title,cost); cfg_g=await db.group_config(m.chat.id)
    if not cfg_g["enabled"]: await m.answer("🚫 Bot is disabled in this group."); return
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)!=2: await m.answer("Usage: /duo 824648540"); return
    await run_duo(m,parts[1].strip(),True,int(cfg_g["request_cost"]))

@router.message(F.chat.type.in_({ChatType.GROUP,ChatType.SUPERGROUP}))
async def group_join(m:Message):
    # This handler only persists group metadata when the bot is actively used; no private data is exposed.
    if m.new_chat_members:
        for member in m.new_chat_members:
            if member.id==bot.id:
                cost=await db.get_int("request_cost",2); await db.save_group(m.chat.id,m.chat.title,cost); return

async def main():
    await db.init(); await api.start()
    log.info("FFDuoInfoBot starting; admins=%s",sorted(cfg.admin_ids))
    try:
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        await api.close(); await bot.session.close()

if __name__=="__main__":
    asyncio.run(main())
