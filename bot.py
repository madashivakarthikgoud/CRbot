#!/usr/bin/env python3
"""
A definitive, production-ready Telegram bot that clones sticker packs.
This version includes a robust image processing step for static stickers
to ensure they meet Telegram's format and dimension requirements.
"""

import os
import logging
import asyncio
import re
import tempfile
import shutil
from pathlib import Path

# Pillow is used for image processing.
from PIL import Image

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

# --- Image Processing Function ---
async def process_static_sticker(input_path: Path, output_path: Path) -> None:
    """
    Resizes and converts an image to a Telegram-compliant static sticker (512xN PNG).
    This runs in a separate thread to avoid blocking the bot.
    """
    def _process():
        with Image.open(input_path) as img:
            # Convert to RGBA to ensure it has an alpha channel for transparency.
            img = img.convert("RGBA")
            # Resize the image to fit within a 512x512 box while maintaining aspect ratio.
            img.thumbnail((512, 512))
            # Save the result as a PNG, which is the required format.
            img.save(output_path, "PNG")
    
    await asyncio.to_thread(_process)


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
        
        # Determine the pack type ONCE.
        is_static_pack = not original_pack.is_animated and not original_pack.is_video
        sticker_format = "static"
        if original_pack.is_animated:
            sticker_format = "animated"
        elif original_pack.is_video:
            sticker_format = "video"
            
        prepared_stickers = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            ext = Path(file.file_path).suffix if Path(file.file_path).suffix else ".tmp"
            original_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(original_dest_path)
            
            final_sticker_path = original_dest_path
            
            # If it's a static pack, process the image.
            if is_static_pack:
                processed_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}.png"
                await process_static_sticker(original_dest_path, processed_dest_path)
                final_sticker_path = processed_dest_path

            prepared_stickers.append({"path": final_sticker_path, "emoji": sticker.emoji})
            logger.info(f"Prepared sticker {i+1}/{len(original_pack.stickers)}")

        if not prepared_stickers:
            raise ValueError("Could not prepare any stickers from the pack.")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="🎨 Creating new pack and uploading stickers..."
        )
        
        # Create the pack with the first sticker.
        first_sticker = prepared_stickers.pop(0)
        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=new_pack_name,
            title=new_title,
            stickers=[InputSticker(first_sticker["path"].read_bytes(), [first_sticker["emoji"]], format=sticker_format)],
        )
        
        # Add the rest of the stickers one by one.
        for i, sticker_data in enumerate(prepared_stickers):
            await context.bot.add_sticker_to_set(
                user_id=user_id,
                name=new_pack_name,
                sticker=InputSticker(sticker_data["path"].read_bytes(), [sticker_data["emoji"]], format=sticker_format)
            )
            # Update status every 5 stickers to avoid hitting rate limits.
            if (i + 2) % 5 == 0:
                 await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id, 
                    message_id=status_msg.message_id, 
                    text=f"📤 Uploading sticker {i+2}/{len(prepared_stickers) + 1}..."
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
        error_text = str(e.message)
        user_message = f"❌ **A Telegram error occurred!**\n\nDetails: `{error_text}`"
        if "Sticker_png_dimensions" in error_text:
            user_message = "❌ **Error!**\n\nTelegram rejected a sticker for having the wrong dimensions. Even with processing, some files can't be fixed. Please try another pack."
        elif "Invalid sticker set name" in error_text:
            user_message = "❌ **Error!**\n\nTelegram says this sticker pack name is invalid. The pack may be deleted or the link is incorrect."
        
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