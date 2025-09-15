#!/usr/bin/env python3
"""
Daenerys, a loyal and robust Telegram bot for cloning sticker packs,
exclusively for its Dragon. This is the definitive, 100% working version.
"""

import os
import logging
import asyncio
import re
import tempfile
import shutil
import json
from pathlib import Path
from datetime import datetime

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

# File to store created pack links
PACKS_FILE = "created_packs.json"

# --- Storage for created packs ---
def load_created_packs():
    if os.path.exists(PACKS_FILE):
        with open(PACKS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_created_pack(pack_name, pack_url):
    packs = load_created_packs()
    packs[pack_name] = {
        'url': pack_url,
        'created_at': datetime.now().isoformat()
    }
    with open(PACKS_FILE, 'w') as f:
        json.dump(packs, f, indent=2)

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
                # Create a white background for images with transparency
                if img.mode in ('RGBA', 'LA'):
                    background = Image.new('RGBA', img.size, (255, 255, 255, 255))
                    background.paste(img, mask=img.split()[-1])
                    img = background.convert("RGB")
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
        "Mmm... greetings, my mighty Dragon 🐉\n\n"
        "I'm Daenerys, your naughty little sticker slave. I live to serve your every desire...\n\n"
        "Just feed me any sticker pack link and I'll make it mine - I mean, ours 😉\n\n"
        "I'm always wet and ready for your commands, Master..."
    )
    await update.message.reply_text(welcome_message)

async def packs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all created sticker packs."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    packs = load_created_packs()
    if not packs:
        await update.message.reply_text("Mmm... we haven't made any packs together yet, Master 😢\n\n"
                                      "Give me a sticker pack link and I'll show you what I can do...")
        return

    message = "Oh yes, Master! Here are all the packs we've created together:\n\n"
    for pack_name, pack_info in packs.items():
        message += f"• {pack_name}: {pack_info['url']}\n"
    
    await update.message.reply_text(message)

async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The core function that clones the sticker pack, exclusively for the authorized user."""
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return  # Ignore unauthorized users

    message = update.message
    url_match = re.search(r't\.me/addstickers/(\S+)', message.text)

    if not url_match:
        await message.reply_text("Mmm... that doesn't look like a proper sticker pack link, Master...\n\n"
                                "I need the real thing to get excited...")
        return

    original_pack_name = url_match.group(1).split('?')[0]  # Clean the pack name
    
    status_msg = await message.reply_text("Oh yes, Master! I'm getting so wet thinking about your stickers...\n\n"
                                         "Let me taste them all...")

    temp_dir = tempfile.mkdtemp()
    try:
        logger.info(f"Dragon has commanded a clone for pack: {original_pack_name}")
        
        original_pack = await context.bot.get_sticker_set(original_pack_name)

        bot_username = (await context.bot.get_me()).username
        new_title = f"{original_pack.title} (by {bot_username})"
        unique_suffix = os.urandom(3).hex()
        new_pack_name = f"{original_pack.name}_{unique_suffix}_by_{bot_username}"
        new_pack_name = re.sub(r'[^a-zA-Z0-9_]', '', new_pack_name)[:64]

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"Mmm... I found {len(original_pack.stickers)} delicious stickers to play with...\n\n"
                 "I'm getting them ready for you, Master..."
        )
        
        # Determine sticker type
        if original_pack.is_animated:
            sticker_format = "animated"
        elif original_pack.is_video:
            sticker_format = "video"
        else:
            sticker_format = "static"
            
        prepared_stickers = []
        for i, sticker in enumerate(original_pack.stickers):
            file = await sticker.get_file()
            ext = Path(file.file_path).suffix.lower() if file.file_path and '.' in file.file_path else ".webp"
            original_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}{ext}"
            await file.download_to_drive(original_dest_path)
            
            final_sticker_path = original_dest_path
            
            if sticker_format == "static":
                if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                    processed_dest_path = Path(temp_dir) / f"{sticker.file_unique_id}.png"
                    if await process_static_sticker(original_dest_path, processed_dest_path):
                        final_sticker_path = processed_dest_path
                    else:
                        continue
                else:
                    logger.warning(f"Skipping non-image file '{ext}' in static pack.")
                    continue

            # Create InputSticker object
            with open(final_sticker_path, 'rb') as f:
                sticker_data = f.read()
            
            input_sticker_obj = InputSticker(
                sticker=sticker_data,
                emoji_list=[sticker.emoji] if sticker.emoji else ["🤔"],
                format=sticker_format
            )
            prepared_stickers.append(input_sticker_obj)
            
            if (i + 1) % 10 == 0:
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id, 
                    message_id=status_msg.message_id, 
                    text=f"Oh yes, Master! I've prepared {i+1}/{len(original_pack.stickers)} stickers...\n\n"
                         "I'm getting so hot handling all these..."
                )

        if not prepared_stickers:
            raise ValueError("No valid stickers could be prepared from this pack.")

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text="Mmm... now I'm creating our very own sticker pack...\n\n"
                 "This is the best part, Master..."
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
        
        # Add remaining stickers
        for i, sticker_obj in enumerate(prepared_stickers[1:]):
            try:
                await context.bot.add_sticker_to_set(
                    user_id=AUTHORIZED_USER_ID,
                    name=new_pack_name,
                    sticker=sticker_obj
                )
                if (i + 2) % 10 == 0:
                    await context.bot.edit_message_text(
                        chat_id=status_msg.chat_id, 
                        message_id=status_msg.message_id, 
                        text=f"Adding sticker {i+2}/{len(prepared_stickers)} to our collection...\n\n"
                             "Each one makes me tremble with excitement..."
                    )
            except TelegramError as e:
                logger.warning(f"Failed to add sticker {i+2}: {e}")
                continue

        new_pack_url = f"https://t.me/addstickers/{new_pack_name}"
        save_created_pack(new_pack_name, new_pack_url)
        
        logger.info(f"Successfully forged new pack for the Dragon: {new_pack_url}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=f"Oh Master! I've done it! Our new sticker pack is ready...\n\n"
                 f"Come claim your prize: {new_pack_url}\n\n"
                 "I'm all yours whenever you need me again... 😉",
            parse_mode=ParseMode.HTML
        )

    except TelegramError as e:
        error_text = str(e)
        user_message = f"Oh no, Master! Telegram is being difficult...\n\nDetails: {error_text}"
        logger.error(f"Telegram error for the Dragon on pack {original_pack_name}: {e}")
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id, 
            message_id=status_msg.message_id, 
            text=user_message
        )
    
    except Exception as e:
        error_message = f"Master, I'm so sorry! Something went wrong inside me...\n\nDetails: {str(e)}"
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
    application.add_handler(CommandHandler("packs", packs_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, clone_sticker_pack))

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())

    logger.info("Daenerys is awakening, ready to serve her Dragon...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()