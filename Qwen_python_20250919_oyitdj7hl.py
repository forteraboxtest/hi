import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
import requests
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Configuration
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Replace with your bot token
ADMIN_USER_ID = 123456789  # Replace with admin's Telegram user ID
TERABOX_API_URL = "https://weathered-mouse-6d3e.gaurav281833.workers.dev/api?url={}"

# File to store user data
USER_DATA_FILE = "user_data.json"
BOT_CONFIG_FILE = "bot_config.json"

# Default bot configuration
DEFAULT_BOT_CONFIG = {
    "bot_name": "Terabox Downloader Pro",
    "free_limit": 5,
    "welcome_message": "Welcome to {bot_name}! 🎬\n\nSend me a Terabox link to download videos.\nFree users: {free_limit} downloads per day\nPaid users: Unlimited downloads",
    "subscription_message": "💎 Subscription Plans 💎\n\nNo automatic payment system.\n\nTo purchase a subscription, contact the bot owner: @{owner_username}\n\nSubscription durations:\n- Daily: 24 hours\n- Monthly: 30 days\n- Yearly: 365 days",
    "owner_username": "bot_owner",  # Admin should change this
    "video_size_limit": 2000,  # MB - Telegram limit is ~2GB, but we'll set lower for reliability
    "download_timeout": 300,  # 5 minutes
}

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class UserDataManager:
    """Manage user data including subscriptions and download counts"""
    
    def __init__(self, file_path=USER_DATA_FILE):
        self.file_path = file_path
        self.users = self.load_users()
    
    def load_users(self):
        """Load user data from file"""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    return json.load(f)
            else:
                return {}
        except Exception as e:
            logger.error(f"Error loading user data: {e}")
            return {}
    
    def save_users(self):
        """Save user data to file"""
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.users, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving user data: {e}")
    
    def get_user(self, user_id):
        """Get user data by ID"""
        str_user_id = str(user_id)
        if str_user_id not in self.users:
            self.users[str_user_id] = {
                "id": user_id,
                "username": "",
                "first_name": "",
                "is_paid": False,
                "subscription_end": None,
                "daily_downloads": 0,
                "last_download_date": None,
                "download_history": []
            }
        return self.users[str_user_id]
    
    def update_user_info(self, user_id, username="", first_name=""):
        """Update user info"""
        user = self.get_user(user_id)
        user["username"] = username
        user["first_name"] = first_name
        self.save_users()
    
    def is_paid_user(self, user_id):
        """Check if user has active paid subscription"""
        user = self.get_user(user_id)
        
        if not user["is_paid"]:
            return False
        
        if user["subscription_end"] is None:
            return True  # Lifetime subscription
        
        # Check if subscription is still valid
        if datetime.now() < datetime.fromisoformat(user["subscription_end"]):
            return True
        else:
            # Subscription expired
            user["is_paid"] = False
            user["subscription_end"] = None
            self.save_users()
            return False
    
    def can_download(self, user_id):
        """Check if user can download based on limits"""
        user = self.get_user(user_id)
        
        # Paid users have no daily limit
        if self.is_paid_user(user_id):
            return True, ""
        
        # Check daily limit for free users
        today = datetime.now().strftime("%Y-%m-%d")
        
        if user["last_download_date"] != today:
            user["daily_downloads"] = 0
            user["last_download_date"] = today
        
        config = BotConfigManager().load_config()
        free_limit = config.get("free_limit", 5)
        
        if user["daily_downloads"] >= free_limit:
            return False, f"Daily limit reached ({free_limit} videos/day for free users). Upgrade to paid for unlimited downloads!"
        
        return True, ""
    
    def increment_download_count(self, user_id, video_info=None):
        """Increment user's download count"""
        user = self.get_user(user_id)
        user["daily_downloads"] += 1
        
        if video_info:
            download_record = {
                "timestamp": datetime.now().isoformat(),
                "video_info": video_info
            }
            user["download_history"].append(download_record)
            # Keep only last 100 downloads
            if len(user["download_history"]) > 100:
                user["download_history"] = user["download_history"][-100:]
        
        self.save_users()
    
    def activate_subscription(self, user_id, duration_days, access_key=None):
        """Activate paid subscription for user"""
        user = self.get_user(user_id)
        user["is_paid"] = True
        
        if duration_days > 0:
            end_date = datetime.now() + timedelta(days=duration_days)
            user["subscription_end"] = end_date.isoformat()
        else:
            # Lifetime subscription
            user["subscription_end"] = None
        
        user["access_key_used"] = access_key
        self.save_users()
        return True
    
    def get_user_stats(self, user_id):
        """Get user statistics"""
        user = self.get_user(user_id)
        config = BotConfigManager().load_config()
        free_limit = config.get("free_limit", 5)
        
        stats = {
            "is_paid": self.is_paid_user(user_id),
            "daily_downloads": user["daily_downloads"],
            "free_limit": free_limit,
            "downloads_remaining": max(0, free_limit - user["daily_downloads"]) if not self.is_paid_user(user_id) else "Unlimited",
            "subscription_end": user.get("subscription_end", None),
            "total_downloads": len(user["download_history"])
        }
        
        return stats

class BotConfigManager:
    """Manage bot configuration"""
    
    def __init__(self, file_path=BOT_CONFIG_FILE):
        self.file_path = file_path
        self.config = self.load_config()
    
    def load_config(self):
        """Load bot configuration from file"""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r') as f:
                    return json.load(f)
            else:
                # Create default config
                self.config = DEFAULT_BOT_CONFIG.copy()
                self.save_config()
                return self.config
        except Exception as e:
            logger.error(f"Error loading bot config: {e}")
            return DEFAULT_BOT_CONFIG.copy()
    
    def save_config(self):
        """Save bot configuration to file"""
        try:
            with open(self.file_path, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving bot config: {e}")
    
    def update_config(self, key, value):
        """Update a specific configuration value"""
        self.config[key] = value
        self.save_config()
    
    def get_config(self, key, default=None):
        """Get a specific configuration value"""
        return self.config.get(key, default)

class AccessKeyManager:
    """Manage subscription access keys"""
    
    def __init__(self):
        self.keys_file = "access_keys.json"
        self.keys = self.load_keys()
    
    def load_keys(self):
        """Load access keys from file"""
        try:
            if os.path.exists(self.keys_file):
                with open(self.keys_file, 'r') as f:
                    return json.load(f)
            else:
                return {}
        except Exception as e:
            logger.error(f"Error loading access keys: {e}")
            return {}
    
    def save_keys(self):
        """Save access keys to file"""
        try:
            with open(self.keys_file, 'w') as f:
                json.dump(self.keys, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving access keys: {e}")
    
    def generate_key(self, duration_days, notes=""):
        """Generate a new access key"""
        import secrets
        key = secrets.token_urlsafe(16)
        
        self.keys[key] = {
            "duration_days": duration_days,
            "notes": notes,
            "created_at": datetime.now().isoformat(),
            "used_by": None,
            "used_at": None
        }
        
        self.save_keys()
        return key
    
    def validate_key(self, key):
        """Validate an access key"""
        if key in self.keys and self.keys[key]["used_by"] is None:
            return True, self.keys[key]["duration_days"]
        return False, 0
    
    def use_key(self, key, user_id):
        """Mark a key as used by a user"""
        if key in self.keys and self.keys[key]["used_by"] is None:
            self.keys[key]["used_by"] = user_id
            self.keys[key]["used_at"] = datetime.now().isoformat()
            self.save_keys()
            return True
        return False
    
    def get_all_keys(self):
        """Get all access keys"""
        return self.keys
    
    def delete_key(self, key):
        """Delete an access key"""
        if key in self.keys:
            del self.keys[key]
            self.save_keys()
            return True
        return False

# Initialize managers
user_manager = UserDataManager()
config_manager = BotConfigManager()
key_manager = AccessKeyManager()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    user_manager.update_user_info(user.id, user.username or "", user.first_name or "")
    
    config = config_manager.load_config()
    welcome_message = config.get("welcome_message", DEFAULT_BOT_CONFIG["welcome_message"])
    bot_name = config.get("bot_name", DEFAULT_BOT_CONFIG["bot_name"])
    free_limit = config.get("free_limit", DEFAULT_BOT_CONFIG["free_limit"])
    
    # Format welcome message
    welcome_text = welcome_message.format(
        bot_name=bot_name,
        free_limit=free_limit,
        owner_username=config.get("owner_username", "bot_owner")
    )
    
    # Create keyboard
    keyboard = [
        [InlineKeyboardButton("📥 Download Video", callback_data="download")],
        [InlineKeyboardButton("💎 Subscription", callback_data="subscription")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
🚀 *Terabox Downloader Bot Commands*

📥 *Download*
- Send any Terabox link directly to the bot
- Wait for processing and download

💎 *Subscription*
- Free users: Limited downloads per day
- Paid users: Unlimited downloads
- Contact owner to purchase subscription

📊 *Commands*
- /start - Start the bot
- /help - Show this help message
- /stats - Show your download statistics
- /subscription - View subscription options

⚙️ *For Admin Only*
- /generate_key <days> <notes> - Generate access key
- /all_keys - List all access keys
- /delete_key <key> - Delete access key
- /config - View bot configuration
- /set_config <key> <value> - Update bot configuration
- /user_stats <user_id> - View user statistics

_NOTE: No automatic payment system. Contact bot owner to purchase subscription._
    """
    
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    user_id = update.effective_user.id
    stats = user_manager.get_user_stats(user_id)
    
    config = config_manager.load_config()
    bot_name = config.get("bot_name", DEFAULT_BOT_CONFIG["bot_name"])
    
    if stats["is_paid"]:
        if stats["subscription_end"]:
            end_date = datetime.fromisoformat(stats["subscription_end"]).strftime("%Y-%m-%d %H:%M:%S")
            status = f"✅ *PAID USER*\nSubscription ends: {end_date}"
        else:
            status = "✅ *PAID USER*\nLifetime subscription"
    else:
        status = "🆓 *FREE USER*"
    
    stats_text = f"""
📊 *{bot_name} - Your Statistics*

{status}

📥 *Download Stats*
- Today's downloads: {stats['daily_downloads']}
- Downloads remaining: {stats['downloads_remaining']}
- Total downloads: {stats['total_downloads']}

💡 Upgrade to paid for unlimited downloads!
    """
    
    keyboard = [
        [InlineKeyboardButton("💎 Get Subscription", callback_data="subscription")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(stats_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=reply_markup)

async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscription command"""
    config = config_manager.load_config()
    subscription_message = config.get("subscription_message", DEFAULT_BOT_CONFIG["subscription_message"])
    owner_username = config.get("owner_username", "bot_owner")
    
    # Format subscription message
    subscription_text = subscription_message.format(owner_username=owner_username)
    
    user_id = update.effective_user.id
    stats = user_manager.get_user_stats(user_id)
    
    if stats["is_paid"]:
        subscription_text = "✅ *You are already a PAID USER!*\n\n" + subscription_text
    else:
        subscription_text = "💎 *Subscription Plans*\n\n" + subscription_text
    
    keyboard = []
    
    if not stats["is_paid"]:
        keyboard.append([InlineKeyboardButton("🔑 Enter Access Key", callback_data="enter_key")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(subscription_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(subscription_text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "start":
        await start(update, context)
    elif query.data == "download":
        await query.edit_message_text("📥 Send me a Terabox link to download!")
    elif query.data == "subscription":
        await subscription_command(update, context)
    elif query.data == "stats":
        await stats_command(update, context)
    elif query.data == "help":
        await help_command(update, context)
    elif query.data == "enter_key":
        context.user_data['awaiting_key'] = True
        await query.edit_message_text("🔑 Please send your access key:")
    elif query.data.startswith("admin_"):
        # Admin callbacks
        await admin_callback(update, context)

async def handle_access_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle access key input"""
    if context.user_data.get('awaiting_key'):
        key = update.message.text.strip()
        user_id = update.effective_user.id
        
        is_valid, duration_days = key_manager.validate_key(key)
        
        if is_valid:
            # Activate subscription
            if key_manager.use_key(key, user_id):
                user_manager.activate_subscription(user_id, duration_days, key)
                
                if duration_days > 0:
                    end_date = (datetime.now() + timedelta(days=duration_days)).strftime("%Y-%m-%d")
                    success_message = f"🎉 Congratulations! Your paid subscription is now active until {end_date}.\nYou now have unlimited downloads!"
                else:
                    success_message = "🎉 Congratulations! You now have a lifetime paid subscription with unlimited downloads!"
                
                # Clear the awaiting state
                context.user_data['awaiting_key'] = False
                
                keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(success_message, reply_markup=reply_markup)
                
                # Notify admin
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_USER_ID,
                        text=f"🔑 User @{update.effective_user.username or update.effective_user.id} activated subscription with key: {key}"
                    )
                except Exception as e:
                    logger.error(f"Could not notify admin: {e}")
            else:
                await update.message.reply_text("❌ Error activating subscription. Please try again or contact support.")
        else:
            await update.message.reply_text("❌ Invalid or already used access key. Please check and try again or contact support.")
        
        # Clear the awaiting state
        context.user_data['awaiting_key'] = False
    else:
        # Not awaiting key, process as normal message
        await handle_message(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages"""
    # Check if we're awaiting an access key
    if context.user_data.get('awaiting_key'):
        await handle_access_key(update, context)
        return
    
    # Check if message contains a Terabox link
    text = update.message.text
    
    if "terabox.com" in text or "teraboxapp.com" in text or "1024tera.com" in text:
        await process_terabox_link(update, context)
    else:
        # Handle other messages
        await update.message.reply_text(
            "Please send a Terabox link for downloading, or use /help for instructions.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
                InlineKeyboardButton("💎 Subscription", callback_data="subscription")
            ]])
        )

async def process_terabox_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process Terabox link and download video"""
    user_id = update.effective_user.id
    user_manager.update_user_info(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
    
    # Check if user can download
    can_download, error_message = user_manager.can_download(user_id)
    
    if not can_download:
        await update.message.reply_text(
            f"❌ {error_message}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💎 Upgrade to Paid", callback_data="subscription"),
                InlineKeyboardButton("📊 Check Stats", callback_data="stats")
            ]])
        )
        return
    
    # Extract URL from message
    url = extract_url_from_text(update.message.text)
    if not url:
        await update.message.reply_text("❌ Could not extract Terabox link. Please send a valid Terabox URL.")
        return
    
    status_message = await update.message.reply_text("🔍 Analyzing link...")
    
    try:
        # Call Terabox API
        await status_message.edit_text("🚀 Connecting to Terabox API...")
        
        api_url = TERABOX_API_URL.format(url)
        
        # Use aiohttp for async request
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=30) as response:
                if response.status != 200:
                    await status_message.edit_text("❌ Error connecting to Terabox API. Please try again later.")
                    return
                
                data = await response.json()
        
        # Check if API returned data
        if not data or "url" not in data:
            await status_message.edit_text("❌ Could not extract video from this link. Please check the URL and try again.")
            return
        
        video_url = data["url"]
        video_name = data.get("name", "video.mp4")
        video_size = data.get("size", 0)  # Size in bytes
        
        # Convert size to MB
        video_size_mb = video_size / (1024 * 1024)
        
        config = config_manager.load_config()
        size_limit = config.get("video_size_limit", 2000)
        
        if video_size_mb > size_limit:
            await status_message.edit_text(
                f"❌ Video is too large ({video_size_mb:.1f} MB). Maximum allowed size is {size_limit} MB.\n"
                "This is a Telegram limitation. Please try a smaller video."
            )
            return
        
        # Update status
        await status_message.edit_text(f"⬇️ Downloading: {video_name}\n📏 Size: {video_size_mb:.1f} MB\nPlease wait...")
        
        # Download video with progress
        downloaded_file = await download_video_with_progress(video_url, video_name, status_message, context)
        
        if not downloaded_file:
            await status_message.edit_text("❌ Error downloading video. Please try again later.")
            return
        
        # Upload to Telegram
        await status_message.edit_text("⬆️ Uploading to Telegram...")
        
        # Send video
        with open(downloaded_file, 'rb') as video_file:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=video_file,
                caption=f"🎬 {video_name}\n\n✅ Downloaded from Terabox\n💎 {get_user_status_emoji(user_id)}",
                supports_streaming=True,
                timeout=config.get("download_timeout", 300)
            )
        
        # Clean up
        try:
            os.remove(downloaded_file)
        except Exception as e:
            logger.error(f"Error cleaning up file: {e}")
        
        # Update user stats
        video_info = {
            "name": video_name,
            "size": video_size,
            "original_url": url,
            "downloaded_at": datetime.now().isoformat()
        }
        user_manager.increment_download_count(user_id, video_info)
        
        # Edit status message
        await status_message.edit_text("✅ Download completed successfully!")
        
        # Send follow-up message
        stats = user_manager.get_user_stats(user_id)
        remaining = stats['downloads_remaining']
        
        if stats['is_paid']:
            followup_text = "✅ Enjoy your unlimited downloads as a paid user!"
        else:
            followup_text = f"✅ Download completed! You have {remaining} downloads remaining today."
        
        keyboard = [
            [InlineKeyboardButton("📥 Download Another", callback_data="download")],
            [InlineKeyboardButton("💎 Upgrade to Paid", callback_data="subscription")],
            [InlineKeyboardButton("📊 My Stats", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(followup_text, reply_markup=reply_markup)
        
    except asyncio.TimeoutError:
        await status_message.edit_text("❌ Download timed out. Please try again later.")
    except Exception as e:
        logger.error(f"Error processing Terabox link: {e}")
        await status_message.edit_text(f"❌ Error: {str(e)[:200]}...")

def extract_url_from_text(text):
    """Extract URL from text"""
    import re
    url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    urls = re.findall(url_pattern, text)
    
    for url in urls:
        if "terabox.com" in url or "teraboxapp.com" in url or "1024tera.com" in url:
            return url
    
    return None

async def download_video_with_progress(url, filename, status_message, context):
    """Download video with progress updates"""
    try:
        # Create downloads directory if it doesn't exist
        if not os.path.exists("downloads"):
            os.makedirs("downloads")
        
        filepath = os.path.join("downloads", filename)
        
        # Clean filename
        filepath = "".join(c for c in filepath if c.isalnum() or c in (' ', '.', '_', '-')).rstrip()
        
        # Use aiohttp for async download
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                
                # Get total size
                total_size = int(response.headers.get('content-length', 0))
                downloaded_size = 0
                chunk_size = 8192
                
                # Update status
                await status_message.edit_text(f"⬇️ Downloading: 0%")
                
                with open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        # Update progress every 5%
                        if total_size > 0:
                            progress = int((downloaded_size / total_size) * 100)
                            if progress % 5 == 0:
                                try:
                                    await status_message.edit_text(f"⬇️ Downloading: {progress}%")
                                except Exception:
                                    # Ignore errors in progress updates
                                    pass
                
                return filepath
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None

def get_user_status_emoji(user_id):
    """Get emoji based on user status"""
    if user_manager.is_paid_user(user_id):
        return "💎 PAID USER"
    else:
        return "🆓 FREE USER"

# Admin Commands
async def generate_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate subscription access key (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /generate_key <days> [notes]\nExample: /generate_key 30 Monthly Subscription")
        return
    
    try:
        duration_days = int(context.args[0])
        notes = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        
        key = key_manager.generate_key(duration_days, notes)
        
        if duration_days > 0:
            duration_text = f"{duration_days} days"
        else:
            duration_text = "lifetime"
        
        response_text = f"""
🔑 *New Access Key Generated*

Key: `{key}`
Duration: {duration_text}
Notes: {notes if notes else "None"}

Give this key to a user to activate their paid subscription.
        """
        
        await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def all_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all access keys (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    keys = key_manager.get_all_keys()
    
    if not keys:
        await update.message.reply_text("No access keys found.")
        return
    
    response_text = "🔑 *All Access Keys*\n\n"
    
    for key, data in keys.items():
        status = "✅ Available" if data["used_by"] is None else f"❌ Used by {data['used_by']}"
        duration = f"{data['duration_days']} days" if data['duration_days'] > 0 else "lifetime"
        created = datetime.fromisoformat(data['created_at']).strftime("%Y-%m-%d %H:%M")
        
        response_text += f"Key: `{key}`\n"
        response_text += f"Duration: {duration}\n"
        response_text += f"Status: {status}\n"
        response_text += f"Created: {created}\n"
        if data.get("notes"):
            response_text += f"Notes: {data['notes']}\n"
        if data["used_by"]:
            used_at = datetime.fromisoformat(data['used_at']).strftime("%Y-%m-%d %H:%M")
            response_text += f"Used at: {used_at}\n"
        response_text += "\n"
    
    # Split message if too long
    if len(response_text) > 4000:
        response_text = response_text[:3990] + "\n... (message truncated)"
    
    await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)

async def delete_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete an access key (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /delete_key <key>")
        return
    
    key = context.args[0]
    
    if key_manager.delete_key(key):
        await update.message.reply_text(f"✅ Access key deleted: {key}")
    else:
        await update.message.reply_text(f"❌ Access key not found: {key}")

async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View bot configuration (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    config = config_manager.load_config()
    
    response_text = "⚙️ *Bot Configuration*\n\n"
    
    for key, value in config.items():
        response_text += f"{key}: `{value}`\n"
    
    response_text += "\nUse /set_config <key> <value> to update configuration."
    
    await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)

async def set_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update bot configuration (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set_config <key> <value>")
        return
    
    key = context.args[0]
    value = " ".join(context.args[1:])
    
    # Handle numeric values
    if value.isdigit():
        value = int(value)
    elif value.replace('.', '', 1).isdigit():
        value = float(value)
    # Keep as string otherwise
    
    config_manager.update_config(key, value)
    
    await update.message.reply_text(f"✅ Configuration updated: {key} = {value}")

async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View user statistics (admin only)"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /user_stats <user_id>")
        return
    
    try:
        user_id = int(context.args[0])
        user = user_manager.get_user(user_id)
        stats = user_manager.get_user_stats(user_id)
        
        response_text = f"📊 *User Statistics for {user_id}*\n\n"
        response_text += f"Username: @{user.get('username', 'N/A')}\n"
        response_text += f"Name: {user.get('first_name', 'N/A')}\n"
        response_text += f"Status: {'💎 PAID USER' if stats['is_paid'] else '🆓 FREE USER'}\n"
        
        if stats['is_paid'] and stats['subscription_end']:
            end_date = datetime.fromisoformat(stats['subscription_end']).strftime("%Y-%m-%d %H:%M:%S")
            response_text += f"Subscription ends: {end_date}\n"
        
        response_text += f"Today's downloads: {stats['daily_downloads']}\n"
        response_text += f"Total downloads: {stats['total_downloads']}\n"
        response_text += f"Last download date: {user.get('last_download_date', 'Never')}\n"
        
        await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callbacks"""
    query = update.callback_query
    await query.answer()
    
    # Add admin-specific callbacks here if needed
    pass

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again later or contact support."
            )
        except Exception:
            pass

def main():
    """Start the bot"""
    # Create Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("subscription", subscription_command))
    
    # Admin commands
    application.add_handler(CommandHandler("generate_key", generate_key_command))
    application.add_handler(CommandHandler("all_keys", all_keys_command))
    application.add_handler(CommandHandler("delete_key", delete_key_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("set_config", set_config_command))
    application.add_handler(CommandHandler("user_stats", user_stats_command))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Start the bot
    logger.info("Starting Terabox Downloader Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()