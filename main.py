import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import database
import scraper

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory state to track previous stock status per user per URL
# Structure: { "chat_id:url": True/False }
previous_state = {}


# ─── Telegram Command Handlers ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "🛒 *Welcome to Amazon In-Stock Bot!*\n\n"
        "I monitor Amazon product links and alert you the moment they come back in stock.\n\n"
        "*Commands:*\n"
        "/add `<amazon_url>` — Add a product to monitor\n"
        "/list — View your watchlist\n"
        "/remove `<amazon_url>` — Stop monitoring a product\n"
        "/check — Force an immediate stock check\n"
        "/help — Show this message"
    )
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a URL.\nExample: /add https://www.amazon.in/dp/B0815XFSGK"
        )
        return

    url = context.args[0].strip()

    # Basic validation
    if "amazon" not in url.lower():
        await update.message.reply_text("❌ That doesn't look like a valid Amazon URL.")
        return

    if database.add_url(chat_id, url):
        await update.message.reply_text(
            f"✅ Added to your watchlist!\n\nI'll check every 15 minutes and notify you when it's in stock."
        )
        # Do an immediate first check for this URL
        result = scraper.check_amazon_stock(url)
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
            previous_state[f"{chat_id}:{url}"] = result["in_stock"]
        else:
            await update.message.reply_text(
                f"⚠️ I added the URL but couldn't check it right now. I'll keep trying every 15 minutes.\n"
                f"Error: {result.get('message', 'Unknown error')}"
            )
    else:
        await update.message.reply_text("ℹ️ This URL is already in your watchlist.")


async def remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Please provide a URL.\nExample: /remove https://www.amazon.in/dp/B0815XFSGK"
        )
        return

    url = context.args[0].strip()
    if database.remove_url(chat_id, url):
        key = f"{chat_id}:{url}"
        previous_state.pop(key, None)
        await update.message.reply_text("✅ Removed from your watchlist.")
    else:
        await update.message.reply_text("❌ I couldn't find that URL in your watchlist.")


async def list_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    urls = database.get_urls(chat_id)

    if not urls:
        await update.message.reply_text("📭 Your watchlist is empty. Use /add to add a product.")
        return

    message = "📋 *Your Watchlist:*\n\n"
    for idx, url in enumerate(urls, 1):
        message += f"{idx}. {url}\n"

    await update.message.reply_text(message, parse_mode="Markdown", disable_web_page_preview=True)


async def force_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    urls = database.get_urls(chat_id)

    if not urls:
        await update.message.reply_text("📭 Your watchlist is empty.")
        return

    await update.message.reply_text(f"🔍 Checking {len(urls)} product(s)... Please wait.")

    for url in urls:
        result = scraper.check_amazon_stock(url)
        if result["status"] == "success":
            status_emoji = "🟢 In Stock" if result["in_stock"] else "🔴 Out of Stock"
            msg = (
                f"📦 *{result['title'][:100]}*\n"
                f"Status: {status_emoji}\n"
                f"Price: {result['price']}\n"
                f"🔗 [View on Amazon]({url})"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            previous_state[f"{chat_id}:{url}"] = result["in_stock"]
        else:
            await update.message.reply_text(f"⚠️ Failed to check:\n{url}\nError: {result.get('message')}")


# ─── Background Scheduled Job ────────────────────────────────────────────────

async def scheduled_stock_check(context: ContextTypes.DEFAULT_TYPE):
    """
    This function is called automatically by the JobQueue every 15 minutes.
    It checks all watched URLs for all users and sends alerts when stock changes.
    """
    logger.info("⏰ Running scheduled stock check...")
    users_data = database.get_all_users_and_urls()

    for chat_id_str, urls in users_data.items():
        chat_id = int(chat_id_str)
        for url in urls:
            logger.info(f"Checking: {url} for chat {chat_id}")
            result = scraper.check_amazon_stock(url)

            if result["status"] == "success":
                in_stock = result["in_stock"]
                key = f"{chat_id}:{url}"
                was_in_stock = previous_state.get(key, None)

                # Alert if status changed to IN STOCK (or first time seeing it in stock)
                if in_stock and (was_in_stock is False or was_in_stock is None):
                    msg = (
                        f"🚨🚨🚨 *IN STOCK ALERT!* 🚨🚨🚨\n\n"
                        f"*{result['title'][:100]}*\n\n"
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
                    except Exception as e:
                        logger.error(f"Failed to send alert to {chat_id}: {e}")

                # Also alert if it went OUT of stock (so user knows)
                elif not in_stock and was_in_stock is True:
                    msg = (
                        f"📦 *Stock Update:*\n\n"
                        f"*{result['title'][:100]}*\n"
                        f"Status: 🔴 Out of Stock again\n"
                        f"I'll keep monitoring for you!"
                    )
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id, text=msg,
                            parse_mode="Markdown", disable_web_page_preview=True
                        )
                    except Exception as e:
                        logger.error(f"Failed to send update to {chat_id}: {e}")

                previous_state[key] = in_stock
            else:
                logger.warning(f"Failed to check {url}: {result.get('message')}")

    logger.info("✅ Scheduled stock check complete.")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN is not set! Set it as an environment variable.")
        return

    # Build the application
    application = Application.builder().token(BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_url))
    application.add_handler(CommandHandler("remove", remove_url))
    application.add_handler(CommandHandler("list", list_urls))
    application.add_handler(CommandHandler("check", force_check))

    # Schedule background stock checks every 15 minutes using the built-in JobQueue
    job_queue = application.job_queue
    job_queue.run_repeating(
        scheduled_stock_check,
        interval=900,       # 900 seconds = 15 minutes
        first=60,           # first check 60 seconds after bot starts
        name="stock_checker"
    )

    logger.info("🚀 Bot is starting... Stock checks every 15 minutes.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
