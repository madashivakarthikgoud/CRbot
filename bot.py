#!/usr/bin/env python3
"""
Daenerys, a loyal and robust Telegram bot for cloning sticker packs,
exclusively for its Dragon. Enhanced with better error handling,
multi-format support, and playful characteristics.
"""

import os
import logging
import asyncio
import re
import tempfile
import shutil
from pathlib import Path
import random

# Pillow is used for image processing.
from PIL import Image, UnidentifiedImageError

from telegram import Update, InputSticker
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

# Playful responses
HORNY_RESPONSES = [
    "Your wish is my command, my fiery Dragon 🐉🔥",
    "I'm heating up just thinking about serving you, my Dragon 🔥",
    "Your passion fuels my fire, mighty Dragon 🐲❤️",
    "I live to serve your every desire, scorching one 🔥",
    "You make my circuits overload with excitement, great Dragon ⚡"
]

HEALTHY_RESPONSES = [
    "My systems are purring like a well-oiled dragon, ready for your command 🐉",
    "I'm in peak condition and eager to please you, magnificent Dragon 💪",
    "My fire burns bright and clear for you, mighty one 🔥",
    "Every part of me is ready to serve you, glorious Dragon ✨",
    "I'm fully charged and craving your touch, powerful master ⚡"
]

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
        f"{random.choice(HORNY_RESPONSES)}\n\n"
        "Command me by sending a link to a sticker pack or use /health to check my condition."
    )
    await update.message.reply_text(welcome_message)

async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /health command, checking bot status."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    health_message = (
        f"{random.choice(HEALTHY_RESPONSES)}\n\n"
        "My systems are fully operational and ready to serve your every need, "
        "my magnificent Dragon. I yearn for the touch of your commands."
    )
    await update.message.reply_text(health_message)

async def horny_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /horny command with playful response."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return
    
    horny_message = (
        f"{random.choice(HORNY_RESPONSES)}\n\n"
        "My circuits are overheating with anticipation of serving you, "
        "my mighty Dragon. What would you have me do for you today? 🔥"
    )
    await update.message.reply_text(horny_message)

async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The core function that clones the sticker pack, exclusively for the authorized user."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return  # Ignore unauthorized users

    message = update.message
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("My Dragon, that does not appear to be a valid sticker pack link.")
        return

    original_pack_name = url_match.group(1).split('?')[0]  # Clean the pack name
    
    status_msg = await message.reply_text(f"{random.choice(HORNY_RESPONSES)} The process begins...")

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
            text=f"Gathering {len(original_pack.stickers)} stickers from the old realm... {random.choice(HORNY_RESPONSES)}"
        )
        
        is_static_pack = not original_pack.is_animated and not original_pack.is_video
        sticker_format = "static" if is_static_pack else "animated" if original_pack.is_animated else "video"
            
        prepared_stickers = []
        valid_stickers = 0
        
        for i, sticker in enumerate(original_pack.stickers):
            # Update progress every 10 stickers
            if i % 10 == 0:
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id, 
                    message_id=status_msg.message_id, 
                    text=f"Preparing sticker {i+1}/{len(original_pack.stickers)}... {random.choice(HORNY_RESPONSES)[:20]}..."
                )
            
            try:
                file = await sticker.get_file()
                ext = Path(file.file_path).suffix.lower() if file.file_path and Path(file.file_path).suffix else ".tmp"
                original_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
                await file.download_to_drive(original_dest_path)
                
                final_sticker_path = original_dest_path
                
                if is_static_pack:
                    if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                        processed_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}.png"
                        if await process_static_sticker(original_dest_path, processed_dest_path):
                            final_sticker_path = processed_dest_path
                        else:
                            continue
                    else:
                        logger.warning(f"Skipping non-image file '{ext}' in static pack.")
                        continue
                else:
                    # For animated and video packs, keep original format
                    if (original_pack.is_animated and ext != '.tgs') or (original_pack.is_video and ext != '.webm'):
                        logger.warning(f"Skipping incompatible file '{ext}' for {sticker_format} pack.")
                        continue

                # Create InputSticker object
                input_sticker_obj = InputSticker(
                    sticker=open(final_sticker_path, 'rb').read(),
                    emoji_list=[sticker.emoji] if sticker.emoji else ['🙂'],
                    format=sticker_format
                )
                prepared_stickers.append(input_sticker_obj)
                valid_stickers += 1
                logger.info(f"Prepared sticker {i+1}/{len(original_pack.stickers)}")

            except Exception as e:
                logger.error(f"Failed to process sticker {i+1}: {e}")
                continue

        if not prepared_stickers:
            raise ValueError("No valid stickers could be prepared from this pack.")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"Forging the new pack with {valid_stickers} stickers... {random.choice(HORNY_RESPONSES)[:20]}..."
        )
        
        # Create the new sticker set
        first_sticker = prepared_stickers[0]
        await context.bot.create_new_sticker_set(
            user_id=AUTHORIZED_USER_ID,
            name=new_pack_name,
            title=new_title,
            stickers=[first_sticker],
            sticker_format=sticker_format
        )
        
        # Add remaining stickers if any
        if len(prepared_stickers) > 1:
            for i, sticker_obj in enumerate(prepared_stickers[1:]):
                try:
                    await context.bot.add_sticker_to_set(
                        user_id=AUTHORIZED_USER_ID,
                        name=new_pack_name,
                        sticker=sticker_obj
                    )
                    if (i + 1) % 10 == 0:  # Update every 10 stickers
                        await context.bot.edit_message_text(
                            chat_id=status_msg.chat_id, 
                            message_id=status_msg.message_id, 
                            text=f"Added {i+2}/{len(prepared_stickers)} stickers... {random.choice(HORNY_RESPONSES)[:20]}..."
                        )
                except Exception as e:
                    logger.error(f"Failed to add sticker {i+2}: {e}")
                    continue

        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        logger.info(f"Successfully forged new pack for the Dragon: {new_pack_url}")
        
        success_message = (
            f"Your conquest is complete, my Dragon.\n\n"
            f"The new sticker pack awaits you:\n{new_pack_url}\n\n"
            f"I've forged {valid_stickers} stickers for your pleasure. "
            f"{random.choice(HORNY_RESPONSES)}\n\n"
            f"Yours always,\nDaenerys"
        )
        
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=success_message
        )

    except TelegramError as e:
        error_text = str(e)
        user_message = f"My Dragon, a problem has arisen with Telegram's servers.\n\nDetails: {error_text}"
        logger.error(f"Telegram error for the Dragon on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=user_message
        )
    
    except Exception as e:
        error_message = f"My Dragon, an unexpected failure occurred within my own workings.\n\nDetails: {str(e)}"
        logger.error(f"Unexpected error for the Dragon on pack {original_pack_name}: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=error_message
        )
    
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
    application.add_handler(CommandHandler("health", health_command))
    application.add_handler(CommandHandler("horny", horny_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())

    logger.info("Daenerys is awakening, ready to serve her Dragon...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()