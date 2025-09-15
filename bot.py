#!/usr/bin/env python3
"""
bot.py — Robust Sticker Pack Maker (polling mode)

Features:
- Polling-based Telegram bot (python-telegram-bot v20.x)
- Accepts PNG/JPEG/WEBP (raster) + .tgs (animated) + .webm (video)
- Raster images: background removal via rembg + PIL -> 512x512 WEBP
- Uses ThreadPoolExecutor and Semaphore to limit concurrent CPU work
- Temp files cleaned up on finish/cancel
- Auto-assigns emoji "😀"
- Ensures sticker pack is created under the BOT identity (anonymous to your user)
- Rate-limits per user actions
"""

import os
import re
import uuid
import shutil
import logging
import tempfile
import time
import asyncio
import concurrent.futures
from pathlib import Path
from typing import List, Dict, Optional
from io import BytesIO

from telegram import Update, InputFile
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackContext,
    filters,
)

# image libs
from PIL import Image
from rembg import remove

# -----------------------
# Configuration & logging
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sticker-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is required in environment")
    raise SystemExit("BOT_TOKEN missing")

# concurrency controls
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))        # CPU-bound workers
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))  # semaphore limit
MAX_IMAGE_MB = float(os.environ.get("MAX_IMAGE_MB", "10"))   # reject files bigger than this (MB)
TEMP_ROOT = os.environ.get("TEMP_ROOT", "")  # optional base temp dir

# conversation states
ST_TITLE, ST_IMAGES = range(2)

# allowed extensions/mimes (basic)
ALLOWED_RASTER_EXT = (".png", ".jpg", ".jpeg", ".webp")
ALLOWED_ANIM_EXT = (".tgs", ".webm")

# in-memory rate-limiting (simple)
USER_LAST_ACTION: Dict[int, float] = {}
RATE_LIMIT_SECONDS = float(os.environ.get("RATE_LIMIT_SECONDS", "1.0"))

# threadpool + semaphore for blocking work
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
sem = asyncio.Semaphore(MAX_CONCURRENT)

# -----------------------
# Utility functions
# -----------------------
def safe_short_name(title: str, bot_username: str) -> str:
    """Create a Telegram-sticker-safe short name. Add suffix _by_botusername."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", title.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) < 1:
        s = uuid.uuid4().hex[:8]
    short = f"{s}_by_{bot_username}"
    # Telegram requires short name length <= ??? (avoid extremely long names)
    return short[:64]

def now_ts() -> float:
    return time.time()

def rate_limit_check(user_id: int) -> bool:
    """Return True if allowed (not rate-limited)."""
    last = USER_LAST_ACTION.get(user_id, 0.0)
    if now_ts() - last < RATE_LIMIT_SECONDS:
        return False
    USER_LAST_ACTION[user_id] = now_ts()
    return True

def ensure_tmpdir() -> Path:
    if TEMP_ROOT:
        base = Path(TEMP_ROOT)
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="sticker_", dir=base))
    return Path(tempfile.mkdtemp(prefix="sticker_"))

def file_size_mb_from_fileobj(file_obj) -> Optional[float]:
    # file_obj might have .file_size attribute (Telegram File)
    try:
        if hasattr(file_obj, "file_size") and file_obj.file_size is not None:
            return file_obj.file_size / (1024 * 1024)
    except Exception:
        pass
    return None

def download_file_sync(bot, file_id: str, dest_path: Path) -> Path:
    """Synchronous helper used inside executor to download via asyncio loop wrappers."""
    # We expect to call this via asyncio run_in_executor with bot.get_file(...) awaited
    raise RuntimeError("download_file_sync should not be called directly in this code")

async def download_file(bot, file_id: str, dest_path: Path) -> Path:
    """Async download file via Telegram File API (uses download_to_drive)."""
    tf = await bot.get_file(file_id)
    # check size
    if getattr(tf, "file_size", None):
        size_mb = tf.file_size / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            raise ValueError(f"File too large: {size_mb:.2f} MB (limit {MAX_IMAGE_MB} MB)")
    await tf.download_to_drive(custom_path=str(dest_path))
    return dest_path

def raster_process_sync(input_path: Path, output_path: Path, size: int = 512) -> None:
    """
    Blocking function to run rembg and PIL processing. Called in executor.
    Steps:
     - read bytes, run rembg.remove
     - open result as PIL, convert RGBA
     - thumbnail to fit within size then center onto transparent square
     - save as webp, lossless
    """
    with input_path.open("rb") as fh:
        input_bytes = fh.read()
    # remove background (may be slow)
    out_bytes = remove(input_bytes)
    img = Image.open(BytesIO(out_bytes)).convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    square.save(output_path, format="WEBP", lossless=True, method=6)

async def raster_process_async(input_path: Path, output_path: Path, size: int = 512):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, raster_process_sync, input_path, output_path, size)

async def ensure_unique_short_name_and_create(bot, bot_user_id: int, base_name: str, title: str, first_path: str, first_type: str, max_tries: int = 6):
    """Try to create sticker set; if name already taken, append suffix and retry."""
    suffix_try = 0
    name = base_name
    last_error = None
    while suffix_try < max_tries:
        try:
            # choose sticker_format based on type
            if first_type == "static":
                sticker_format = "static"
            elif first_type == "tgs":
                sticker_format = "animated"
            elif first_type == "webm":
                sticker_format = "video"
            else:
                sticker_format = "static"
            await bot.create_new_sticker_set(
                user_id=bot_user_id,
                name=name,
                title=title,
                stickers=[{"sticker": InputFile(first_path), "emoji_list": ["😀"]}],
                sticker_format=sticker_format,
            )
            return name  # success
        except TelegramError as e:
            last_error = e
            # Detect 'set name already occupied' in message (Telegram returns 400)
            st = str(e)
            logger.warning("create_new_sticker_set failed: %s", st)
            if "name is already occupied" in st or "already exists" in st or "file is invalid" in st:
                # append random 4 chars and retry
                suffix = uuid.uuid4().hex[:4]
                name = (base_name[:50] + "_" + suffix) if len(base_name) > 55 else (base_name + "_" + suffix)
                suffix_try += 1
                await asyncio.sleep(0.3)
                continue
            else:
                # other non-retriable error
                raise
    # If we exit loop
    raise last_error or RuntimeError("Failed to create sticker set after retries")

# -----------------------
# Conversation handlers
# -----------------------
async def cmd_help(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        "🤖 *Sticker Maker (Polling)*\n\n"
        "/stickers — create a new sticker pack (bot owns the pack)\n"
        "/done — finish and publish\n"
        "/cancel — cancel flow\n"
        "/health — bot health\n\n"
        "Send PNG/JPG/WEBP images (auto-background removal). Send .tgs/.webm for animated/video.",
        parse_mode="Markdown"
    )

async def cmd_health(update: Update, ctx: CallbackContext):
    await update.message.reply_text("✅ OK — bot is running (polling).")

async def start_stickers(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a little (rate limit).")
        return ConversationHandler.END
    ctx.user_data["sticker_data"] = {"title": None, "short_name": None, "stickers": []}
    await update.message.reply_text("🎨 Send the title for the sticker pack (e.g., `Cool Cats`).")
    return ST_TITLE

async def handle_title(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a little (rate limit).")
        return ConversationHandler.END
    title = update.message.text.strip()
    bot_username = (await ctx.bot.get_me()).username
    short_name = safe_short_name(title, bot_username)
    ctx.user_data["sticker_data"]["title"] = title
    ctx.user_data["sticker_data"]["short_name"] = short_name
    await update.message.reply_text(
        f"✅ Title set: {title}\nNow send images. Images will be processed with background removal. Send /done when finished."
    )
    return ST_IMAGES

async def handle_files(update: Update, ctx: CallbackContext) -> int:
    """Main file handler — accepts photo or document."""
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a little (rate limit).")
        return ST_IMAGES

    msg = update.message
    d = ctx.user_data.get("sticker_data")
    if not d:
        await update.message.reply_text("Use /stickers first to start a pack.")
        return ConversationHandler.END

    # create temp dir for this user's flow if not exists
    tmpdir = ensure_tmpdir()
    ctx.user_data.setdefault("_tmpdirs", []).append(str(tmpdir))

    # determine the file id and filename
    file_id = None
    filename = None
    mime = None

    if msg.photo:
        file_id = msg.photo[-1].file_id
        filename = f"{uuid.uuid4().hex}.jpg"
    elif msg.document:
        doc = msg.document
        file_id = doc.file_id
        filename = doc.file_name or f"{uuid.uuid4().hex}"
        mime = doc.mime_type
    else:
        await msg.reply_text("❌ Please send a photo or a file (png/jpg/webp/tgs/webm).")
        return ST_IMAGES

    dest_path = tmpdir / filename
    try:
        # download
        await update.message.reply_text("⬇️ Downloading file...")
        await download_file(ctx.bot, file_id, dest_path)

        # quick size check
        size_mb = dest_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            await update.message.reply_text(f"❌ File too large ({size_mb:.2f} MB). Limit is {MAX_IMAGE_MB} MB.")
            shutil.rmtree(tmpdir, ignore_errors=True)
            ctx.user_data["_tmpdirs"].remove(str(tmpdir))
            return ST_IMAGES

        lower = filename.lower()
        # animated or video pass-through
        if lower.endswith(".tgs"):
            d["stickers"].append({"type": "tgs", "path": str(dest_path)})
            await update.message.reply_text("✅ Animated sticker (.tgs) added.")
            return ST_IMAGES
        if lower.endswith(".webm"):
            d["stickers"].append({"type": "webm", "path": str(dest_path)})
            await update.message.reply_text("✅ Video sticker (.webm) added.")
            return ST_IMAGES

        # else treat as raster -> schedule background removal
        # use semaphore to limit concurrency
        await update.message.reply_text("🧠 Processing image (background removal). This may take a few seconds...")
        async with sem:
            out_webp = tmpdir / f"{uuid.uuid4().hex}.webp"
            # call blocking process in executor
            await raster_process_async(dest_path, out_webp, size=512)
        d["stickers"].append({"type": "static", "path": str(out_webp)})
        await update.message.reply_text("✅ Image processed and added as sticker (auto emoji 😀).")
        return ST_IMAGES

    except ValueError as ve:
        # from file size check in download
        logger.info("User upload rejected: %s", ve)
        await update.message.reply_text(f"❌ {ve}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        ctx.user_data["_tmpdirs"].remove(str(tmpdir))
        return ST_IMAGES
    except Exception as e:
        logger.exception("Failed to handle file")
        await update.message.reply_text(f"❌ Failed to process file: {e}")
        # keep tmpdir for debugging or remove it
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            ctx.user_data["_tmpdirs"].remove(str(tmpdir))
        except Exception:
            pass
        return ST_IMAGES

async def finish_pack(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a little (rate limit).")
        return ConversationHandler.END

    d = ctx.user_data.get("sticker_data", {})
    if not d or not d.get("stickers"):
        await update.message.reply_text("⚠️ You didn't add any stickers.")
        return ConversationHandler.END

    bot = ctx.bot
    bot_user = await bot.get_me()
    base_name = d["short_name"]
    title = d["title"]
    stickers = d["stickers"]
    tmpdirs = ctx.user_data.get("_tmpdirs", [])

    await update.message.reply_text("🚀 Publishing sticker pack...")

    try:
        # create sticker set using bot identity; ensure unique short name by retries
        first = stickers[0]
        first_path = first["path"]
        first_type = first["type"]

        created_short_name = await ensure_unique_short_name_and_create(
            bot, bot_user.id, base_name, title, first_path, first_type, max_tries=8
        )

        # add remaining stickers
        for s in stickers[1:]:
            try:
                await bot.add_sticker_to_set(
                    user_id=bot_user.id,
                    name=created_short_name,
                    sticker=InputFile(s["path"]),
                    emojis="😀",
                )
            except TelegramError as e:
                logger.exception("Failed to add sticker to set")
                # continue adding others; inform user later
                await update.message.reply_text(f"⚠️ Could not add one sticker: {e}")

        await update.message.reply_text(f"🎉 Sticker pack created!\n👉 https://t.me/addstickers/{created_short_name}")

    except Exception as e:
        logger.exception("Failed to create sticker pack")
        await update.message.reply_text(f"❌ Failed to create sticker pack: {e}")

    finally:
        # cleanup tempdirs
        for td in tmpdirs:
            try:
                shutil.rmtree(td, ignore_errors=True)
            except Exception:
                logger.exception("Cleanup failed for %s", td)
        ctx.user_data.pop("_tmpdirs", None)
        ctx.user_data.pop("sticker_data", None)

    return ConversationHandler.END

async def cancel_flow(update: Update, ctx: CallbackContext) -> int:
    tmpdirs = ctx.user_data.get("_tmpdirs", [])
    for td in tmpdirs:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            logger.exception("Cleanup failed")
    ctx.user_data.pop("_tmpdirs", None)
    ctx.user_data.pop("sticker_data", None)
    await update.message.reply_text("❌ Sticker creation cancelled and cleaned up.")
    return ConversationHandler.END

# -----------------------
# Boot the bot
# -----------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # conversation handler
    conv = ConversationHandler(
        entry_points=[CommandHandler("stickers", start_stickers)],
        states={
            ST_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            ST_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files),
                CommandHandler("done", finish_pack),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv)

    # simple commands
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))

    logger.info("Starting bot (polling mode) with MAX_WORKERS=%d MAX_CONCURRENT=%d", MAX_WORKERS, MAX_CONCURRENT)
    app.run_polling()

if __name__ == "__main__":
    main()
