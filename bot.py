#!/usr/bin/env python3
"""
A simple Telegram bot that clones a sticker pack to a new pack.
The new pack's name will include the bot's username, hiding the original creator.
Designed for Render.com free web services.

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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Get BOT_TOKEN and PORT from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

# --- Bot Command and Message Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the /start command.
    Welcomes the user and explains what the bot does.
    """
    welcome_message = (
        "👋 Hello!\n\n"
        "I can clone any sticker pack for you. This creates a new pack where "
        "I'm listed as the creator in the URL, which hides who originally made it.\n\n"
        "➡️ **To start, just send me a link to a sticker pack**, like:\n"
        "https://t.me/addstickers/StickerPackName"
    )
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles messages containing a sticker pack link.
    This is the core function that clones the sticker pack.
    """
    message = update.message
    # Use regex to find a sticker pack URL in the message
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("That doesn't look like a valid Telegram sticker pack link. Please try again.")
        return

    original_pack_name = url_match.group(1)
    user_id = update.effective_user.id
    
    status_msg = await message.reply_text("✅ Link received! Starting the cloning process. This might take a few minutes for large packs...")

    # Create a temporary directory to store sticker files
    temp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"User {user_id} initiated cloning for pack: {original_pack_name}")
        
        # 1. Get all information about the original sticker pack
        original_pack = await context.bot.get_sticker_set(original_pack_name)

        # 2. Prepare details for the new pack
        bot_username = (await context.bot.get_me()).username
        new_title = original_pack.title
        # Create a new, unique name to avoid conflicts.
        # Format: OriginalName_RandomChars_by_BotUsername
        unique_suffix = os.urandom(3).hex()
        new_pack_name = f"{original_pack.name}_{unique_suffix}_by_{bot_username}"
        # Ensure the name is valid for Telegram
        new_pack_name = re.sub(r'[^a-zA-Z0-9_]', '', new_pack_name)[:64]

        # 3. Download all stickers from the original pack
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"📥 Downloading {len(original_pack.stickers)} stickers..."
        )
        
        input_stickers_to_upload = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            # Determine the file extension (.webp, .tgs, .webm)
            ext = Path(file.file_path).suffix
            dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(dest_path)
            
            # Prepare the sticker object for upload
            input_sticker = InputSticker(
                sticker=dest_path.read_bytes(), 
                emoji_list=[sticker.emoji] # A list containing the sticker's main emoji
            )
            input_stickers_to_upload.append(input_sticker)
            logger.info(f"Downloaded sticker {i+1}/{len(original_pack.stickers)}")

        # 4. Determine the correct sticker format (static, animated, or video)
        if original_pack.is_animated:
            sticker_format = "animated"
        elif original_pack.is_video:
            sticker_format = "video"
        else:
            sticker_format = "static"

        # 5. Create the new sticker pack in a single API call
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="🎨 Uploading stickers and creating your new pack..."
        )

        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=new_pack_name,
            title=new_title,
            stickers=input_stickers_to_upload,
            sticker_format=sticker_format
        )
        
        # 6. Send the success message with the link to the new pack
        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        logger.info(f"Successfully created new pack for user {user_id}: {new_pack_url}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"🎉 **Success!**\n\nYour cloned sticker pack is ready:\n{new_pack_url}",
            parse_mode="Markdown"
        )

    except TelegramError as e:
        logger.error(f"Telegram error for user {user_id} on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"❌ **Error!**\n\nI couldn't clone the pack. Telegram said: '{e.message}'.\nPlease make sure the link is correct and public."
        )
    except Exception as e:
        logger.error(f"Unexpected error for user {user_id} on pack {original_pack_name}: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="❌ **An unexpected error occurred.**\n\nSomething went wrong on my end. Please try again later."
        )
    finally:
        # 7. Clean up by deleting the temporary directory and all its contents
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up temp directory: {temp_dir}")


# --- Health Check Server for Render ---

async def health_check_handler(reader, writer):
    """Responds to HTTP requests to confirm the bot is running."""
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nOK')
    await writer.drain()
    writer.close()

async def start_health_server():
    """Starts the simple server for Render's health checks."""
    server = await asyncio.start_server(health_check_handler, '0.0.0.0', PORT)
    logger.info(f"Health check server started on http://0.0.0.0:{PORT}")
    async with server:
        await server.serve_forever()


# --- Main Application Runner ---

def main() -> None:
    """Starts the bot and the health check server."""
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN environment variable is not set!")
        return

    # Create the bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Register the handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    # Get the asyncio event loop to run tasks concurrently
    loop = asyncio.get_event_loop()
    
    # Start the health check server as a background task
    loop.create_task(start_health_server())

    logger.info("Bot is starting up...")
    # Run the bot until you press Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()