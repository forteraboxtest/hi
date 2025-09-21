import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)
import sqlite3
import requests
import os
from datetime import datetime, timedelta

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_PATH = "botdata.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Users table: id, username, is_paid, subscription_expiry, daily_downloads
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_paid INTEGER DEFAULT 0,
            subscription_expiry TEXT,
            daily_downloads INTEGER DEFAULT 0
        )
    ''')
    # Admin config table for customization
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Helper functions for DB
def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, is_paid, subscription_expiry, daily_downloads FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "is_paid": bool(row[2]),
            "subscription_expiry": row[3],
            "daily_downloads": row[4]
        }
    return None

def add_or_update_user(user_id, username):
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user:
        c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    else:
        c.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def update_user_subscription(user_id, is_paid, expiry_date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_paid=?, subscription_expiry=? WHERE user_id=?", (int(is_paid), expiry_date, user_id))
    conn.commit()
    conn.close()

def increment_daily_download(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET daily_downloads = daily_downloads + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def reset_daily_downloads():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET daily_downloads=0")
    conn.commit()
    conn.close()

def get_admin_config(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM admin_config WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def set_admin_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if get_admin_config(key) is None:
        c.execute("INSERT INTO admin_config (key, value) VALUES (?, ?)", (key, value))
    else:
        c.execute("UPDATE admin_config SET value=? WHERE key=?", (value, key))
    conn.commit()
    conn.close()

# Constants
FREE_DAILY_LIMIT = 5
ADMIN_USER_ID = 5016461081  # Your admin Telegram user ID
BOT_TOKEN = "8356383599:AAH5xQrrUiDz1NXKqJi8-DLC8MDzaP8JT9Y"

# Terabox API URL template
TERABOX_API_URL = "https://weathered-mouse-6d3e.gaurav281833.workers.dev/api?url={}"

# Bot commands and handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_or_update_user(user.id, user.username or "")
    welcome_text = get_admin_config("welcome_text") or (
        "Welcome to Terabox Video Downloader Bot!\n"
        "You can download videos from Terabox and upload here.\n"
        "Free users can download 5 videos daily.\n"
        "Paid users have unlimited downloads.\n"
        "Use /subscribe to get subscription info."
    )
    await update.message.reply_text(welcome_text)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscription_text = get_admin_config("subscription_text") or (
        "To buy a subscription, please contact the owner.\n"
        "Send /contact to get owner contact info."
    )
    await update.message.reply_text(subscription_text)

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact_info = get_admin_config("contact_info") or "Contact owner at @YourTelegramUsername"
    await update.message.reply_text(contact_info)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "/start - Start the bot\n"
        "/subscribe - Subscription info\n"
        "/contact - Contact owner\n"
        "Send a Terabox video link to download and upload."
    )
    await update.message.reply_text(help_text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = get_user(user.id)
    if not user_data:
        add_or_update_user(user.id, user.username or "")
        user_data = get_user(user.id)

    text = update.message.text.strip()
    if not text.startswith("http"):
        await update.message.reply_text("Please send a valid Terabox video link.")
        return

    # Check subscription expiry for paid users
    if user_data["is_paid"]:
        if user_data["subscription_expiry"]:
            expiry = datetime.strptime(user_data["subscription_expiry"], "%Y-%m-%d")
            if expiry < datetime.now():
                update_user_subscription(user.id, False, None)
                user_data["is_paid"] = False
                await update.message.reply_text("Your subscription has expired. You are now a free user.")
    # Check daily limit for free users
    if not user_data["is_paid"]:
        if user_data["daily_downloads"] >= FREE_DAILY_LIMIT:
            await update.message.reply_text(f"You have reached your daily limit of {FREE_DAILY_LIMIT} videos. Please subscribe for unlimited downloads.")
            return

    await update.message.reply_text("Processing your video, please wait...")

    try:
        api_url = TERABOX_API_URL.format(text)
        response = requests.get(api_url)
        response.raise_for_status()
        data = response.json()
        if "url" not in data or "filename" not in data:
            await update.message.reply_text("Failed to get video info from Terabox API.")
            return

        video_url = data["url"]
        filename = data["filename"]

        video_response = requests.get(video_url, stream=True)
        video_response.raise_for_status()

        temp_path = f"temp_{user.id}_{filename}"
        with open(temp_path, "wb") as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        with open(temp_path, "rb") as video_file:
            await update.message.reply_video(video_file, supports_streaming=True)

        os.remove(temp_path)

        if not user_data["is_paid"]:
            increment_daily_download(user.id)

    except Exception as e:
        logger.error(f"Error processing video: {e}")
        await update.message.reply_text("An error occurred while processing your video. Please try again later.")

# Admin commands

async def admin_set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /setwelcome Your welcome message here")
        return
    set_admin_config("welcome_text", text)
    await update.message.reply_text("Welcome message updated.")

async def admin_set_subscription_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /setsubscriptiontext Your subscription info here")
        return
    set_admin_config("subscription_text", text)
    await update.message.reply_text("Subscription info updated.")

async def admin_set_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /setcontactinfo Your contact info here")
        return
    set_admin_config("contact_info", text)
    await update.message.reply_text("Contact info updated.")

async def admin_add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addsub user_id days")
        return
    try:
        user_id = int(context.args[0])
        days = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid user_id or days.")
        return

    user = get_user(user_id)
    if not user:
        await update.message.reply_text("User  not found.")
        return

    now = datetime.now()
    if user["subscription_expiry"]:
        expiry = datetime.strptime(user["subscription_expiry"], "%Y-%m-%d")
        if expiry > now:
            new_expiry = expiry + timedelta(days=days)
        else:
            new_expiry = now + timedelta(days=days)
    else:
        new_expiry = now + timedelta(days=days)

    update_user_subscription(user_id, True, new_expiry.strftime("%Y-%m-%d"))
    await update.message.reply_text(f"Subscription updated for user {user_id} until {new_expiry.strftime('%Y-%m-%d')}.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_paid=1")
    paid_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_paid=0")
    free_users = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"Total users: {total_users}\nPaid users: {paid_users}\nFree users: {free_users}"
    )

# Scheduler to reset daily downloads at midnight
from apscheduler.schedulers.asyncio import AsyncIOScheduler

def schedule_reset_daily_downloads():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(reset_daily_downloads, 'cron', hour=0, minute=0)
    scheduler.start()

# Main function to run bot

def main():
    init_db()
    schedule_reset_daily_downloads()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("contact", contact))

    # Admin commands
    app.add_handler(CommandHandler("setwelcome", admin_set_welcome))
    app.add_handler(CommandHandler("setsubscriptiontext", admin_set_subscription_text))
    app.add_handler(CommandHandler("setcontactinfo", admin_set_contact_info))
    app.add_handler(CommandHandler("addsub", admin_add_subscription))
    app.add_handler(CommandHandler("stats", admin_stats))

    # Handle messages (Terabox links)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()