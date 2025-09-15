#!/usr/bin/env python3
"""
A simple, production-ready Telegram bot that clones a sticker pack.
This final version includes fixes for API changes, conflict errors, and network timeouts.
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
from telegram.request import Request

# --- Basic Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("That doesn't look like a valid Telegram sticker pack link.")
        return

    original_pack_name = url_match.group(1)
    user_id = update.effective_user.id
    
    status_msg = await message.reply_text("✅ Link received! Starting the cloning process...")

    temp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"User {user_id} starting clone for pack: {original_pack_name}")
        
        original_pack = await context.bot.get_sticker_set(original_pack_name)

        bot_username = (await context.bot.get_me()).username
        new_title = original_pack.title
        unique_suffix = os.urandom(3).hex()
        new_pack_name = f"{original_pack.name}_{unique_suffix}_by_{bot_username}"
        new_pack_name = re.sub(r'[^a-zA-Z0-9_]', '', new_pack_name)[:64]

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
            
            # --- FIX FOR TypeError ---
            # Determine the format for each sticker and pass it to InputSticker.
            sticker_format = "static"
            if sticker.is_animated:
                sticker_format = "animated"
            elif sticker.is_video:
                sticker_format = "video"

            input_sticker = InputSticker(
                sticker=dest_path.read_bytes(), 
                emoji_list=[sticker.emoji],
                format=sticker_format  # This required argument was missing
            )
            input_stickers_to_upload.append(input_sticker)
            logger.info(f"Downloaded sticker {i+1}/{len(original_pack.stickers)}")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="🎨 Creating your new sticker pack..."
        )

        # The API method for creating a sticker set has also changed.
        # We now must pass a list of InputSticker objects directly.
        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=new_pack_name,
            title=new_title,
            stickers=input_stickers_to_upload
        )
        
        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        logger.info(f"Successfully created new pack for user {user_id}: {new_pack_url}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"🎉 **Success!**\n\nYour cloned sticker pack is ready:\n{new_pack_url}",
            parse_mode="Markdown"
        )

    except TelegramError as e:
        error_message = f"❌ **An error occurred!**\n\nTelegram's server said: `{e.message}`\n\nThis usually means the sticker pack is private, deleted, or the link is wrong."
        logger.error(f"Telegram error for user {user_id} on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=error_message, parse_mode="Markdown")
    
    except Exception as e:
        error_message = f"❌ **An unexpected system error occurred!**\n\nDetails: `{str(e)}`\n\nThis often happens on free servers with limited disk space. Please try a smaller sticker pack."
        logger.error(f"Unexpected error for user {user_id} on pack {original_pack_name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=error_message, parse_mode="Markdown")
    
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temporary directory: {temp_dir}")


# --- Health Check Server for Render ---
async def health_check_handler(reader, writer):
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nOK')
    await writer.drain()
    writer.close()

async def start_health_server():
    server = await asyncio.start_server(health_check_handler, '0.0.0.0', PORT)
    logger.info(f"Health check server started on http://0.0.0.0:{PORT}")
    async with server:
        await server.serve_forever()

# --- Main Application Runner ---
def main() -> None:
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN environment variable is not set!")
        return

    # --- FIX FOR Timeouts ---
    # Increase the timeouts for connecting and reading from the API.
    request = Request(connect_timeout=30.0, read_timeout=30.0)
    
    application = Application.builder().token(BOT_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())

    logger.info("Bot is starting up...")
    # --- FIX FOR Conflict ---
    # drop_pending_updates helps the bot start clean after a crash or restart.
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()