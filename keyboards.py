from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def main_kb(is_admin: bool=False):
    b=InlineKeyboardBuilder()
    for text,cb in [("🔎 Check Duo","check"),("💰 Balance","balance"),("🎁 Daily Free","daily"),("👥 Refer & Earn","referral"),("📊 My Stats","stats"),("📜 History","history"),("💳 Buy Coins","buy"),("📞 Support","support")]:
        b.button(text=text,callback_data=f"m:{cb}")
    if is_admin: b.button(text="👑 Admin Panel",callback_data="admin:menu")
    b.adjust(2,2,2,2,1)
    return b.as_markup()

def back(cb="back"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back",callback_data=cb)]])

def daily_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Use Daily Free",callback_data="daily:use")],[InlineKeyboardButton(text="🔙 Back",callback_data="back")]])
def support_kb(username="",chat=""):
    rows=[]
    if username: rows.append([InlineKeyboardButton(text="💬 Support",url=f"https://t.me/{username.lstrip('@')}")])
    elif chat: rows.append([InlineKeyboardButton(text="💬 Support Chat",url=chat)])
    rows.append([InlineKeyboardButton(text="🔙 Back",callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel",callback_data="cancel")]])

def admin_menu():
    b=InlineKeyboardBuilder()
    items=[("📊 Dashboard","dashboard"),("👥 Users","users"),("🔎 Requests","requests"),("💰 Economy","economy"),("🎁 Daily Free","daily"),("👥 Referrals","referrals"),("📢 Broadcast","broadcast"),("⚙️ Settings","settings"),("📜 Logs","logs"),("🛡 Security","security"),("💳 Payments","payments")]
    for t,c in items:b.button(text=t,callback_data=f"adm:{c}")
    b.adjust(2,2,2,2,2,1)
    return b.as_markup()

def admin_back(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Admin Menu",callback_data="admin:menu")]])

def admin_settings():
    rows=[]
    for text,key in [("🔎 Request Cost","request_cost"),("🎁 Daily Free","daily_free_requests"),("👥 Referral Reward","referral_reward"),("⏳ Rate Limit","rate_limit_seconds"),("🗄 Cache TTL","cache_ttl"),("🛠 Maintenance","maintenance_mode"),("👥 Referrals ON/OFF","referral_enabled"),("🎁 Daily ON/OFF","daily_free_enabled"),("👥 Group ON/OFF","group_enabled"),("💳 Payments ON/OFF","payment_enabled")]:
        rows.append([InlineKeyboardButton(text=text,callback_data=f"set:{key}")])
    rows.append([InlineKeyboardButton(text="🔙 Admin Menu",callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm(action): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Confirm",callback_data=f"confirm:{action}"),InlineKeyboardButton(text="❌ Cancel",callback_data="admin:menu")]])

def payment_packages(packages: dict):
    b=InlineKeyboardBuilder()
    for key,p in packages.items(): b.button(text=f"{p['coins']} Coins — ৳{p['price']}",callback_data=f"pkg:{key}")
    b.button(text="🔙 Back",callback_data="back")
    b.adjust(1)
    return b.as_markup()

def payment_methods():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💸 bKash",callback_data="paym:bkash"),InlineKeyboardButton(text="💸 Nagad",callback_data="paym:nagad")],[InlineKeyboardButton(text="🔙 Back",callback_data="back")]])

def payment_review(pid:int):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Approve",callback_data=f"pay:approve:{pid}"),InlineKeyboardButton(text="❌ Reject",callback_data=f"pay:reject:{pid}")],[InlineKeyboardButton(text="🔙 Admin",callback_data="admin:menu")]])

def history_nav(page:int):
    rows=[]
    row=[]
    if page>0: row.append(InlineKeyboardButton(text="⬅️",callback_data=f"hist:{page-1}"))
    row.append(InlineKeyboardButton(text=f"Page {page+1}",callback_data="noop"))
    row.append(InlineKeyboardButton(text="➡️",callback_data=f"hist:{page+1}"))
    rows.append(row); rows.append([InlineKeyboardButton(text="🔙 Back",callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def contact_kb():
    b=ReplyKeyboardBuilder(); b.add(KeyboardButton(text="📞 Share Contact",request_contact=True)); b.add(KeyboardButton(text="❌ Cancel")); b.adjust(1); return b.as_markup(resize_keyboard=True,one_time_keyboard=True)
