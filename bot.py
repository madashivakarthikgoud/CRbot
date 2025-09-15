#!/usr/bin/env python3
"""
bot.py — Sticker Maker with background job queue (polling)

- Upload images to build a sticker pack
- Processing runs in background workers (async queue)
- Immediate "Queued" reply on /done; final result posted when ready
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
from typing import List, Dict, Any, Optional
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
from PIL import Image
from rembg import remove

# -----------------------
# Configuration & logging
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sticker-bot-queue")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is required in environment")
    raise SystemExit("BOT_TOKEN missing")

# Concurrency settings
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))        # threadpool workers for CPU tasks
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))  # semaphore concurrent processing
MAX_IMAGE_MB = float(os.environ.get("MAX_IMAGE_MB", "12"))   # reject huge uploads (MB)
TEMP_ROOT = os.environ.get("TEMP_ROOT", "")                  # optional base temp directory

# Rate-limiting (simple)
RATE_LIMIT_SECONDS = float(os.environ.get("RATE_LIMIT_SECONDS", "0.4"))
USER_LAST_ACTION: Dict[int, float] = {}

# Conversation states
ST_TITLE, ST_IMAGES = range(2)

# Executor + semaphore + task queue
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
sem = asyncio.Semaphore(MAX_CONCURRENT)
TASK_QUEUE: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

# Allowed file suffixes (lowercase)
ALLOWED_RASTER_EXT = (".png", ".jpg", ".jpeg", ".webp")
ALLOWED_ANIM_EXT = (".tgs", ".webm")

# -----------------------
# Helpers
# -----------------------
def now_ts() -> float:
    return time.time()

def rate_limit_check(user_id: int) -> bool:
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

def safe_short_name(title: str, bot_username: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]", "_", title.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = uuid.uuid4().hex[:8]
    name = f"{s}_by_{bot_username}"
    return name[:64]

async def download_file(bot, file_id: str, dest_path: Path) -> Path:
    tf = await bot.get_file(file_id)
    if getattr(tf, "file_size", None):
        size_mb = tf.file_size / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            raise ValueError(f"File too large: {size_mb:.2f} MB (limit {MAX_IMAGE_MB} MB)")
    await tf.download_to_drive(custom_path=str(dest_path))
    return dest_path

def raster_process_sync(input_path: Path, output_path: Path, size: int = 512) -> None:
    """Blocking rembg + PIL work — run in threadpool."""
    with input_path.open("rb") as fh:
        input_bytes = fh.read()
    out_bytes = remove(input_bytes)  # may take several seconds
    img = Image.open(BytesIO(out_bytes)).convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    square.save(output_path, format="WEBP", lossless=True, method=6)

async def raster_process_async(input_path: Path, output_path: Path, size: int = 512) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, raster_process_sync, input_path, output_path, size)

async def ensure_unique_short_name_and_create(bot, bot_user_id: int, base_name: str, title: str,
                                              first_path: str, first_type: str, max_tries: int = 6) -> str:
    """Try create_new_sticker_set with retries on name conflict."""
    attempt = 0
    name = base_name
    last_exc: Optional[Exception] = None
    while attempt < max_tries:
        try:
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
                sticker_format=sticker_format
            )
            return name
        except TelegramError as e:
            last_exc = e
            st = str(e).lower()
            logger.warning("create_new_sticker_set attempt %d failed: %s", attempt, e)
            if "already exists" in st or "is already occupied" in st or "name is already occupied" in st:
                suffix = uuid.uuid4().hex[:4]
                # limit length to avoid too long names
                base_cut = base_name[:50]
                name = f"{base_cut}_{suffix}_by_{bot_user_id}"[:64]
                attempt += 1
                await asyncio.sleep(0.3)
                continue
            else:
                raise
    raise last_exc or RuntimeError("Failed to create sticker set after retries")

# -----------------------
# Conversation handlers - enqueue style
# -----------------------
async def cmd_help(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        "🤖 *Sticker Maker (Queued Processing)*\n\n"
        "/stickers — start new sticker pack\n"
        "/done — finish & queue the pack for background processing\n"
        "/cancel — cancel & cleanup\n"
        "/health — check bot status\n\n"
        "Send PNG/JPEG/WEBP for auto-background removal, or .tgs/.webm for animated/video stickers.",
        parse_mode="Markdown"
    )

async def cmd_health(update: Update, ctx: CallbackContext):
    await update.message.reply_text("✅ OK — bot running (polling).")

async def start_stickers(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ You're doing that too quickly — wait a second.")
        return ConversationHandler.END
    ctx.user_data["sticker_data"] = {"title": None, "short_name": None, "raw_files": [], "tmpdirs": []}
    await update.message.reply_text("🎨 Send the sticker pack title (e.g., `Cool Cats`).")
    return ST_TITLE

async def handle_title(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a bit.")
        return ConversationHandler.END
    title = update.message.text.strip()
    bot_username = (await ctx.bot.get_me()).username
    short_name = safe_short_name(title, bot_username)
    ctx.user_data["sticker_data"]["title"] = title
    ctx.user_data["sticker_data"]["short_name"] = short_name
    await update.message.reply_text(
        f"✅ Title set: *{title}*\nNow send images/documents. When ready, send /done to queue processing.",
        parse_mode="Markdown"
    )
    return ST_IMAGES

async def handle_files(update: Update, ctx: CallbackContext) -> int:
    user_id = update.effective_user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a bit.")
        return ST_IMAGES

    d = ctx.user_data.get("sticker_data")
    if not d:
        await update.message.reply_text("Start with /stickers first.")
        return ConversationHandler.END

    tmpdir = ensure_tmpdir()
    ctx.user_data["sticker_data"].setdefault("tmpdirs", []).append(str(tmpdir))

    msg = update.message
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
            filename = f"{uuid.uuid4().hex}.jpg"
        elif msg.document:
            doc = msg.document
            file_id = doc.file_id
            filename = doc.file_name or f"{uuid.uuid4().hex}"
        else:
            await msg.reply_text("❌ Please send a photo or a supported file (.png/.jpg/.webp/.tgs/.webm).")
            return ST_IMAGES

        dest = tmpdir / filename
        await msg.reply_text("⬇️ Downloading...")
        await download_file(ctx.bot, file_id, dest)

        size_mb = dest.stat().st_size / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            await msg.reply_text(f"❌ File too large ({size_mb:.2f}MB). Limit is {MAX_IMAGE_MB}MB.")
            # cleanup this tmpdir
            shutil.rmtree(tmpdir, ignore_errors=True)
            ctx.user_data["sticker_data"]["tmpdirs"].remove(str(tmpdir))
            return ST_IMAGES

        lower = filename.lower()
        # store raw file info; processing will happen in worker
        if lower.endswith(".tgs"):
            d["raw_files"].append({"type": "tgs", "path": str(dest)})
            await msg.reply_text("✅ Animated .tgs file queued (no processing needed).")
        elif lower.endswith(".webm"):
            d["raw_files"].append({"type": "webm", "path": str(dest)})
            await msg.reply_text("✅ Video .webm file queued (no processing needed).")
        else:
            # raster: will be processed in background
            d["raw_files"].append({"type": "raster", "path": str(dest)})
            await msg.reply_text("✅ Image downloaded and queued for background processing.")
        return ST_IMAGES

    except ValueError as ve:
        logger.info("Upload rejected: %s", ve)
        await update.message.reply_text(f"❌ {ve}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        ctx.user_data["sticker_data"]["tmpdirs"].remove(str(tmpdir))
        return ST_IMAGES
    except Exception as e:
        logger.exception("Failed to handle file")
        await update.message.reply_text(f"❌ Failed: {e}")
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            ctx.user_data["sticker_data"]["tmpdirs"].remove(str(tmpdir))
        except Exception:
            pass
        return ST_IMAGES

async def cmd_done_queue(update: Update, ctx: CallbackContext) -> int:
    """Called when user sends /done — create a job and push to TASK_QUEUE, reply Queued immediately."""
    user = update.effective_user
    user_id = user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down a bit.")
        return ConversationHandler.END

    d = ctx.user_data.get("sticker_data")
    if not d or not d.get("raw_files"):
        await update.message.reply_text("⚠️ No files added. Send images first.")
        return ST_IMAGES

    # prepare job
    job_id = uuid.uuid4().hex[:10]
    title = d["title"]
    short_name = d["short_name"]
    raw_files = list(d["raw_files"])  # copy
    tmpdirs = list(d.get("tmpdirs", []))

    # send immediate queued message and capture message id for progress updates
    qmsg = await update.message.reply_text(f"⏳ Queued job `{job_id}` — processing will start soon.", parse_mode="Markdown")
    job = {
        "job_id": job_id,
        "user_id": user_id,
        "chat_id": update.effective_chat.id,
        "reply_message_id": qmsg.message_id,
        "title": title,
        "short_name": short_name,
        "raw_files": raw_files,
        "tmpdirs": tmpdirs,
    }

    await TASK_QUEUE.put(job)
    logger.info("Enqueued job %s for user %s (%d files)", job_id, user.username or user_id, len(raw_files))

    # clear in-memory user data to avoid reusing same files accidentally
    # (we keep tmpdirs because worker will cleanup them)
    ctx.user_data.pop("sticker_data", None)
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: CallbackContext) -> int:
    # cleanup tmpdirs for this user, if any
    d = ctx.user_data.get("sticker_data")
    if d and "tmpdirs" in d:
        for td in d["tmpdirs"]:
            shutil.rmtree(td, ignore_errors=True)
    ctx.user_data.pop("sticker_data", None)
    await update.message.reply_text("❌ Cancelled and cleaned up.")
    return ConversationHandler.END

# -----------------------
# Worker that processes queued jobs
# -----------------------
async def worker_loop(app: Application, worker_id: int):
    bot = app.bot
    logger.info("Worker %d started", worker_id)
    while True:
        job = await TASK_QUEUE.get()
        job_id = job.get("job_id")
        chat_id = job.get("chat_id")
        reply_message_id = job.get("reply_message_id")
        title = job.get("title")
        short_name = job.get("short_name")
        raw_files = job.get("raw_files", [])
        tmpdirs = job.get("tmpdirs", [])

        try:
            # update queued message
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                            text=f"🔁 Job `{job_id}` started — processing {len(raw_files)} file(s)...", parse_mode="Markdown")
            except Exception:
                # fallback to send message
                await bot.send_message(chat_id=chat_id, text=f"🔁 Job `{job_id}` started — processing...")

            processed_stickers: List[Dict[str, Any]] = []
            idx = 0
            for rf in raw_files:
                idx += 1
                ftype = rf["type"]
                path = rf["path"]
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                            text=f"🔁 Job `{job_id}` — processing file {idx}/{len(raw_files)}...", parse_mode="Markdown")
                if ftype == "raster":
                    # convert in background with semaphore
                    async with sem:
                        out_webp = Path(path).with_suffix(".webp")
                        try:
                            await raster_process_async(Path(path), out_webp, size=512)
                            processed_stickers.append({"type": "static", "path": str(out_webp)})
                        except Exception as e:
                            logger.exception("Raster processing failed for %s", path)
                            await bot.send_message(chat_id=chat_id, text=f"⚠️ Failed to process image {Path(path).name}: {e}")
                elif ftype in ("tgs", "webm"):
                    # pass-through
                    typ = "tgs" if ftype == "tgs" else "webm"
                    processed_stickers.append({"type": typ, "path": str(path)})
                else:
                    logger.warning("Unknown raw file type: %s", ftype)

            if not processed_stickers:
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                            text=f"❌ Job `{job_id}` failed — no valid stickers processed.")
                continue

            # create sticker set under bot's identity (with retries for name conflicts)
            bot_user = await bot.get_me()
            first = processed_stickers[0]
            first_path = first["path"]
            first_type = first["type"]

            await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                       text=f"🚀 Job `{job_id}` — creating sticker pack...")
            created_name = await ensure_unique_short_name_and_create(bot, bot_user.id, short_name, title, first_path, first_type, max_tries=8)

            # add rest
            for idx, s in enumerate(processed_stickers[1:], start=2):
                try:
                    await bot.add_sticker_to_set(user_id=bot_user.id, name=created_name, sticker=InputFile(s["path"]), emojis="😀")
                except Exception as e:
                    logger.exception("Failed to add sticker #%d", idx)
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Couldn't add sticker #{idx}: {e}")

            await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                       text=f"🎉 Job `{job_id}` complete!\n👉 https://t.me/addstickers/{created_name}")
        except Exception as e:
            logger.exception("Worker failed processing job %s", job_id)
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                           text=f"❌ Job `{job_id}` failed: {e}")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=f"❌ Job `{job_id}` failed: {e}")
        finally:
            # cleanup temporary directories belonging to this job
            for td in tmpdirs:
                try:
                    shutil.rmtree(td, ignore_errors=True)
                except Exception:
                    logger.exception("Failed to cleanup tmpdir %s", td)
            TASK_QUEUE.task_done()

# -----------------------
# Boot the bot
# -----------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("stickers", start_stickers)],
        states={
            ST_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            ST_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files),
                CommandHandler("done", cmd_done_queue)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True, per_chat=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))

    # Start background workers after Application is ready — we will attach them to the loop
    async def start_workers_and_poll():
        # spawn worker coroutines
        loop = asyncio.get_running_loop()
        workers = []
        for wid in range(max(1, MAX_WORKERS)):
            workers.append(loop.create_task(worker_loop(app, wid + 1)))
        logger.info("Spawned %d worker(s)", len(workers))
        # start polling (blocking call)
        await app.start()
        await app.updater.start_polling()  # starts polling inside ptb internals
        # join forever (polling will keep running)
        await app.updater.idle()

    # Run polling with our workers using asyncio.run
    # We can't call app.run_polling() because we want to also start worker tasks in same loop
    # So we create an asyncio task to run polling and start workers
    async def runner():
        # schedule worker tasks
        loop = asyncio.get_running_loop()
        for wid in range(max(1, MAX_WORKERS)):
            loop.create_task(worker_loop(app, wid + 1))
        logger.info("Workers scheduled; starting polling")
        # run polling (this returns when stopped)
        await app.run_polling()

    # Run runner
    asyncio.run(runner())

if __name__ == "__main__":
    main()
