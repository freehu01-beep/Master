import os
import base64
import sqlite3
import string
import random
import requests
from flask import Flask, request

app = Flask(__name__)

# ========= CONFIG =========
MASTER_BOT_TOKEN = os.environ.get("MASTER_BOT_TOKEN")  # your master bot token
BASE_URL = os.environ.get("BASE_URL")  # e.g. https://your-app.onrender.com
DB_PATH = "bots.db"

TG_API = "https://api.telegram.org/bot{token}/{method}"


# ========= DB HELPERS =========
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # bots table: each hosted clone
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        secret TEXT UNIQUE NOT NULL,
        username TEXT,
        owner_id INTEGER,
        join_channel TEXT    -- optional: channel to force join (@channel or channel username)
    )
    """)

    # files table: stored file entries per bot
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_username TEXT,
        bot_token TEXT,
        file_id TEXT,
        file_type TEXT,
        caption TEXT
    )
    """)

    # users table: users per clone bot (for stats + broadcast)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_username TEXT,
        bot_token TEXT,
        user_id INTEGER
    )
    """)

    conn.commit()
    conn.close()


def rand_secret(length=32):
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


# ========= TELEGRAM HELPERS =========
def tg_post(token: str, method: str, data=None):
    url = TG_API.format(token=token, method=method)
    r = requests.post(url, json=data or {})
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": "invalid json"}


def tg_send(token: str, chat_id: int | str, text: str):
    tg_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text
    })


# ========= MASTER BOT WEBHOOK =========
@app.route("/webhook/master", methods=["POST"])
def master_webhook():
    update = request.get_json(force=True, silent=True) or {}
    msg = update.get("message", {})
    text = msg.get("text", "") or ""
    chat_id = msg.get("chat", {}).get("id")

    if not chat_id:
        return "ok"

    # /start
    if text.startswith("/start"):
        tg_send(
            MASTER_BOT_TOKEN,
            chat_id,
            "👋 Welcome to Clone File Host Manager!\n\n"
            "Commands:\n"
            "/newbot <token>  - Host a new file bot\n"
            "/mybots          - List your bots\n"
            "/mbroadcast <text> - (Owner) Broadcast to all users of all bots\n"
            "/mstats          - Global stats"
        )
        return "ok"

    # /newbot <token>
    if text.startswith("/newbot"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(MASTER_BOT_TOKEN, chat_id, "Usage:\n/newbot <bot_token>")
            return "ok"

        bot_token = parts[1].strip()
        me = tg_post(bot_token, "getMe")
        if not me.get("ok"):
            tg_send(MASTER_BOT_TOKEN, chat_id, "❌ Invalid bot token.")
            return "ok"

        username = me["result"]["username"]
        secret = rand_secret()

        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO bots (token, secret, username, owner_id) VALUES (?, ?, ?, ?)",
                (bot_token, secret, username, chat_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            tg_send(MASTER_BOT_TOKEN, chat_id, "⚠️ This bot is already registered.")
            conn.close()
            return "ok"
        conn.close()

        webhook_url = f"{BASE_URL}/webhook/{secret}"
        set_hook = tg_post(bot_token, "setWebhook", {"url": webhook_url})
        if not set_hook.get("ok"):
            tg_send(MASTER_BOT_TOKEN, chat_id, "❌ Failed to set webhook. Check BASE_URL.")
            return "ok"

        tg_send(
            MASTER_BOT_TOKEN,
            chat_id,
            f"✅ @{username} hosted successfully as file clone bot.\n"
            f"Users can send files to it and get t.me links."
        )
        return "ok"

    # /mybots
    if text.startswith("/mybots"):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT username, join_channel FROM bots WHERE owner_id=?", (chat_id,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            tg_send(MASTER_BOT_TOKEN, chat_id, "You don't have any hosted bots yet.")
        else:
            msg_lines = ["🤖 Your hosted bots:"]
            for r in rows:
                jc = r["join_channel"]
                if jc:
                    msg_lines.append(f"• @{r['username']} (join: @{jc})")
                else:
                    msg_lines.append(f"• @{r['username']} (no join requirement)")
            tg_send(MASTER_BOT_TOKEN, chat_id, "\n".join(msg_lines))
        return "ok"

    # /mbroadcast <text>  (MASTER BROADCAST to all users of all bots)
    if text.startswith("/mbroadcast"):
        # Only you should use this – you can also restrict by chat_id manually if you want
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(MASTER_BOT_TOKEN, chat_id, "Usage:\n/mbroadcast <message>")
            return "ok"

        b_text = parts[1]
        tg_send(MASTER_BOT_TOKEN, chat_id, "🚀 Starting master broadcast...")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT bot_username, bot_token, user_id FROM users")
        rows = cur.fetchall()
        conn.close()

        sent = 0
        for r in rows:
            try:
                tg_send(r["bot_token"], r["user_id"], b_text)
                sent += 1
            except Exception:
                pass

        tg_send(MASTER_BOT_TOKEN, chat_id, f"✅ Master broadcast sent to {sent} users.")
        return "ok"

    # /mstats – global stats
    if text.startswith("/mstats"):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM bots")
        bots_c = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM users")
        users_c = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM files")
        files_c = cur.fetchone()["c"]

        conn.close()

        tg_send(
            MASTER_BOT_TOKEN,
            chat_id,
            f"📊 Global Stats:\n"
            f"• Hosted bots: {bots_c}\n"
            f"• Total users: {users_c}\n"
            f"• Total files: {files_c}"
        )
        return "ok"

    # default
    tg_send(MASTER_BOT_TOKEN, chat_id, "Unknown command. Use /start to see options.")
    return "ok"


# ========= CLONE BOT WEBHOOK =========
@app.route("/webhook/<secret>", methods=["POST"])
def clone_webhook(secret):
    # identify bot from secret
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bots WHERE secret=?", (secret,))
    bot = cur.fetchone()
    if not bot:
        conn.close()
        return "ok"

    bot_token = bot["token"]
    bot_username = bot["username"]
    owner_id = bot["owner_id"]
    join_channel = bot["join_channel"]  # may be None
    conn.close()

    update = request.get_json(force=True, silent=True) or {}
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "") or ""

    if not chat_id:
        return "ok"

    # Track user (for stats + broadcast)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM users WHERE bot_username=? AND user_id=?",
        (bot_username, chat_id)
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (bot_username, bot_token, user_id) VALUES (?, ?, ?)",
            (bot_username, bot_token, chat_id)
        )
        conn.commit()
    conn.close()

    # ===== Owner-only commands (via clone bot) =====
    is_owner = (chat_id == owner_id)

    # /setchannel @ChannelUsername
    if text.startswith("/setchannel") and is_owner:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(bot_token, chat_id, "Usage:\n/setchannel @YourChannelUsername")
            return "ok"
        chan = parts[1].strip()
        if chan.startswith("@"):
            chan = chan[1:]

        conn = get_db()
        conn.execute("UPDATE bots SET join_channel=? WHERE username=?", (chan, bot_username))
        conn.commit()
        conn.close()

        tg_send(bot_token, chat_id, f"✅ Join channel set to: @{chan}\nUsers must join this channel to get files.")
        return "ok"

    # /clearchannel
    if text.startswith("/clearchannel") and is_owner:
        conn = get_db()
        conn.execute("UPDATE bots SET join_channel=NULL WHERE username=?", (bot_username,))
        conn.commit()
        conn.close()
        tg_send(bot_token, chat_id, "✅ Join channel requirement cleared.")
        return "ok"

    # /channel – show join channel
    if text.startswith("/channel") and is_owner:
        if join_channel:
            tg_send(bot_token, chat_id, f"Current join channel: @{join_channel}")
        else:
            tg_send(bot_token, chat_id, "No join channel set.")
        return "ok"

    # /broadcast <text> (owner → all users of this clone)
    if text.startswith("/broadcast") and is_owner:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            tg_send(bot_token, chat_id, "Usage:\n/broadcast <message>")
            return "ok"

        b_text = parts[1]
        tg_send(bot_token, chat_id, "📢 Starting broadcast to your bot users...")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM users WHERE bot_username=?", (bot_username,))
        rows = cur.fetchall()
        conn.close()

        sent = 0
        for r in rows:
            try:
                tg_send(bot_token, r["user_id"], b_text)
                sent += 1
            except Exception:
                pass

        tg_send(bot_token, chat_id, f"✅ Broadcast sent to {sent} users.")
        return "ok"

    # /stats – basic stats for this clone
    if text.startswith("/stats") and is_owner:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE bot_username=?", (bot_username,))
        u_c = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM files WHERE bot_username=?", (bot_username,))
        f_c = cur.fetchone()["c"]
        conn.close()
        jc_text = f"@{join_channel}" if join_channel else "none"
        tg_send(
            bot_token,
            chat_id,
            f"📊 Bot Stats (@{bot_username}):\n"
            f"• Users: {u_c}\n"
            f"• Files: {f_c}\n"
            f"• Join Channel: {jc_text}"
        )
        return "ok"

    # ===== /start with payload (file link) =====
    if text.startswith("/start") and len(text.split()) > 1:
        payload = text.split(maxsplit=1)[1].strip()
        # Decode file row ID
        try:
            decoded = base64.urlsafe_b64decode(payload.encode()).decode()
            file_row_id = int(decoded)
        except Exception:
            tg_send(bot_token, chat_id, "❌ Invalid or broken link.")
            return "ok"

        # If join channel is set → check membership
        if join_channel:
            check = tg_post(bot_token, "getChatMember", {
                "chat_id": f"@{join_channel}",
                "user_id": chat_id
            })
            if not check.get("ok") or check["result"]["status"] in ["left", "kicked"]:
                # Not joined
                tg_send(
                    bot_token,
                    chat_id,
                    "🚫 You must join our channel to access this file:\n"
                    f"https://t.me/{join_channel}\n\n"
                    "After joining, open this link again."
                )
                return "ok"

        # Fetch file data
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM files WHERE id=? AND bot_username=?", (file_row_id, bot_username))
        f_row = cur.fetchone()
        conn.close()

        if not f_row:
            tg_send(bot_token, chat_id, "❌ File not found or removed.")
            return "ok"

        file_id = f_row["file_id"]
        file_type = f_row["file_type"]
        caption = f_row["caption"] or ""

        # Send with forwarding restricted (protect_content=True)
        if file_type == "document":
            tg_post(bot_token, "sendDocument", {
                "chat_id": chat_id,
                "document": file_id,
                "caption": caption,
                "protect_content": True
            })
        elif file_type == "photo":
            tg_post(bot_token, "sendPhoto", {
                "chat_id": chat_id,
                "photo": file_id,
                "caption": caption,
                "protect_content": True
            })
        elif file_type == "video":
            tg_post(bot_token, "sendVideo", {
                "chat_id": chat_id,
                "video": file_id,
                "caption": caption,
                "protect_content": True
            })
        else:
            # fallback as document
            tg_post(bot_token, "sendDocument", {
                "chat_id": chat_id,
                "document": file_id,
                "caption": caption,
                "protect_content": True
            })
        return "ok"

    # ===== File upload flow (user sends file) =====
    if "document" in msg or "photo" in msg or "video" in msg:
        caption = msg.get("caption", "") or ""

        if "document" in msg:
            file_type = "document"
            file_obj = msg["document"]
        elif "photo" in msg:
            file_type = "photo"
            file_obj = msg["photo"][-1]
        else:
            file_type = "video"
            file_obj = msg["video"]

        file_id = file_obj["file_id"]

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (bot_username, bot_token, file_id, file_type, caption) VALUES (?, ?, ?, ?, ?)",
            (bot_username, bot_token, file_id, file_type, caption)
        )
        conn.commit()
        file_row_id = cur.lastrowid
        conn.close()

        encoded = base64.urlsafe_b64encode(str(file_row_id).encode()).decode()
        link = f"https://t.me/{bot_username}?start={encoded}"

        tg_send(
            bot_token,
            chat_id,
            "✅ File saved!\n"
            f"🔗 Share link:\n{link}\n\n"
            "Anyone who opens this link (and joins required channels) will get the file."
        )
        return "ok"

    # ===== Plain /start (no payload) =====
    if text.startswith("/start"):
        tg_send(
            bot_token,
            chat_id,
            f"👋 I'm @{bot_username}, a file sharing bot.\n"
            "Send me any file and I'll give you a shareable link.\n\n"
            "Owners commands:\n"
            "/setchannel @Channel  - set join channel\n"
            "/clearchannel         - remove join lock\n"
            "/channel              - show channel\n"
            "/broadcast <text>     - send message to all users\n"
            "/stats                - show stats"
        )
        return "ok"

    # Default message to clone
    tg_send(bot_token, chat_id, "📁 Send me a file to get a link.")
    return "ok"


# ========= BASIC HOME ROUTE =========
@app.route("/")
def home():
    return "✅ Clone File Host Manager is running."


# Initialize DB on import (for gunicorn)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
