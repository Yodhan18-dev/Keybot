"""
Panel Key Manager Bot
python-telegram-bot v20+ | SQLite | Render background worker
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
BOT_TOKEN = "8921480391:AAHLZj8m_Ty8VFWyWkrHeWVQ3_bCDQkErO4"
ADMIN_ID   = 8373593477

DB_PATH = Path("data/bot.db")
KEY_TTL = timedelta(hours=24)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────
def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS keys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT NOT NULL,
                added_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS used_keys (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                key            TEXT NOT NULL,
                user_id        INTEGER,
                assigned_time  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                key_added_time TIMESTAMP
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ──────────────────────────────────────────────
# Utility: time helpers
# ──────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime:
    """Return an aware UTC datetime from whatever SQLite gives back."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # string
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {value!r}")


def fmt_remaining(expiry: datetime) -> str:
    delta = expiry - utcnow()
    if delta.total_seconds() <= 0:
        return "expired"
    total_secs = int(delta.total_seconds())
    h, rem = divmod(total_secs, 3600)
    m, _   = divmod(rem, 60)
    return f"{h}h {m}m"


# ──────────────────────────────────────────────
# DB query helpers
# ──────────────────────────────────────────────
def is_authorised(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM authorized_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def add_authorised_user(user_id: int, username: str, first_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO authorized_users (user_id, username, first_name)
               VALUES (?, ?, ?)""",
            (user_id, username, first_name),
        )


def remove_authorised_user(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM authorized_users WHERE user_id = ?", (user_id,))


def get_all_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM authorized_users").fetchall()


def add_key(key_str: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO keys (key, added_time) VALUES (?, ?)",
            (key_str, utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )


def get_active_key_for_user(user_id: int):
    """Return the used_key row if the user has an active (non-expired) key."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM used_keys WHERE user_id = ? ORDER BY assigned_time DESC LIMIT 1",
            (user_id,),
        ).fetchall()
    if not rows:
        return None
    row = rows[0]
    added = parse_ts(row["key_added_time"])
    expiry = added + KEY_TTL
    if utcnow() < expiry:
        return row
    return None


def assign_key_to_user(user_id: int):
    """
    Pick the oldest unassigned & unexpired key, assign it, remove from keys table.
    Returns (key_string, expiry_datetime) or (None, None).
    """
    now = utcnow()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM keys ORDER BY added_time ASC"
        ).fetchall()

        for row in rows:
            added = parse_ts(row["added_time"])
            expiry = added + KEY_TTL
            if now < expiry:                      # still valid
                # assign
                conn.execute(
                    """INSERT INTO used_keys (key, user_id, assigned_time, key_added_time)
                       VALUES (?, ?, ?, ?)""",
                    (
                        row["key"],
                        user_id,
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        row["added_time"],
                    ),
                )
                conn.execute("DELETE FROM keys WHERE id = ?", (row["id"],))
                return row["key"], expiry

    return None, None


def get_stock_keys():
    """Return unexpired keys still in stock."""
    now = utcnow()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM keys ORDER BY added_time ASC").fetchall()
    valid = []
    for row in rows:
        added  = parse_ts(row["added_time"])
        expiry = added + KEY_TTL
        if now < expiry:
            valid.append((row, expiry))
    return valid


def get_used_today():
    today = utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM used_keys WHERE assigned_time LIKE ?", (f"{today}%",)
        ).fetchall()
    return rows


def revoke_user_key(user_id: int) -> bool:
    """Delete the most recent active used_key for user. Returns True if found."""
    row = get_active_key_for_user(user_id)
    if not row:
        return False
    with get_conn() as conn:
        conn.execute("DELETE FROM used_keys WHERE id = ?", (row["id"],))
    return True


# ──────────────────────────────────────────────
# Decorators
# ──────────────────────────────────────────────
def restrict_to_admin(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Unauthorised.")
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


def require_auth(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not is_authorised(update.effective_user.id):
            await update.message.reply_text(
                "🚫 You are not authorised.\n\n"
                "To take a valid plan, contact us:\n"
                "📞 Phone/WhatsApp: 9502183889\n"
                "📸 Instagram: @Yodhan_18\n\n"
                "Once you have a plan, use /request to apply for access."
            )
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────
# User handlers
# ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if is_authorised(user.id):
        await update.message.reply_text(
            f"👋 Welcome back, {user.first_name}!\n"
            "Use /getkey to get your panel key.\n"
            "Use /mykey to check your current key."
        )
    else:
        await update.message.reply_text(
            "👋 Hello! You are *not* authorised yet.\n\n"
            "To take a valid plan, contact us:\n"
            "📞 Phone/WhatsApp: 9502183889\n"
            "📸 Instagram: @Yodhan_18\n\n"
            "Once you have a plan, use /request to apply for access.",
            parse_mode="Markdown",
        )


async def cmd_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if is_authorised(user.id):
        await update.message.reply_text("✅ You are already authorised! Use /getkey.")
        return

    # Notify admin
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user.id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{user.id}"),
        ]
    ])

    text = (
        f"📬 *New Access Request*\n\n"
        f"👤 Name: {user.first_name}\n"
        f"🪪 Username: @{user.username or 'N/A'}\n"
        f"🆔 ID: `{user.id}`"
    )

    await ctx.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    await update.message.reply_text(
        "📨 Your request has been sent to the admin. Please wait for approval."
    )


async def callback_approve_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query  = update.callback_query
    admin  = query.from_user

    if admin.id != ADMIN_ID:
        await query.answer("⛔ Unauthorised.", show_alert=True)
        return

    action, uid_str = query.data.split(":", 1)
    target_id = int(uid_str)

    if action == "approve":
        # We don't have username/first_name handy – store what we can
        # (they will be updated next time user interacts)
        try:
            chat = await ctx.bot.get_chat(target_id)
            uname  = chat.username   or ""
            fname  = chat.first_name or ""
        except Exception:
            uname, fname = "", ""

        add_authorised_user(target_id, uname, fname)
        await ctx.bot.send_message(
            chat_id=target_id,
            text="🎉 You are now authorised! Use /getkey to receive your panel key.",
        )
        await query.edit_message_text(
            query.message.text + "\n\n✅ *Approved*", parse_mode="Markdown"
        )

    elif action == "reject":
        await ctx.bot.send_message(
            chat_id=target_id,
            text="❌ Your access request was denied. Contact support for more info.",
        )
        await query.edit_message_text(
            query.message.text + "\n\n❌ *Rejected*", parse_mode="Markdown"
        )

    await query.answer()


@require_auth
async def cmd_getkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Check for existing active key
    active = get_active_key_for_user(user.id)
    if active:
        added  = parse_ts(active["key_added_time"])
        expiry = added + KEY_TTL
        remaining = fmt_remaining(expiry)
        await update.message.reply_text(
            f"🔑 Your current key:\n`{active['key']}`\n\n"
            f"⏳ Expires in: *{remaining}*",
            parse_mode="Markdown",
        )
        return

    # Assign a new key
    key_str, expiry = assign_key_to_user(user.id)
    if key_str is None:
        await update.message.reply_text(
            "⚠️ No valid keys available right now. Please try again later."
        )
        return

    remaining = fmt_remaining(expiry)
    await update.message.reply_text(
        f"🔑 Your key:\n`{key_str}`\n\n"
        f"⏳ Expires in: *{remaining}*",
        parse_mode="Markdown",
    )


@require_auth
async def cmd_mykey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user   = update.effective_user
    active = get_active_key_for_user(user.id)

    if not active:
        await update.message.reply_text(
            "❌ You don't have an active key. Use /getkey to get one."
        )
        return

    added     = parse_ts(active["key_added_time"])
    expiry    = added + KEY_TTL
    remaining = fmt_remaining(expiry)
    await update.message.reply_text(
        f"🔑 Your active key:\n`{active['key']}`\n\n"
        f"⏳ Expires in: *{remaining}*",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# Admin handlers
# ──────────────────────────────────────────────
@restrict_to_admin
async def cmd_addkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /addkey <key1> [key2] ...")
        return

    added = 0
    for key_str in ctx.args:
        key_str = key_str.strip()
        if key_str:
            add_key(key_str)
            added += 1

    await update.message.reply_text(f"✅ Added {added} key(s) to stock.")


@restrict_to_admin
async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    stock = get_stock_keys()
    if not stock:
        await update.message.reply_text("📭 No keys in stock.")
        return

    lines = ["📋 *Keys in stock:*\n"]
    for i, (row, expiry) in enumerate(stock, 1):
        remaining = fmt_remaining(expiry)
        lines.append(f"{i}. `{row['key']}` — ⏳ {remaining}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restrict_to_admin
async def cmd_used(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = get_used_today()
    if not rows:
        await update.message.reply_text("📭 No keys given out today.")
        return

    lines = [f"📊 *Keys given out today: {len(rows)}*\n"]
    for row in rows:
        lines.append(
            f"• `{row['key']}` → user `{row['user_id']}` at {row['assigned_time']}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restrict_to_admin
async def cmd_members(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    users = get_all_users()
    if not users:
        await update.message.reply_text("👥 No authorised members yet.")
        return

    lines = [f"👥 *Authorised members ({len(users)}):*\n"]
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "N/A"
        lines.append(f"• `{u['user_id']}` — {u['first_name']} ({uname})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restrict_to_admin
async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return

    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    revoke_user_key(target_id)          # invalidate active key
    remove_authorised_user(target_id)

    await update.message.reply_text(
        f"✅ User `{target_id}` removed and their active key revoked.",
        parse_mode="Markdown",
    )
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text="⛔ Your access has been revoked. Contact support for more info.",
        )
    except Exception:
        pass


@restrict_to_admin
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    message = " ".join(ctx.args)
    users   = get_all_users()
    sent, failed = 0, 0

    for user in users:
        try:
            await ctx.bot.send_message(chat_id=user["user_id"], text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


@restrict_to_admin
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    stock   = get_stock_keys()
    used_td = get_used_today()

    with get_conn() as conn:
        total_added = conn.execute("SELECT COUNT(*) FROM used_keys").fetchone()[0]
        total_added += len(stock)           # still in stock + already given
        # more accurate: total ever inserted = stock + used_keys
        total_ever = conn.execute("SELECT COUNT(*) FROM used_keys").fetchone()[0] + len(stock)

    await update.message.reply_text(
        f"📊 *Stats*\n\n"
        f"🗂 Total keys added (ever): `{total_ever}`\n"
        f"📦 Keys remaining in stock: `{len(stock)}`\n"
        f"📤 Keys given out today: `{len(used_td)}`",
        parse_mode="Markdown",
    )


@restrict_to_admin
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return

    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    revoked = revoke_user_key(target_id)
    if revoked:
        await update.message.reply_text(
            f"✅ Active key for user `{target_id}` has been revoked.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"ℹ️ User `{target_id}` has no active key to revoke.",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main() -> None:
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("request", cmd_request))
    app.add_handler(CommandHandler("getkey",  cmd_getkey))
    app.add_handler(CommandHandler("mykey",   cmd_mykey))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_approve_reject, pattern=r"^(approve|reject):\d+$"))

    # Admin commands
    app.add_handler(CommandHandler("addkey",     cmd_addkey))
    app.add_handler(CommandHandler("keys",       cmd_keys))
    app.add_handler(CommandHandler("used",       cmd_used))
    app.add_handler(CommandHandler("members",    cmd_members))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("revoke",     cmd_revoke))

    logger.info("Bot starting — polling…")
    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
