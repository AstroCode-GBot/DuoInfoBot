# Duo Info Telegram Bot

A production-ready Telegram Bot built using Python, MongoDB Atlas, and the direct Telegram Bot API to query Free Fire Duo Information.

## Features

- **Button-First UI**: Uses Telegram premium custom emojis and custom inline keypads.
- **Group Support**: Responds in groups via `/duo <UID>` commands.
- **Coin & Referral System**: Daily free requests, paid requests with coin deduction on success, and referral bonuses.
- **Admin Control Panel**: View user statistics, manage user coin balances, adjust global costs, broadcast messages, export CSV logs, and ban users.
- **Persistent Data**: MongoDB Atlas database integration.
- **Render Ready**: Includes a Flask health endpoint and `render.yaml` configuration.

---

## Environment Variables Configuration

Set these environment variables on Render:

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `BOT_TOKEN` | **Yes** | - | Telegram Bot Token from @BotFather |
| `ADMIN_ID` | **Yes** | - | Primary Telegram User ID for the bot owner |
| `MONGO_URI` | **Yes** | - | MongoDB Atlas Connection String |
| `MONGO_DB_NAME` | No | `duo_info_bot` | Database Name |
| `DUO_API_URL` | No | `https://duoinfo.onrender.com/api/duo` | Duo API Endpoint |
| `LOG_GROUP_ID` | No | - | Group ID for audit logging (e.g., `-100123456789`) |
| `FORCE_SUB_CHANNEL`| No | - | Channel username for mandatory join requirement (e.g., `@mychannel`) |
| `SUPPORT_URL` | No | `https://t.me/telegram` | Telegram support link |
| `TIMEZONE` | No | `Asia/Dhaka` | Timezone for daily request resets |
| `REQUEST_COST` | No | `1` | Coins required per paid request |
| `DAILY_FREE_REQUESTS`| No | `1` | Free requests per user per day |
| `REFERRAL_REWARD` | No | `5` | Coins rewarded per successful referral |

---

## Deployment Steps

### 1. Create Bot & Database
1. Create a bot using [@BotFather](https://t.me/BotFather) and copy the Bot Token.
2. Create a cluster on [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) and get your connection string (`MONGO_URI`).

### 2. Deploy on Render
1. Push this repository to GitHub.
2. Create a new **Web Service** on [Render](https://render.com).
3. Connect your repository.
4. Set **Runtime** to `Python 3`.
5. Set **Build Command**: `pip install -r requirements.txt`
6. Set **Start Command**: `python main.py`
7. Add the environment variables listed above.
8. Click **Deploy Web Service**.
