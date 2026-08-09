import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram.ext import Application
import bot
import database
import scraper
import schedule
import time
from threading import Thread

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# To keep track of previous stock states to avoid spamming
# Structure: { chat_id: { url: in_stock_boolean } }
previous_state = {}

async def notify_user(application, chat_id, message):
    try:
        await application.bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")

def check_all_stock(application):
    """
    This function iterates through all users and their watched URLs,
    checks the stock, and sends a notification if the status changes to IN STOCK.
    """
    logger.info("Running scheduled stock check...")
    users_data = database.get_all_users_and_urls()
    
    for chat_id, urls in users_data.items():
        if chat_id not in previous_state:
            previous_state[chat_id] = {}

        for url in urls:
            logger.info(f"Checking URL for {chat_id}: {url}")
            result = scraper.check_amazon_stock(url)
            
            # Avoid hammering Amazon too fast
            time.sleep(3) 

            if result["status"] == "success":
                in_stock = result["in_stock"]
                title = result["title"]
                price = result["price"]
                
                was_in_stock = previous_state[chat_id].get(url, False)
                
                # If it wasn't in stock before, but it is now -> ALERT!
                if in_stock and not was_in_stock:
                    msg = (
                        f"🚨 **IN STOCK ALERT** 🚨\n\n"
                        f"**{title}**\n"
                        f"Price: {price}\n\n"
                        f"{url}"
                    )
                    # We need to run the async send_message in the event loop
                    asyncio.run_coroutine_threadsafe(notify_user(application, chat_id, msg), application.job_queue.scheduler.loop)
                
                # Update state
                previous_state[chat_id][url] = in_stock
            else:
                logger.error(f"Failed to check {url}: {result.get('message')}")

def run_scheduler(application):
    """
    Runs the scheduling loop in a separate thread.
    Checks every 15 minutes to avoid being blocked quickly.
    """
    # Check every 15 minutes
    schedule.every(15).minutes.do(check_all_stock, application=application)
    
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Please set it in your .env file or environment variables.")
        return

    # Initialize the Telegram Bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    bot.setup_bot_handlers(application)

    # Start the background thread for checking stock
    scheduler_thread = Thread(target=run_scheduler, args=(application,), daemon=True)
    scheduler_thread.start()

    logger.info("Bot is starting...")
    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
