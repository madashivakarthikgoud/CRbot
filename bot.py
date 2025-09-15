#!/usr/bin/env python3
"""
A simple, production-ready Telegram bot that clones a sticker pack.
The new pack's name will include the bot's username, hiding the original creator.
Designed for Render.com free web services with robust error handling.

Set environment variables:
- BOT_TOKEN: Your Telegram bot token.
- PORT: The port for the health check server (Render sets this automatically).
"""

import os
import logging
import asyncio
import re
import tempfile
import shutil
from pathlib import Path

from telegram import Update, InputSticker
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# --- Basic Configuration ---
# Set up logging to see bot activity and errors in your Render logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Get BOT_TOKEN and PORT from environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

# --- Bot Command and Message Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command with a welcome message."""
    welcome_message = (
        "👋 **Welcome!**\n\n"
        "I can clone any public sticker pack for you. This creates a new pack where "
        "my username is in the link, hiding the original creator.\n\n"
        "➡️ **To start, just send me a link to a sticker pack**, like:\n"
        "`https://t.me/addstickers/StickerPackName`"
    )
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The core function that clones the sticker pack when a link is received."""
    message = update.message
    # Use regex to find a valid sticker pack URL.
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("That doesn't look like a valid Telegram sticker pack link. Please try again.")
        return

    original_pack_name = url_match.group(1)
    user_id = update.effective_user.id
    
    status_msg = await message.reply_text("✅ Link received! Starting the cloning process...")

    # Create a temporary directory to safely store sticker files.
    temp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"User {user_id} starting clone for pack: {original_pack_name}")
        
        # Step 1: Get all metadata about the original sticker pack.
        original_pack = await context.bot.get_sticker_set(original_pack_name)

        # Step 2: Prepare a new, unique name and title for the cloned pack.
        bot_username = (await context.bot.get_me()).username
        new_title = original_pack.title
        unique_suffix = os.urandom(3).hex() # Prevents name conflicts.
        new_pack_name = f"{original_pack.name}_{unique_suffix}_by_{bot_username}"
        new_pack_name = re.sub(r'[^a-zA-Z0-9_]', '', new_pack_name)[:64] # Ensure valid format.

        # Step 3: Download all stickers from the original pack into the temp directory.
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"📥 Downloading {len(original_pack.stickers)} stickers..."
        )
        
        input_stickers_to_upload = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            ext = Path(file.file_path).suffix
            dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(dest_path)
            
            # Prepare the sticker object for re-upload.
            input_sticker = InputSticker(sticker=dest_path.read_bytes(), emoji_list=[sticker.emoji])
            input_stickers_to_upload.append(input_sticker)
            logger.info(f"Downloaded sticker {i+1}/{len(original_pack.stickers)}")

        # Step 4: Determine the correct sticker format (static, animated, or video).
        sticker_format = "static"
        if original_pack.is_animated:
            sticker_format = "animated"
        elif original_pack.is_video:
            sticker_format = "video"

        # Step 5: Create the new sticker pack in a single, efficient API call.
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="🎨 Creating your new sticker pack..."
        )

        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=new_pack_name,
            title=new_title,
            stickers=input_stickers_to_upload,
            sticker_format=sticker_format
        )
        
        # Step 6: Send the success message with the link to the new pack.
        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        logger.info(f"Successfully created new pack for user {user_id}: {new_pack_url}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"🎉 **Success!**\n\nYour cloned sticker pack is ready:\n{new_pack_url}",
            parse_mode="Markdown"
        )

    except TelegramError as e:
        # This block catches errors from Telegram's API (e.g., sticker pack not found).
        error_message = f"❌ **An error occurred!**\n\nTelegram's server said: `{e.message}`\n\nThis usually means the sticker pack is private, deleted, or the link is wrong."
        logger.error(f"Telegram error for user {user_id} on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=error_message, parse_mode="Markdown")
    
    except Exception as e:
        # This block catches other errors (e.g., server-side issues like running out of disk space).
        error_message = f"❌ **An unexpected system error occurred!**\n\nDetails: `{str(e)}`\n\nThis often happens on free servers with limited disk space. Please try a smaller sticker pack."
        logger.error(f"Unexpected error for user {user_id} on pack {original_pack_name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=error_message, parse_mode="Markdown")
    
    finally:
        # This block always runs, ensuring the temporary directory is deleted.
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temporary directory: {temp_dir}")


# --- Health Check Server for Render ---

async def health_check_handler(reader, writer):
    """Responds to Render's HTTP health checks to keep the service alive."""
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nOK')
    await writer.drain()
    writer.close()

async def start_health_server():
    """Starts the simple server for health checks."""
    server = await asyncio.start_server(health_check_handler, '0.0.0.0', PORT)
    logger.info(f"Health check server started on http://0.0.0.0:{PORT}")
    async with server:
        await server.serve_forever()


# --- Main Application Runner ---

def main() -> None:
    """Starts the bot and the concurrent health check server."""
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN environment variable is not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Register the handlers for commands and messages.
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())

    logger.info("Bot is starting up...")
    application.run_polling()


if __name__ == "__main__":
    main()