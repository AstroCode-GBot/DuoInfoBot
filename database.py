from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from config import Settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 telegram_id INTEGER NOT NULL UNIQUE,
 username TEXT,
 first_name TEXT,
 last_name TEXT,
 phone_number TEXT,
 phone_shared_at TEXT,
 balance INTEGER NOT NULL DEFAULT 10,
 total_earned INTEGER NOT NULL DEFAULT 0,
 total_spent INTEGER NOT NULL DEFAULT 0,
 referral_earnings INTEGER NOT NULL DEFAULT 0,
 referrer_id INTEGER,
 referral_rewarded INTEGER NOT NULL DEFAULT 0,
 is_banned INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 last_active TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at);

CREATE TABLE IF NOT EXISTS transactions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL,
 amount INTEGER NOT NULL,
 reason TEXT NOT NULL,
 balance_after INTEGER NOT NULL,
 related_id TEXT,
 created_at TEXT NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL,
 chat_id INTEGER NOT NULL,
 uid TEXT NOT NULL,
 cost INTEGER NOT NULL DEFAULT 0,
 used_free INTEGER NOT NULL DEFAULT 0,
 success INTEGER NOT NULL DEFAULT 0,
 error_code TEXT,
 created_at TEXT NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_requests_uid ON requests(uid, created_at DESC);

CREATE TABLE IF NOT EXISTS referrals (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 referrer_user_id INTEGER NOT NULL,
 referred_user_id INTEGER NOT NULL UNIQUE,
 reward INTEGER NOT NULL DEFAULT 0,
 qualified INTEGER NOT NULL DEFAULT 0,
 rewarded INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 FOREIGN KEY(referrer_user_id) REFERENCES users(id),
 FOREIGN KEY(referred_user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id);

CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL,
 package_name TEXT NOT NULL,
 coin_amount INTEGER NOT NULL,
 amount REAL NOT NULL,
 method TEXT NOT NULL,
 transaction_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL,
 reviewed_by INTEGER,
 reviewed_at TEXT,
 FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at DESC);

CREATE TABLE IF NOT EXISTS groups_cfg (
 group_id INTEGER PRIMARY KEY,
 title TEXT,
 enabled INTEGER NOT NULL DEFAULT 1,
 request_cost INTEGER NOT NULL DEFAULT 2,
 allowed_commands TEXT NOT NULL DEFAULT 'duo,checkduo',
 joined_at TEXT NOT NULL,
 last_used TEXT
);

CREATE TABLE IF NOT EXISTS settings (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 admin_id INTEGER,
 action TEXT NOT NULL,
 target TEXT,
 details TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_logs_created ON admin_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS api_cache (
 uid TEXT PRIMARY KEY,
 payload TEXT NOT NULL,
 fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_claims (
 user_id INTEGER NOT NULL,
 claim_date TEXT NOT NULL,
 used_count INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 PRIMARY KEY(user_id, claim_date),
 FOREIGN KEY(user_id) REFERENCES users(id)
);
"""


class Database:
    def __init__(self, path: str, defaults: Settings):
        self.path = path
        self.defaults = defaults
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def init(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        defaults = {
            "request_cost": self.defaults.request_cost,
            "daily_free_requests": self.defaults.daily_free_requests,
            "referral_reward": self.defaults.referral_reward,
            "referral_min_activity": self.defaults.referral_min_activity,
            "rate_limit_seconds": self.defaults.rate_limit_seconds,
            "group_rate_limit_seconds": self.defaults.group_rate_limit_seconds,
            "cache_ttl": self.defaults.cache_ttl,
            "maintenance_mode": int(self.defaults.maintenance_mode),
            "referral_enabled": int(self.defaults.referral_enabled),
            "daily_free_enabled": int(self.defaults.daily_free_enabled),
            "group_enabled": int(self.defaults.group_enabled),
            "payment_enabled": int(self.defaults.payment_enabled),
            "packages_json": json.dumps({"10": {"coins": 10, "price": 10}, "25": {"coins": 25, "price": 20}, "60": {"coins": 60, "price": 45}}),
            "bkash_number": "",
            "nagad_number": "",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, str(v)))
        conn.close()

    @contextmanager
    def tx(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def _one(self, sql: str, params: Iterable[Any] = ()):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, tuple(params)).fetchone()

    async def get_settings(self) -> dict[str, str]:
        rows = await self._run(lambda: self._all("SELECT key,value FROM settings"))
        return {r["key"]: r["value"] for r in rows}

    def _all(self, sql: str, params: Iterable[Any] = ()):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, tuple(params)).fetchall()

    async def setting(self, key: str, default: Any = None) -> Any:
        row = await self._run(self._one, "SELECT value FROM settings WHERE key=?", (key,))
        return default if row is None else row["value"]

    async def set_setting(self, key: str, value: Any) -> None:
        await self._run(self._set_setting, key, value)

    def _set_setting(self, key: str, value: Any) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    async def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(await self.setting(key, default))
        except (TypeError, ValueError):
            return default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(await self.setting(key, default))
        except (TypeError, ValueError):
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        v = str(await self.setting(key, int(default))).lower()
        return v in {"1", "true", "yes", "on"}

    async def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None, last_name: str | None, referrer_id: int | None = None) -> tuple[dict, bool]:
        return await self._run(self._upsert_user, telegram_id, username, first_name, last_name, referrer_id)

    def _upsert_user(self, telegram_id, username, first_name, last_name, referrer_id):
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self.tx() as c:
            row = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            created = False
            if row is None:
                c.execute("INSERT INTO users(telegram_id,username,first_name,last_name,referrer_id,created_at,last_active) VALUES (?,?,?,?,?,?,?)",
                          (telegram_id, username, first_name, last_name, referrer_id, now, now))
                row = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
                created = True
            else:
                c.execute("UPDATE users SET username=?,first_name=?,last_name=?,last_active=? WHERE telegram_id=?", (username, first_name, last_name, now, telegram_id))
                row = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            return dict(row), created

    async def get_user(self, telegram_id: int) -> dict | None:
        row = await self._run(self._one, "SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        return dict(row) if row else None

    async def get_user_by_db_id(self, user_id: int) -> dict | None:
        row = await self._run(self._one, "SELECT * FROM users WHERE id=?", (user_id,))
        return dict(row) if row else None

    async def set_phone(self, telegram_id: int, phone: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        await self._run(self._set_phone, telegram_id, phone, now)

    def _set_phone(self, telegram_id, phone, now):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE users SET phone_number=?,phone_shared_at=?,last_active=? WHERE telegram_id=?", (phone, now, now, telegram_id))

    async def is_banned(self, telegram_id: int) -> bool:
        u = await self.get_user(telegram_id)
        return bool(u and u["is_banned"])

    async def add_coins(self, telegram_id: int, amount: int, reason: str, related_id: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        return await self._run(self._coin_change, telegram_id, amount, reason, related_id, False)

    async def remove_coins(self, telegram_id: int, amount: int, reason: str, related_id: str | None = None) -> int | None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        return await self._run(self._coin_change, telegram_id, -amount, reason, related_id, True)

    def _coin_change(self, telegram_id, delta, reason, related_id, require_funds):
        with self.tx() as c:
            row = c.execute("SELECT id,balance,total_earned,total_spent FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not row:
                raise ValueError("user not found")
            if require_funds and row["balance"] < -delta:
                return None
            new_balance = row["balance"] + delta
            earned = row["total_earned"] + max(delta, 0)
            spent = row["total_spent"] + max(-delta, 0)
            c.execute("UPDATE users SET balance=?,total_earned=?,total_spent=? WHERE telegram_id=?", (new_balance, earned, spent, telegram_id))
            c.execute("INSERT INTO transactions(user_id,amount,reason,balance_after,related_id,created_at) VALUES (?,?,?,?,?,?)",
                      (row["id"], delta, reason, new_balance, related_id, datetime.utcnow().isoformat(timespec="seconds")))
            return new_balance

    async def charge(self, telegram_id: int, cost: int, reason: str, related_id: str | None = None) -> int | None:
        if cost < 0:
            raise ValueError("cost")
        return await self._run(self._charge, telegram_id, cost, reason, related_id)

    def _charge(self, telegram_id, cost, reason, related_id):
        with self.tx() as c:
            row = c.execute("SELECT id,balance,total_spent FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not row or row["balance"] < cost:
                return None
            new_balance = row["balance"] - cost
            c.execute("UPDATE users SET balance=?,total_spent=total_spent+? WHERE telegram_id=?", (new_balance, cost, telegram_id))
            if cost:
                c.execute("INSERT INTO transactions(user_id,amount,reason,balance_after,related_id,created_at) VALUES (?,?,?,?,?,?)",
                          (row["id"], -cost, reason, new_balance, related_id, datetime.utcnow().isoformat(timespec="seconds")))
            return new_balance

    async def reserve_free_request(self, telegram_id: int, daily_limit: int, day: str) -> bool:
        return await self._run(self._reserve_free_request, telegram_id, daily_limit, day)

    def _reserve_free_request(self, telegram_id, daily_limit, day):
        if daily_limit <= 0:
            return False
        with self.tx() as c:
            row = c.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not row:
                return False
            # Each claim date is unique, so two concurrent callbacks cannot both consume the same free slot.
            now=datetime.utcnow().isoformat(timespec="seconds")
            c.execute("INSERT OR IGNORE INTO daily_claims(user_id,claim_date,used_count,created_at) VALUES(?,?,0,?)",(row["id"],day,now))
            cur=c.execute("UPDATE daily_claims SET used_count=used_count+1 WHERE user_id=? AND claim_date=? AND used_count<?",(row["id"],day,daily_limit))
            return cur.rowcount==1

    async def release_free_request(self, telegram_id: int, day: str) -> None:
        await self._run(self._release_free_request, telegram_id, day)

    def _release_free_request(self, telegram_id, day):
        with self.tx() as c:
            u=c.execute("SELECT id FROM users WHERE telegram_id=?",(telegram_id,)).fetchone()
            if u:
                c.execute("DELETE FROM daily_claims WHERE user_id=? AND claim_date=?",(u["id"],day))

    async def create_request(self, telegram_id: int, chat_id: int, uid: str, cost: int, used_free: bool, success: bool, error_code: str | None = None) -> int:
        return await self._run(self._create_request, telegram_id, chat_id, uid, cost, int(used_free), int(success), error_code)

    def _create_request(self, telegram_id, chat_id, uid, cost, used_free, success, error_code):
        with self.tx() as c:
            u = c.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not u:
                raise ValueError("user not found")
            cur = c.execute("INSERT INTO requests(user_id,chat_id,uid,cost,used_free,success,error_code,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (u["id"], chat_id, uid, cost, used_free, success, error_code, datetime.utcnow().isoformat(timespec="seconds")))
            return int(cur.lastrowid)

    async def refund(self, telegram_id: int, amount: int, reason: str, related_id: str | None = None) -> int:
        return await self.add_coins(telegram_id, amount, reason, related_id)

    async def transactions(self, telegram_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
        u = await self.get_user(telegram_id)
        if not u:
            return []
        rows = await self._run(self._all, "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (u["id"], limit, offset))
        return [dict(r) for r in rows]

    async def request_history(self, telegram_id: int, limit: int = 8, offset: int = 0) -> list[dict]:
        u = await self.get_user(telegram_id)
        if not u:
            return []
        rows = await self._run(self._all, "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (u["id"], limit, offset))
        return [dict(r) for r in rows]

    async def request_count_today(self, telegram_id: int) -> int:
        u = await self.get_user(telegram_id)
        if not u:
            return 0
        day = datetime.utcnow().date().isoformat()
        row = await self._run(self._one, "SELECT COUNT(*) n FROM requests WHERE user_id=? AND substr(created_at,1,10)=?", (u["id"], day))
        return int(row["n"]) if row else 0

    async def attach_referral(self, referred_telegram_id: int, referrer_telegram_id: int) -> bool:
        if referred_telegram_id == referrer_telegram_id:
            return False
        return await self._run(self._attach_referral, referred_telegram_id, referrer_telegram_id)

    def _attach_referral(self, referred_tid, referrer_tid):
        with self.tx() as c:
            ref = c.execute("SELECT id FROM users WHERE telegram_id=?", (referrer_tid,)).fetchone()
            user = c.execute("SELECT id,referrer_id FROM users WHERE telegram_id=?", (referred_tid,)).fetchone()
            if not ref or not user or user["referrer_id"] is not None:
                return False
            c.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref["id"], user["id"]))
            c.execute("INSERT OR IGNORE INTO referrals(referrer_user_id,referred_user_id,created_at) VALUES (?,?,?)", (ref["id"], user["id"], datetime.utcnow().isoformat(timespec="seconds")))
            return True

    async def qualify_referral(self, referred_telegram_id: int, reward: int, min_activity: int) -> tuple[bool, int | None, int | None]:
        return await self._run(self._qualify_referral, referred_telegram_id, reward, min_activity)

    def _qualify_referral(self, referred_tid, reward, min_activity):
        with self.tx() as c:
            u = c.execute("SELECT id,referrer_id FROM users WHERE telegram_id=?", (referred_tid,)).fetchone()
            if not u or not u["referrer_id"]:
                return False, None, None
            r = c.execute("SELECT * FROM referrals WHERE referred_user_id=?", (u["id"],)).fetchone()
            if not r or r["rewarded"]:
                return False, None, None
            count = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND success=1", (u["id"],)).fetchone()["n"]
            if count < min_activity:
                return False, None, None
            c.execute("UPDATE referrals SET qualified=1,rewarded=1,reward=? WHERE id=?", (reward, r["id"]))
            ref = c.execute("SELECT id,balance,total_earned,referral_earnings FROM users WHERE id=?", (r["referrer_user_id"],)).fetchone()
            new_balance = ref["balance"] + reward
            c.execute("UPDATE users SET balance=?,total_earned=total_earned+?,referral_earnings=referral_earnings+? WHERE id=?", (new_balance, reward, reward, ref["id"]))
            c.execute("INSERT INTO transactions(user_id,amount,reason,balance_after,related_id,created_at) VALUES (?,?,?,?,?,?)", (ref["id"], reward, "Referral reward", new_balance, str(r["id"]), datetime.utcnow().isoformat(timespec="seconds")))
            return True, ref["id"], reward

    async def referral_stats(self, telegram_id: int) -> dict:
        u = await self.get_user(telegram_id)
        if not u:
            return {"total": 0, "successful": 0, "earned": 0}
        rows = await self._run(self._all, "SELECT COUNT(*) total,SUM(rewarded) successful,SUM(reward) earned FROM referrals WHERE referrer_user_id=?", (u["id"],))
        r = rows[0]
        return {"total": int(r["total"] or 0), "successful": int(r["successful"] or 0), "earned": int(r["earned"] or 0)}

    async def cache_get(self, uid: str, ttl: int) -> dict | None:
        row = await self._run(self._one, "SELECT payload,fetched_at FROM api_cache WHERE uid=?", (uid,))
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if datetime.utcnow() - fetched > timedelta(seconds=ttl):
                return None
            return json.loads(row["payload"])
        except Exception:
            return None

    async def cache_set(self, uid: str, payload: dict) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        await self._run(self._cache_set, uid, json.dumps(payload, ensure_ascii=False), now)

    def _cache_set(self, uid, payload, now):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO api_cache(uid,payload,fetched_at) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at", (uid, payload, now))

    async def save_group(self, group_id: int, title: str | None, request_cost: int) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        await self._run(self._save_group, group_id, title, request_cost, now)

    def _save_group(self, group_id, title, request_cost, now):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO groups_cfg(group_id,title,request_cost,joined_at,last_used) VALUES(?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET title=excluded.title,last_used=excluded.last_used", (group_id, title, request_cost, now, now))

    async def group_config(self, group_id: int) -> dict | None:
        row = await self._run(self._one, "SELECT * FROM groups_cfg WHERE group_id=?", (group_id,))
        return dict(row) if row else None

    async def set_group(self, group_id: int, enabled: bool | None = None, cost: int | None = None, allowed: str | None = None) -> None:
        cfg = await self.group_config(group_id)
        if not cfg:
            await self.save_group(group_id, None, cost or await self.get_int("request_cost", 2))
            cfg = await self.group_config(group_id)
        vals = (int(enabled), cost, allowed)
        await self._run(self._set_group, group_id, *vals)

    def _set_group(self, group_id, enabled, cost, allowed):
        parts=[]; params=[]
        if enabled is not None: parts.append("enabled=?"); params.append(enabled)
        if cost is not None: parts.append("request_cost=?"); params.append(cost)
        if allowed is not None: parts.append("allowed_commands=?"); params.append(allowed)
        if parts:
            with sqlite3.connect(self.path) as c:
                c.execute(f"UPDATE groups_cfg SET {','.join(parts)} WHERE group_id=?", (*params, group_id))

    async def create_payment(self, telegram_id: int, package_name: str, coin_amount: int, amount: float, method: str, txid: str) -> int:
        return await self._run(self._create_payment, telegram_id, package_name, coin_amount, amount, method, txid)

    def _create_payment(self, telegram_id, package_name, coin_amount, amount, method, txid):
        with sqlite3.connect(self.path) as c:
            u = c.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not u: raise ValueError("user not found")
            cur = c.execute("INSERT INTO payments(user_id,package_name,coin_amount,amount,method,transaction_id,status,created_at) VALUES (?,?,?,?,?,?,?,?)", (u[0], package_name, coin_amount, amount, method, txid, "pending", datetime.utcnow().isoformat(timespec="seconds")))
            return int(cur.lastrowid)

    async def pending_payments(self, limit: int = 20) -> list[dict]:
        rows = await self._run(self._all, "SELECT p.*,u.telegram_id,u.username FROM payments p JOIN users u ON u.id=p.user_id WHERE p.status='pending' ORDER BY p.id ASC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def review_payment(self, payment_id: int, admin_id: int, approve: bool) -> dict | None:
        return await self._run(self._review_payment, payment_id, admin_id, approve)

    def _review_payment(self, payment_id, admin_id, approve):
        with self.tx() as c:
            p = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            if not p or p["status"] != "pending":
                return None
            status = "approved" if approve else "rejected"
            now = datetime.utcnow().isoformat(timespec="seconds")
            c.execute("UPDATE payments SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", (status, admin_id, now, payment_id))
            if approve:
                u = c.execute("SELECT balance,total_earned FROM users WHERE id=?", (p["user_id"],)).fetchone()
                new_balance = u["balance"] + p["coin_amount"]
                c.execute("UPDATE users SET balance=?,total_earned=total_earned+? WHERE id=?", (new_balance, p["coin_amount"], p["user_id"]))
                c.execute("INSERT INTO transactions(user_id,amount,reason,balance_after,related_id,created_at) VALUES (?,?,?,?,?,?)", (p["user_id"], p["coin_amount"], f"Payment #{payment_id}", new_balance, str(payment_id), now))
            return dict(p) | {"status": status}

    async def search_users(self, query: str, limit: int = 10) -> list[dict]:
        q = query.strip().lstrip("@").lower()
        if q.isdigit():
            rows = await self._run(self._all, "SELECT * FROM users WHERE telegram_id=? LIMIT ?", (int(q), limit))
        else:
            rows = await self._run(self._all, "SELECT * FROM users WHERE lower(username)=? OR lower(first_name) LIKE ? OR lower(last_name) LIKE ? ORDER BY id DESC LIMIT ?", (q, f"%{q}%", f"%{q}%", limit))
        return [dict(r) for r in rows]

    async def set_ban(self, telegram_id: int, banned: bool) -> None:
        await self._run(lambda: self._execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (int(banned), telegram_id)))

    def _execute(self, sql, params=()):
        with sqlite3.connect(self.path) as c:
            c.execute(sql, params)

    async def stats(self) -> dict:
        rows = await self._run(self._all, "SELECT (SELECT COUNT(*) FROM users) users,(SELECT COUNT(*) FROM requests) requests,(SELECT SUM(success) FROM requests) successful,(SELECT SUM(cost) FROM requests WHERE success=1) spent,(SELECT SUM(amount) FROM transactions WHERE amount>0) distributed")
        r=rows[0]
        today=datetime.utcnow().date().isoformat()
        tr=await self._run(self._one,"SELECT COUNT(*) n FROM requests WHERE substr(created_at,1,10)=?",(today,))
        nu=await self._run(self._one,"SELECT COUNT(*) n FROM users WHERE substr(created_at,1,10)=?",(today,))
        return {"users":r["users"] or 0,"requests":r["requests"] or 0,"successful":r["successful"] or 0,"spent":r["spent"] or 0,"distributed":r["distributed"] or 0,"today_requests":tr["n"] or 0,"today_users":nu["n"] or 0}

    async def log_admin(self, admin_id: int | None, action: str, target: str = "", details: str = "") -> None:
        now=datetime.utcnow().isoformat(timespec="seconds")
        await self._run(self._insert_log, admin_id, action, target, details, now)

    def _insert_log(self, admin_id, action, target, details, now):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO admin_logs(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",(admin_id,action,target,details,now))

    async def all_user_ids(self) -> list[int]:
        rows=await self._run(self._all,"SELECT telegram_id FROM users WHERE is_banned=0 ORDER BY id")
        return [int(r["telegram_id"]) for r in rows]

    async def request_analytics(self, days: int | None = None) -> dict:
        clause=""; params=()
        if days is not None:
            clause=" WHERE datetime(created_at)>=datetime('now', ?)"; params=(f"-{int(days)} days",)
        row=await self._run(self._one,f"SELECT COUNT(*) total,SUM(success) successful,COUNT(DISTINCT user_id) users,SUM(cost) spent FROM requests{clause}",params)
        return {"total":row["total"] or 0,"successful":row["successful"] or 0,"users":row["users"] or 0,"spent":row["spent"] or 0}

    async def top_users(self, limit=10) -> list[dict]:
        rows=await self._run(self._all,"SELECT u.telegram_id,u.username,COUNT(r.id) requests FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id ORDER BY requests DESC LIMIT ?",(limit,))
        return [dict(r) for r in rows]

    async def top_uids(self, limit=10) -> list[dict]:
        rows=await self._run(self._all,"SELECT uid,COUNT(*) requests FROM requests GROUP BY uid ORDER BY requests DESC LIMIT ?",(limit,))
        return [dict(r) for r in rows]
