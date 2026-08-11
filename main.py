import os
import re
import time
import asyncio
import random
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import aiohttp
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import database
import scraper
import admin

# Load environment variables
load_dotenv()

# Base URL for the web dashboard (auto-detected on Railway, override with DASHBOARD_URL env var)
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

CHECK_INTERVAL = 120       # 2 minutes (seconds)
FIRST_CHECK_DELAY = 30     # first check 30 seconds after bot starts
MAX_CONCURRENT = 10        # max concurrent scrape tasks
STAGGER_DELAY = (0.5, 2.0) # random delay between checks to avoid throttling

# Track bot start time for /status
BOT_START_TIME = None
LAST_CHECK_TIME = None

# Global reference to the Telegram application (for runtime interval changes)
_application = None


def update_check_interval(new_interval_seconds):
    """
    Update the CHECK_INTERVAL at runtime and reschedule the repeating job.
    Called from admin.py when the admin changes the interval.
    """
    global CHECK_INTERVAL
    CHECK_INTERVAL = new_interval_seconds
    admin.set_shared_state("check_interval", CHECK_INTERVAL)

    if _application and _application.job_queue:
        # Remove existing stock_checker job(s)
        existing_jobs = _application.job_queue.get_jobs_by_name("stock_checker")
        for job in existing_jobs:
            job.schedule_removal()

        # Schedule a new one with the updated interval
        _application.job_queue.run_repeating(
            scheduled_stock_check,
            interval=CHECK_INTERVAL,
            first=10,  # start the next check in 10 seconds
            name="stock_checker"
        )
        logger.info(f"⏱️ Check interval updated to {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60}min)")
        admin.log_activity("info", f"Check interval changed to {CHECK_INTERVAL // 60} minutes")


# ─── Amazon Short-URL Domains ────────────────────────────────────────────────

AMAZON_SHORT_DOMAINS = ["amzn.in", "amzn.to", "amzn.eu", "amzn.asia", "a.co"]


async def resolve_short_url(url):
    """
    Resolve an Amazon shortened URL (amzn.in, amzn.to, a.co, etc.)
    by following redirects to get the final Amazon product URL.
    Returns the resolved URL, or None if resolution fails.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0"}
            ) as resp:
                final_url = str(resp.url)
                return final_url
    except Exception as e:
        logger.warning(f"Failed to resolve short URL {url}: {e}")
        # Fallback: try GET if HEAD didn't work
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    final_url = str(resp.url)
                    return final_url
        except Exception as e2:
            logger.error(f"Failed to resolve short URL (GET fallback) {url}: {e2}")
            return None


def is_amazon_short_url(url):
    """Check if a URL is an Amazon shortened link."""
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    return any(hostname == domain or hostname.endswith("." + domain) for domain in AMAZON_SHORT_DOMAINS)


# ─── URL Normalization ────────────────────────────────────────────────────────

def normalize_amazon_url(url):
    """
    Normalize an Amazon URL to a canonical form.
    Strips tracking parameters, normalizes path to /dp/ASIN format.
    This prevents duplicate watchlist entries for the same product.
    """
    url = url.strip()

    # Extract ASIN from various Amazon URL patterns
    asin_patterns = [
        r'/dp/([A-Z0-9]{10})',
        r'/gp/product/([A-Z0-9]{10})',
        r'/gp/aw/d/([A-Z0-9]{10})',
        r'/exec/obidos/asin/([A-Z0-9]{10})',
        r'/o/ASIN/([A-Z0-9]{10})',
        r'/product/([A-Z0-9]{10})',
    ]

    parsed = urlparse(url)
    asin = None

    for pattern in asin_patterns:
        match = re.search(pattern, parsed.path, re.IGNORECASE)
        if match:
            asin = match.group(1).upper()
            break

    if asin:
        # Rebuild a clean URL: https://www.amazon.{domain}/dp/{ASIN}
        domain = parsed.netloc
        if not domain:
            domain = "www.amazon.in"
        clean_url = f"https://{domain}/dp/{asin}"
        return clean_url

    # If we can't extract an ASIN, just strip query params and fragments
    clean = urlunparse((
        parsed.scheme or "https",
        parsed.netloc,
        parsed.path,
        '',  # params
        '',  # query (strip tracking)
        '',  # fragment
    ))
    return clean


def validate_amazon_url(url):
    """Validate that the URL is a legitimate Amazon product URL."""
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()

    # Allow Amazon short-link domains (they'll be resolved before normalization)
    if is_amazon_short_url(url):
        return True, None

    # Must be an amazon domain
    if not any(domain in hostname for domain in ["amazon.com", "amazon.in", "amazon.co.uk",
                                                   "amazon.de", "amazon.fr", "amazon.es",
                                                   "amazon.it", "amazon.ca", "amazon.co.jp",
                                                   "amazon.com.au", "amazon.com.br",
                                                   "amazon.sg", "amazon.ae", "amazon.sa"]):
        return False, "Not a recognized Amazon domain."

    # Should have some kind of product path
    path = parsed.path.lower()
    if not any(seg in path for seg in ["/dp/", "/gp/product/", "/gp/aw/d/", "/product/"]):
        # It might still be valid but we can't extract ASIN — warn but allow
        if "amazon" in hostname:
            return True, None
        return False, "Doesn't look like a product page URL."

    return True, None


# ─── Inline Keyboard Helpers ─────────────────────────────────────────────────

def get_main_keyboard():
    """Build the main inline keyboard for the welcome/help message."""
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Product", callback_data="help_add"),
            InlineKeyboardButton("📋 My Watchlist", callback_data="action_list"),
        ],
        [
            InlineKeyboardButton("🔍 Check Now", callback_data="action_check"),
            InlineKeyboardButton("📊 Bot Status", callback_data="action_status"),
        ],
        [
            InlineKeyboardButton("🌐 Web Dashboard", callback_data="action_dashboard"),
            InlineKeyboardButton("❓ Help", callback_data="action_help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ─── Telegram Command Handlers ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒  *Amazon In\\-Stock Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ *Lightning\\-fast stock alerts\\!*\n\n"
        "I monitor Amazon products and notify you\n"
        "the *instant* they come back in stock\\.\n\n"
        "🔄  Checks every *2 minutes*\n"
        "🌐  Rotating proxies for reliability\n"
        "📱  Instant Telegram notifications\n"
        "🎯  99% accurate stock detection\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *Quick Start:*\n"
        "Send `/add` \\+ Amazon product URL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 _Made by Proofy Gamerz_"
    )
    await update.message.reply_text(
        welcome_message,
        parse_mode="MarkdownV2",
        reply_markup=get_main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a URL.\n"
            "Example: `/add https://www.amazon.in/dp/B0815XFSGK`",
            parse_mode="Markdown"
        )
        return

    raw_url = context.args[0].strip()

    # Resolve Amazon short URLs (amzn.in, amzn.to, a.co, etc.)
    if is_amazon_short_url(raw_url):
        await update.message.reply_text("🔗 Resolving shortened link...")
        resolved = await resolve_short_url(raw_url)
        if resolved:
            raw_url = resolved
            logger.info(f"Resolved short URL to: {raw_url}")
        else:
            await update.message.reply_text(
                "❌ Couldn't resolve this shortened link.\n\n"
                "Please try pasting the full Amazon product URL instead.\n"
                "Example: `https://www.amazon.in/dp/B0815XFSGK`",
                parse_mode="Markdown"
            )
            return

    # Validate
    is_valid, error_msg = validate_amazon_url(raw_url)
    if not is_valid:
        await update.message.reply_text(
            f"❌ Invalid URL: {error_msg}\n\n"
            "Please provide a direct Amazon product link.\n"
            "Example: `https://www.amazon.in/dp/B0815XFSGK`",
            parse_mode="Markdown"
        )
        return

    # Normalize to canonical form
    url = normalize_amazon_url(raw_url)

    if database.add_url(chat_id, url):
        await update.message.reply_text(
            f"✅ Added to your watchlist!\n\n"
            f"🔗 Tracking: `{url}`\n"
            f"⚡ I'll check every {CHECK_INTERVAL // 60} minutes and notify you instantly when it's in stock.\n\n"
            f"🔍 Running first check now...",
            parse_mode="Markdown"
        )
        admin.log_activity("info", f"User {chat_id} added URL", chat_id=chat_id, url=url)

        # Do an immediate first check
        result = await scraper.check_amazon_stock_async(url)
        if result["status"] == "success":
            status_emoji = "🟢 In Stock" if result["in_stock"] else "🔴 Out of Stock"
            msg = (
                f"📦 *Current Status:*\n\n"
                f"*{result['title'][:100]}*\n"
                f"Status: {status_emoji}\n"
                f"Price: {result['price']}\n"
                f"🔗 [View on Amazon]({url})"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            database.update_stock_state(chat_id, url, result["in_stock"])
        elif result["status"] == "blocked":
            await update.message.reply_text(
                "⚠️ Amazon is temporarily blocking requests. "
                "I'll keep trying on the next scheduled check — don't worry!"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Couldn't check right now, but I've added it. I'll keep trying every {CHECK_INTERVAL // 60} minutes.\n"
                f"Reason: {result.get('message', 'Unknown error')}"
            )
    else:
        await update.message.reply_text("ℹ️ This product is already in your watchlist.")


async def remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a URL.\n"
            "Example: `/remove https://www.amazon.in/dp/B0815XFSGK`\n\n"
            "💡 Use /list to see your watchlist, or /clearall to remove everything.",
            parse_mode="Markdown"
        )
        return

    raw_url = context.args[0].strip()
    url = normalize_amazon_url(raw_url)

    if database.remove_url(chat_id, url):
        await update.message.reply_text("✅ Removed from your watchlist.")
        admin.log_activity("info", f"User {chat_id} removed URL", chat_id=chat_id, url=url)
    else:
        # Maybe they pasted the original un-normalized URL — try raw
        if database.remove_url(chat_id, raw_url):
            await update.message.reply_text("✅ Removed from your watchlist.")
            admin.log_activity("info", f"User {chat_id} removed URL", chat_id=chat_id, url=raw_url)
        else:
            await update.message.reply_text(
                "❌ Couldn't find that URL in your watchlist.\n"
                "Use /list to see your current watchlist."
            )


async def clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    count = database.clear_all_urls(chat_id)
    if count > 0:
        await update.message.reply_text(f"🗑️ Cleared {count} product(s) from your watchlist.")
        admin.log_activity("info", f"User {chat_id} cleared {count} URLs", chat_id=chat_id)
    else:
        await update.message.reply_text("📭 Your watchlist is already empty.")


async def list_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    urls = database.get_urls(chat_id)

    if not urls:
        await update.message.reply_text("📭 Your watchlist is empty. Use /add to add a product.")
        return

    message = f"📋 *Your Watchlist ({len(urls)} items):*\n\n"
    for idx, url in enumerate(urls, 1):
        # Show last known state
        state = database.get_stock_state(chat_id, url)
        if state is True:
            indicator = "🟢"
        elif state is False:
            indicator = "🔴"
        else:
            indicator = "⚪"
        message += f"{idx}. {indicator} {url}\n"

    message += "\n🟢 In Stock  🔴 Out of Stock  ⚪ Not checked yet"

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)


async def force_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    urls = database.get_urls(chat_id)

    if not urls:
        await update.message.reply_text("📭 Your watchlist is empty.")
        return

    await update.message.reply_text(f"🔍 Checking {len(urls)} product(s) concurrently... Please wait.")

    # Check all URLs concurrently
    tasks = [scraper.check_amazon_stock_async(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    state_updates = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            await update.message.reply_text(f"⚠️ Error checking:\n{url}\n{str(result)}")
            continue

        if result["status"] == "success":
            status_emoji = "🟢 In Stock" if result["in_stock"] else "🔴 Out of Stock"
            msg = (
                f"📦 *{result['title'][:100]}*\n"
                f"Status: {status_emoji}\n"
                f"Price: {result['price']}\n"
                f"🔗 [View on Amazon]({url})"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            state_updates.append((chat_id, url, result["in_stock"]))
        elif result["status"] == "blocked":
            await update.message.reply_text(f"🛡️ Amazon blocked request for:\n{url}\nWill retry on next scheduled check.")
        else:
            await update.message.reply_text(f"⚠️ Failed to check:\n{url}\nError: {result.get('message')}")

    # Batch update stock states
    database.bulk_update_stock_states(state_updates)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = database.get_stats()
    now = datetime.now(timezone.utc)

    uptime_str = "Unknown"
    if BOT_START_TIME:
        uptime = now - BOT_START_TIME
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

    last_check_str = "Not yet"
    if LAST_CHECK_TIME:
        ago = now - LAST_CHECK_TIME
        mins_ago = int(ago.total_seconds() / 60)
        last_check_str = f"{mins_ago} min ago" if mins_ago > 0 else "Just now"

    proxy_count = len(scraper._PROXY_POOL) if hasattr(scraper, '_PROXY_POOL') else 0

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *Bot Status*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟢 Status: Running\n"
        f"⏱️ Uptime: {uptime_str}\n"
        f"👥 Active Users: {stats['total_users']}\n"
        f"🔗 Tracked URLs: {stats['total_urls']}\n"
        f"⏰ Check Interval: Every {CHECK_INTERVAL // 60} minutes\n"
        f"🕐 Last Check: {last_check_str}\n"
        f"🌐 Proxies: {proxy_count} active\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── Callback Query Handler (Inline Buttons) ─────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Acknowledge the button press

    data = query.data
    chat_id = query.message.chat_id

    if data == "help_add":
        await query.message.reply_text(
            "➕ *How to Add a Product:*\n\n"
            "1️⃣ Go to Amazon and find your product\n"
            "2️⃣ Copy the product URL\n"
            "3️⃣ Send: `/add <paste_url_here>`\n\n"
            "📌 *Example:*\n"
            "`/add https://www.amazon.in/dp/B0815XFSGK`\n\n"
            "I'll start monitoring it immediately!",
            parse_mode="Markdown"
        )

    elif data == "action_list":
        urls = database.get_urls(chat_id)
        if not urls:
            await query.message.reply_text("📭 Your watchlist is empty. Use /add to add a product.")
            return

        message = f"📋 *Your Watchlist ({len(urls)} items):*\n\n"
        for idx, url in enumerate(urls, 1):
            state = database.get_stock_state(chat_id, url)
            if state is True:
                indicator = "🟢"
            elif state is False:
                indicator = "🔴"
            else:
                indicator = "⚪"
            message += f"{idx}. {indicator} {url}\n"
        message += "\n🟢 In Stock  🔴 Out of Stock  ⚪ Not checked yet"
        await query.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)

    elif data == "action_check":
        urls = database.get_urls(chat_id)
        if not urls:
            await query.message.reply_text("📭 Your watchlist is empty.")
            return
        await query.message.reply_text(f"🔍 Checking {len(urls)} product(s)... Please wait.")
        tasks = [scraper.check_amazon_stock_async(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        state_updates = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                continue
            if result["status"] == "success":
                status_emoji = "🟢 In Stock" if result["in_stock"] else "🔴 Out of Stock"
                msg = (
                    f"📦 *{result['title'][:100]}*\n"
                    f"Status: {status_emoji}\n"
                    f"Price: {result['price']}\n"
                    f"🔗 [View on Amazon]({url})"
                )
                await query.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
                state_updates.append((chat_id, url, result["in_stock"]))
        database.bulk_update_stock_states(state_updates)

    elif data == "action_status":
        stats = database.get_stats()
        now = datetime.now(timezone.utc)
        uptime_str = "Unknown"
        if BOT_START_TIME:
            uptime = now - BOT_START_TIME
            h, rem = divmod(int(uptime.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
        proxy_count = len(scraper._PROXY_POOL) if hasattr(scraper, '_PROXY_POOL') else 0
        msg = (
            "📊 *Bot Status*\n\n"
            f"🟢 Running | ⏱️ {uptime_str}\n"
            f"👥 {stats['total_users']} users | 🔗 {stats['total_urls']} URLs\n"
            f"🌐 {proxy_count} proxies | ⏰ Every {CHECK_INTERVAL // 60}min"
        )
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "action_clearall":
        count = database.clear_all_urls(chat_id)
        if count > 0:
            await query.message.reply_text(f"🗑️ Cleared {count} product(s) from your watchlist.")
            admin.log_activity("info", f"User {chat_id} cleared {count} URLs", chat_id=chat_id)
        else:
            await query.message.reply_text("📭 Your watchlist is already empty.")

    elif data == "action_dashboard":
        await _send_dashboard_link(query.message, chat_id)

    elif data == "action_help":
        help_msg = (
            "📖 *All Commands:*\n\n"
            "➕ `/add <url>` — Add a product\n"
            "🗑 `/remove <url>` — Remove a product\n"
            "📋 `/list` — View your watchlist\n"
            "🔍 `/check` — Force stock check\n"
            "🧹 `/clearall` — Clear watchlist\n"
            "🌐 `/dashboard` — Open web dashboard\n"
            "📊 `/status` — Bot stats\n"
            "❓ `/help` — Show this message"
        )
        await query.message.reply_text(help_msg, parse_mode="Markdown")


# ─── Background Scheduled Job ────────────────────────────────────────────────

async def scheduled_stock_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Called automatically by JobQueue every CHECK_INTERVAL seconds.
    Checks all watched URLs for all users concurrently and sends alerts on stock changes.
    """
    global LAST_CHECK_TIME
    LAST_CHECK_TIME = datetime.now(timezone.utc)
    admin.set_shared_state("last_check_time", LAST_CHECK_TIME)

    logger.info("⏰ Running scheduled stock check...")
    admin.log_activity("check", "Scheduled stock check started")
    users_data = database.get_all_users_and_urls()

    if not users_data:
        logger.info("No users/URLs to check.")
        return

    # Build a flat list of (chat_id, url) pairs
    check_list = []
    for chat_id_str, urls in users_data.items():
        for url in urls:
            check_list.append((int(chat_id_str), url))

    if not check_list:
        logger.info("No URLs to check.")
        return

    logger.info(f"📋 Checking {len(check_list)} URL(s) across {len(users_data)} user(s)")

    # Get unique URLs to avoid checking the same URL multiple times
    unique_urls = list(set(url for _, url in check_list))

    # Check all unique URLs concurrently with a semaphore to limit concurrency
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def check_with_limit(url):
        async with semaphore:
            # Small random delay to stagger requests
            await asyncio.sleep(random.uniform(*STAGGER_DELAY))
            return await scraper.check_amazon_stock_async(url)

    tasks = [check_with_limit(url) for url in unique_urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Map URL -> result
    url_results = {}
    for url, result in zip(unique_urls, results):
        if isinstance(result, Exception):
            logger.error(f"Exception checking {url}: {result}")
            url_results[url] = {"status": "error", "message": str(result)}
        else:
            url_results[url] = result

    # Process results and send alerts
    state_updates = []
    blocked_count = 0
    error_count = 0
    checked_count = 0

    for chat_id, url in check_list:
        result = url_results.get(url)
        if not result:
            continue

        if result["status"] == "blocked":
            blocked_count += 1
            continue

        if result["status"] == "error":
            error_count += 1
            logger.warning(f"Failed to check {url}: {result.get('message')}")
            continue

        if result["status"] != "success":
            continue

        checked_count += 1
        in_stock = result["in_stock"]
        was_in_stock = database.get_stock_state(chat_id, url)

        # ── Alert: came INTO stock ──
        if in_stock and (was_in_stock is False or was_in_stock is None):
            title = result.get('title', 'Product')[:100]
            msg = (
                f"🚨🚨🚨 *IN STOCK ALERT!* 🚨🚨🚨\n\n"
                f"*{title}*\n\n"
                f"💰 Price: {result['price']}\n"
                f"📦 Status: 🟢 IN STOCK\n\n"
                f"🔗 [BUY NOW on Amazon]({url})\n\n"
                f"⚡ *Hurry! It might go out of stock again!*"
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                admin.log_activity("in_stock", f"🟢 {title} is IN STOCK!", chat_id=chat_id, url=url)
            except Exception as e:
                logger.error(f"Failed to send IN STOCK alert to {chat_id}: {e}")

        # ── Alert: went OUT of stock ──
        elif not in_stock and was_in_stock is True:
            title = result.get('title', 'Product')[:100]
            msg = (
                f"📦 *Stock Update:*\n\n"
                f"*{title}*\n"
                f"Status: 🔴 Out of Stock again\n"
                f"I'll keep monitoring for you!"
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=msg,
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                admin.log_activity("out_of_stock", f"🔴 {title} went OUT OF STOCK", chat_id=chat_id, url=url)
            except Exception as e:
                logger.error(f"Failed to send OOS update to {chat_id}: {e}")

        state_updates.append((chat_id, url, in_stock))

    # Batch update all stock states in one write
    database.bulk_update_stock_states(state_updates)

    admin.log_activity("check",
        f"Check done: {checked_count} checked, {blocked_count} blocked, {error_count} errors"
    )
    logger.info(
        f"✅ Scheduled check complete: "
        f"{checked_count} checked, {blocked_count} blocked, {error_count} errors"
    )


# ─── Dashboard Command ───────────────────────────────────────────────────────

def _get_dashboard_url(chat_id):
    """Build the personal dashboard URL for a user."""
    token = database.generate_user_token(chat_id)
    base = DASHBOARD_BASE_URL
    if not base:
        port = os.getenv("PORT", "8080")
        base = f"http://localhost:{port}"
    return f"{base}/dashboard?token={token}"


async def _send_dashboard_link(message, chat_id):
    """Send the dashboard link to a user as a Telegram Mini App."""
    url = _get_dashboard_url(chat_id)
    # Ensure URL is https for Web Apps to work properly
    if url.startswith("http://") and "localhost" not in url:
        url = url.replace("http://", "https://")
        
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Dashboard", web_app=WebAppInfo(url=url))]
    ])
    await message.reply_text(
        "🌐 *Your Personal Dashboard*\n\n"
        "View your watchlist in a beautiful web interface\!"
        "\nAuto\-refreshes every 30 seconds\."
        "\n\n🔗 Tap the button below to open:",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the user their personal web dashboard link."""
    chat_id = update.effective_chat.id
    await _send_dashboard_link(update.message, chat_id)


# ─── Admin Command ───────────────────────────────────────────────────────────

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a one-click login link for the admin panel, restricted to ADMIN_IDS."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    # Generate a fresh token and build the auto-login URL
    token = admin.generate_admin_token()
    base = DASHBOARD_BASE_URL
    if not base:
        port = os.getenv("PORT", "8080")
        base = f"http://localhost:{port}"
    url = f"{base}/?admin_token={token}"
    if url.startswith("http://") and "localhost" not in url:
        url = url.replace("http://", "https://")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Open Admin Panel", web_app=WebAppInfo(url=url))]
    ])
    await update.message.reply_text(
        "🔐 *Admin Access Granted*\n\n"
        "Tap the button below to securely log into the admin dashboard\.\n"
        "This link will auto\-authenticate you without a password\.",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


# ─── Bot Menu Commands ───────────────────────────────────────────────────────

async def set_bot_commands(application):
    """Set the bot's command menu in Telegram."""
    commands = [
        BotCommand("start", "🚀 Welcome & quick actions"),
        BotCommand("add", "➕ Add an Amazon product URL"),
        BotCommand("list", "📋 View your watchlist"),
        BotCommand("check", "🔍 Force stock check now"),
        BotCommand("remove", "🗑️ Remove a product URL"),
        BotCommand("clearall", "🧹 Clear entire watchlist"),
        BotCommand("dashboard", "🌐 Open web dashboard"),
        BotCommand("status", "📊 Bot health & stats"),
        BotCommand("help", "❓ Show help"),
    ]
    await application.bot.set_my_commands(commands)


# ─── Main ────────────────────────────────────────────────────────────────────

async def run():
    """Main async entry point — runs both the Telegram bot and admin panel."""
    global BOT_START_TIME

    if not BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN is not set! Set it as an environment variable.")
        return

    BOT_START_TIME = datetime.now(timezone.utc)

    # Share state with admin panel
    admin.set_shared_state("bot_start_time", BOT_START_TIME)
    admin.set_shared_state("check_interval", CHECK_INTERVAL)

    # Build the Telegram bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Store global reference for runtime interval changes
    global _application
    _application = application

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_url))
    application.add_handler(CommandHandler("remove", remove_url))
    application.add_handler(CommandHandler("clearall", clear_all))
    application.add_handler(CommandHandler("list", list_urls))
    application.add_handler(CommandHandler("check", force_check))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("admin", admin_command))

    # Register inline button callback handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Schedule background stock checks
    job_queue = application.job_queue
    job_queue.run_repeating(
        scheduled_stock_check,
        interval=CHECK_INTERVAL,
        first=FIRST_CHECK_DELAY,
        name="stock_checker"
    )

    # Start the admin web server
    admin_runner = await admin.start_admin_server()
    admin.log_activity("info", "Bot started")

    logger.info(
        f"🚀 Bot is starting... Stock checks every {CHECK_INTERVAL // 60} minutes "
        f"(first check in {FIRST_CHECK_DELAY}s)"
    )

    # Initialize and start the bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    # Set bot command menu
    try:
        await set_bot_commands(application)
        logger.info("✅ Bot command menu set")
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

    # Run forever until interrupted
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await admin_runner.cleanup()
        logger.info("Bot stopped.")


def main():
    """Sync entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
