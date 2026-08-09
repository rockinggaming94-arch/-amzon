from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import database
import os
import logging

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "Welcome to the Amazon In-Stock Bot! 🛒\n\n"
        "I can monitor Amazon links and alert you when they come back in stock.\n\n"
        "Commands:\n"
        "/add <amazon_url> - Add a new product to monitor\n"
        "/list - View all products you are monitoring\n"
        "/remove <amazon_url> - Stop monitoring a product\n"
        "/help - Show this message"
    )
    await update.message.reply_text(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "Commands:\n"
        "/add <amazon_url> - Add a new product to monitor\n"
        "/list - View all products you are monitoring\n"
        "/remove <amazon_url> - Stop monitoring a product\n"
    )
    await update.message.reply_text(help_message)

async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Please provide a URL. Example: /add https://www.amazon.com/dp/B0815XFSGK")
        return

    url = context.args[0]
    if "amazon" not in url.lower():
        await update.message.reply_text("That doesn't look like a valid Amazon URL.")
        return

    if database.add_url(chat_id, url):
        await update.message.reply_text(f"Successfully added to your watchlist!\nI will notify you when it comes in stock.")
    else:
        await update.message.reply_text("This URL is already in your watchlist.")

async def remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Please provide a URL. Example: /remove https://www.amazon.com/dp/B0815XFSGK")
        return

    url = context.args[0]
    if database.remove_url(chat_id, url):
        await update.message.reply_text("Removed from your watchlist.")
    else:
        await update.message.reply_text("I couldn't find that URL in your watchlist.")

async def list_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    urls = database.get_urls(chat_id)
    
    if not urls:
        await update.message.reply_text("Your watchlist is empty.")
        return

    message = "Your Watchlist:\n\n"
    for idx, url in enumerate(urls, 1):
        message += f"{idx}. {url}\n"
    
    await update.message.reply_text(message)

def setup_bot_handlers(application: Application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_url))
    application.add_handler(CommandHandler("remove", remove_url))
    application.add_handler(CommandHandler("list", list_urls))
