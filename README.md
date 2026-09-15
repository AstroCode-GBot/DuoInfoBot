# FFDuoInfoBot

Production-oriented Telegram Duo Info bot using exactly **5 Python source files**:

- `main.py` — handlers, workflows, FSM, admin/group logic
- `config.py` — environment config + emoji helpers
- `database.py` — SQLite schema, atomic economy, referrals, payments, analytics/logs
- `api.py` — async Duo API client, validation, retry/timeout, normalization/cache adapter
- `keyboards.py` — inline/reply keyboards

`requirements.txt`, `.env.example`, and this README are not source-code modules.

## 1. Security first

The Telegram bot token pasted into the original specification is intentionally **not** embedded anywhere in this project. Because a bot token was exposed in chat, rotate/revoke it in **@BotFather** before deployment and put the replacement in `BOT_TOKEN`.

Never commit `.env`, the SQLite database, payment credentials, or secrets.

## 2. Install

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `BOT_TOKEN`, `ADMIN_IDS`, and (recommended) `ADMIN_LOG_CHAT_ID`.

## 3. Run

```bash
python main.py
```

The SQLite database is created automatically on first start.

## 4. Main commands

`/start`, `/help`, `/duo UID`, `/checkduo UID`, `/balance`, `/referral`, `/stats`, `/history`

Admins additionally have `/admin`, `/searchuser ID_OR_USERNAME`, `/addcoins USER_ID AMOUNT`, `/removecoins USER_ID AMOUNT`, `/ban USER_ID`, `/unban USER_ID`, `/broadcast MESSAGE`, `/broadcastconfirm`, `/broadcastcancel`, `/health`.

## 5. Admin settings

Open `/admin` → **Settings**. Numeric settings can be edited from Telegram. Boolean switches toggle directly. Changes are persisted in SQLite and survive restarts.

Payment packages are stored in `settings.packages_json`. Payment method numbers are stored as `bkash_number` and `nagad_number`.

For initial package setup, the database seeds:

```json
{"10":{"coins":10,"price":10},"25":{"coins":25,"price":20},"60":{"coins":60,"price":45}}
```

Change these values in the database/settings layer before live use if your pricing differs.

## 6. Group use

Add the bot to a group and use:

```text
/duo 824648540
```

Global group usage is controlled by `group_enabled`. A group gets its own request cost in `groups_cfg` and does not gain global admin privileges just because a Telegram member is a group admin.

## 7. Deployment

### Render / Railway / VPS

Use a Python worker/service with:

```text
Build: pip install -r requirements.txt
Start: python main.py
```

For persistent SQLite, attach a persistent disk/volume. Without persistent storage, the database will be lost on redeploy/restart.

For higher scale than a single-instance SQLite worker, move the database/economy layer to PostgreSQL and keep the same 5-file architecture.

## 8. Manual payment workflow

1. User chooses package.
2. User chooses bKash/Nagad.
3. Bot shows the configured payment destination.
4. User submits transaction ID.
5. Payment is stored as `pending`.
6. Admin reviews it from `/admin` → Payments.
7. Coins are credited only after approval.

There is no automatic trust of a submitted transaction ID.

## 9. Important implementation notes

- UID input is numeric-only and length-limited.
- API requests use `urlencode`, timeouts, retries, safe JSON validation, and short-lived SQLite caching.
- Paid balance is deducted atomically and cannot go negative.
- Daily free claims use an atomic per-user/per-day counter.
- Failed paid API calls are refunded.
- Failed free API calls release the free claim.
- Admin callbacks re-check `ADMIN_IDS` server-side.
- User-submitted phone numbers are stored only when Telegram contact sharing is explicitly used.
- Sensitive credentials are never sent to the admin log group.
- Maintenance mode bypass is available to admins so the panel remains usable.

## 10. Test checklist

Before production, manually test:

- `/start`, menu navigation, UID validation, successful Duo lookup, API timeout/failure, cache hit
- free request, multiple daily free requests, insufficient coins, atomic double-spend
- referral deep link, duplicate/self referral, reward qualification
- history/stats
- admin authorization and unauthorized callback handling
- add/remove coins, ban/unban, settings persistence, payment approval/rejection
- broadcast preview/send/failures
- group `/duo UID`, group disable, group-specific cost
- voluntary contact sharing

## 11. Current verification

The Python files were syntax-compiled successfully and the SQLite economy/free-request tests passed in the build environment. Full aiogram runtime import could not be executed there because that environment had no network access to install external packages; the dependency versions are pinned in `requirements.txt`.
