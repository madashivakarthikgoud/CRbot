#!/usr/bin/env python3
"""
A simple, production-ready Telegram bot that clones a sticker pack.
This definitive version uses a robust one-by-one upload method for large packs
and provides more detailed error feedback.
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
        
        downloaded_stickers = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            ext = Path(file.file_path).suffix
            dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(dest_path)
            
            downloaded_stickers.append({"path": dest_path, "emoji": sticker.emoji})
            logger.info(f"Downloaded sticker {i+1}/{len(original_pack.stickers)}")

        if not downloaded_stickers:
            raise ValueError("Could not download any stickers from the pack.")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="🎨 Creating new pack and uploading stickers..."
        )
        
        # --- ROBUST UPLOAD LOGIC ---
        # 1. Create the pack with the first sticker.
        first_sticker = downloaded_stickers.pop(0)
        first_sticker_format = "static"
        if original_pack.is_animated:
            first_sticker_format = "animated"
        elif original_pack.is_video:
            first_sticker_format = "video"

        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=new_pack_name,
            title=new_title,
            stickers=[InputSticker(first_sticker["path"].read_bytes(), [first_sticker["emoji"]], format=first_sticker_format)],
        )
        
        # 2. Add the rest of the stickers one by one.
        for i, sticker_data in enumerate(downloaded_stickers):
            await context.bot.add_sticker_to_set(
                user_id=user_id,
                name=new_pack_name,
                sticker=InputSticker(sticker_data["path"].read_bytes(), [sticker_data["emoji"]], format=first_sticker_format)
            )
            # Update status every 5 stickers to avoid hitting rate limits
            if (i + 2) % 5 == 0:
                 await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id, 
                    message_id=status_msg.message_id, 
                    text=f"📤 Uploading sticker {i+2}/{len(downloaded_stickers) + 1}..."
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
        # --- IMPROVED ERROR MESSAGES ---
        error_text = str(e.message)
        user_message = ""
        if "Request Entity Too Large" in error_text:
            user_message = "❌ **Error!**\n\nThe sticker pack is too large to process in a single request. This issue should be rare with the new upload method."
        elif "Invalid sticker set name" in error_text:
            user_message = "❌ **Error!**\n\nTelegram says this sticker pack name is invalid. The pack may be deleted or the link is incorrect."
        else:
            user_message = f"❌ **A Telegram error occurred!**\n\nDetails: `{error_text}`"
        
        logger.error(f"Telegram error for user {user_id} on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=user_message, parse_mode="Markdown")
    
    except Exception as e:
        error_message = f"❌ **An unexpected system error occurred!**\n\nDetails: `{str(e)}`"
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())

    logger.info("Bot is starting up...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()