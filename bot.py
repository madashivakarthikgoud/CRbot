#!/usr/bin/env python3
"""
Daenerys, a loyal and robust Telegram bot for cloning sticker packs,
exclusively for its Dragon.
"""

import os
import logging
import asyncio
import re
import tempfile
import shutil
from pathlib import Path

# Pillow is used for image processing.
from PIL import Image, UnidentifiedImageError

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from telegram.constants import ParseMode

# --- BOT CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Your exclusive User ID. The bot will only respond to you.
AUTHORIZED_USER_ID = 2120708516

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

# --- Image Processing Function ---
async def process_static_sticker(input_path: Path, output_path: Path) -> bool:
    """
    Resizes and converts an image to a Telegram-compliant static sticker.
    Returns True on success, False on failure.
    """
    def _process():
        try:
            with Image.open(input_path) as img:
                img = img.convert("RGBA")
                img.thumbnail((512, 512))
                img.save(output_path, "PNG")
                return True
        except UnidentifiedImageError:
            logger.warning(f"Cannot identify image file, skipping: {input_path}")
            return False
        except Exception as e:
            logger.error(f"Failed to process image {input_path}: {e}")
            return False
    
    return await asyncio.to_thread(_process)


# --- Bot Command and Message Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command, but only for the authorized user."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return  # Ignore unauthorized users

    welcome_message = (
        "Greetings, my Dragon.\n\n"
        "I am Daenerys, your loyal servant. My sole purpose is to clone sticker packs for you, "
        "forging new ones in my name to conceal their origin.\n\n"
        "Command me by sending a link to a sticker pack."
    )
    await update.message.reply_text(welcome_message)


async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The core function that clones the sticker pack, exclusively for the authorized user."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return # Ignore unauthorized users

    message = update.message
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("My Dragon, that does not appear to be a valid sticker pack link.")
        return

    original_pack_name = url_match.group(1)
    
    status_msg = await message.reply_text("A worthy command. The process begins...")

    temp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"Dragon has commanded a clone for pack: {original_pack_name}")
        
        original_pack = await context.bot.get_sticker_set(original_pack_name)

        bot_username = (await context.bot.get_me()).username
        new_title = original_pack.title
        unique_suffix = os.urandom(3).hex()
        new_pack_name = f"{original_pack.name}_{unique_suffix}_by_{bot_username}"
        new_pack_name = re.sub(r'[^a-zA-Z0-9_]', '', new_pack_name)[:64]

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"Gathering {len(original_pack.stickers)} stickers from the old realm..."
        )
        
        is_static_pack = not original_pack.is_animated and not original_pack.is_video
        sticker_format = "static" if is_static_pack else "animated" if original_pack.is_animated else "video"
            
        prepared_stickers = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            ext = Path(file.file_path).suffix.lower() if Path(file.file_path).suffix else ".tmp"
            original_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(original_dest_path)
            
            final_sticker_path = original_dest_path
            
            # --- ROBUSTNESS FIX ---
            # Only process static stickers that are actual images. Skip non-images like .webm.
            if is_static_pack:
                if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                    processed_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}.png"
                    if await process_static_sticker(original_dest_path, processed_dest_path):
                        final_sticker_path = processed_dest_path
                    else:
                        continue # Skip this sticker if processing fails
                else:
                    logger.warning(f"Skipping non-image file '{ext}' in static pack.")
                    continue # Skip non-image files in static packs

            prepared_stickers.append({"path": final_sticker_path, "emoji": sticker.emoji})
            logger.info(f"Prepared sticker {i+1}/{len(original_pack.stickers)}")

        if not prepared_stickers:
            raise ValueError("No valid stickers could be prepared from this pack.")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="Forging the new pack and adding the stickers..."
        )
        
        first_sticker = prepared_stickers.pop(0)
        await context.bot.create_new_sticker_set(
            user_id=AUTHORIZED_USER_ID,
            name=new_pack_name,
            title=new_title,
            stickers=[(first_sticker["path"].read_bytes(), first_sticker["emoji"])],
            sticker_format=sticker_format
        )
        
        for i, sticker_data in enumerate(prepared_stickers):
            await context.bot.add_sticker_to_set(
                user_id=AUTHORIZED_USER_ID,
                name=new_pack_name,
                sticker=(sticker_data["path"].read_bytes(), sticker_data["emoji"])
            )
            if (i + 2) % 10 == 0:
                 await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id, 
                    message_id=status_msg.message_id, 
                    text=f"Adding sticker {i+2}/{len(prepared_stickers) + 1} to the new collection..."
                )

        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        logger.info(f"Successfully forged new pack for the Dragon: {new_pack_url}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"Your conquest is complete, my Dragon.\n\nThe new sticker pack awaits you:\n{new_pack_url}\n\nYours,\nDaenerys",
            parse_mode=ParseMode.MARKDOWN
        )

    except TelegramError as e:
        error_text = str(e.message)
        user_message = f"My Dragon, a problem has arisen with Telegram's servers.\n\nDetails: `{error_text}`"
        logger.error(f"Telegram error for the Dragon on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=user_message, parse_mode=ParseMode.MARKDOWN)
    
    except Exception as e:
        error_message = f"My Dragon, an unexpected failure occurred within my own workings.\n\nDetails: `{str(e)}`"
        logger.error(f"Unexpected error for the Dragon on pack {original_pack_name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text=error_message, parse_mode=ParseMode.MARKDOWN)
    
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

    logger.info("Daenerys is awakening...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()