#!/usr/bin/env python3
"""
Production-ready polling sticker bot with background job queue + small health HTTP server.

Copy this to bot.py and run: python bot.py
"""

import os
import re
import uuid
import shutil
import logging
import tempfile
import time
import asyncio
import signal
import concurrent.futures
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
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
logger = logging.getLogger("sticker-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is required")
    raise SystemExit("BOT_TOKEN missing")

# Tunables (via env)
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))           # threadpool workers
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))     # concurrent raster tasks
MAX_IMAGE_MB = float(os.environ.get("MAX_IMAGE_MB", "12"))      # MB
MAX_FILES_PER_JOB = int(os.environ.get("MAX_FILES_PER_JOB", "25"))
RATE_LIMIT_SECONDS = float(os.environ.get("RATE_LIMIT_SECONDS", "0.3"))
TEMP_ROOT = os.environ.get("TEMP_ROOT", "")                     # optional base tmp dir
ADMIN_USER_IDS = set(int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip())  # optional

# Conversation states
ST_TITLE, ST_IMAGES = range(2)

# Executor + semaphore + task queue
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
sem = asyncio.Semaphore(MAX_CONCURRENT)
TASK_QUEUE: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
JOB_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Allowed extensions
ALLOWED_RASTER_EXT = (".png", ".jpg", ".jpeg", ".webp")

# Rate limiting
USER_LAST_ACTION: Dict[int, float] = {}

# -----------------------
# Minimal HTTP health server (for Render web service)
# -----------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")
    def log_message(self, format, *args):
        return  # silence

def start_health_http_server(port: int):
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except Exception as e:
        logger.warning("Could not start health server on port %s: %s", port, e)
        return None
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health HTTP server started on port %d", port)
    return server

# -----------------------
# Utility functions
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

def sanitize_short_name(title: str, bot_username: str) -> str:
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
    with input_path.open("rb") as fh:
        input_bytes = fh.read()
    out_bytes = remove(input_bytes)
    img = Image.open(BytesIO(out_bytes)).convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    square.save(output_path, format="WEBP", lossless=True, method=6)

async def raster_process_async(input_path: Path, output_path: Path, size: int = 512):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, raster_process_sync, input_path, output_path, size)

async def create_sticker_set_with_retries(bot, bot_user_id: int, base_name: str, title: str, first_path: str, first_type: str, max_tries: int = 6) -> str:
    attempt = 0
    name = base_name
    last_exc: Optional[Exception] = None
    while attempt < max_tries:
        try:
            if first_type == "static":
                sfmt = "static"
            elif first_type == "tgs":
                sfmt = "animated"
            elif first_type == "webm":
                sfmt = "video"
            else:
                sfmt = "static"
            await bot.create_new_sticker_set(
                user_id=bot_user_id,
                name=name,
                title=title,
                stickers=[{"sticker": InputFile(first_path), "emoji_list": ["😀"]}],
                sticker_format=sfmt
            )
            return name
        except TelegramError as e:
            last_exc = e
            st = str(e).lower()
            logger.warning("create_new_sticker_set attempt %d failed: %s", attempt, e)
            if "already exists" in st or "name is already occupied" in st or "is already occupied" in st:
                suffix = uuid.uuid4().hex[:4]
                base_cut = base_name[:48]
                name = f"{base_cut}_{suffix}_by_{bot_user_id}"[:64]
                attempt += 1
                await asyncio.sleep(0.3)
                continue
            else:
                raise
    raise last_exc or RuntimeError("Failed to create sticker set after retries")

# -----------------------
# Conversation handlers
# -----------------------
async def cmd_help(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        "🤖 *Sticker Maker (Queued)*\n\n"
        "/stickers — start a new sticker pack\n"
        "/done — finish and queue processing\n"
        "/cancel — cancel flow & cleanup\n"
        "/health — check bot\n\n"
        "Upload PNG/JPEG/WEBP for background removal or .tgs/.webm for animated/video.",
        parse_mode="Markdown"
    )

async def cmd_health(update: Update, ctx: CallbackContext):
    qsize = TASK_QUEUE.qsize()
    await update.message.reply_text(f"✅ OK — bot running. queue_size={qsize}")

async def start_stickers(update: Update, ctx: CallbackContext) -> int:
    uid = update.effective_user.id
    if not rate_limit_check(uid):
        await update.message.reply_text("⏳ Slow down (rate limit).")
        return ConversationHandler.END
    ctx.user_data["sticker_data"] = {"title": None, "short_name": None, "raw_files": [], "tmpdirs": []}
    await update.message.reply_text("🎨 Send the sticker pack title (e.g., `Cool Cats`).")
    return ST_TITLE

async def handle_title(update: Update, ctx: CallbackContext) -> int:
    uid = update.effective_user.id
    if not rate_limit_check(uid):
        await update.message.reply_text("⏳ Slow down (rate limit).")
        return ConversationHandler.END
    title = update.message.text.strip()
    bot_username = (await ctx.bot.get_me()).username
    short_name = sanitize_short_name(title, bot_username)
    ctx.user_data["sticker_data"]["title"] = title
    ctx.user_data["sticker_data"]["short_name"] = short_name
    await update.message.reply_text(f"✅ Title set: *{title}*\nNow send files. When ready, send /done", parse_mode="Markdown")
    return ST_IMAGES

async def handle_files(update: Update, ctx: CallbackContext) -> int:
    uid = update.effective_user.id
    if not rate_limit_check(uid):
        await update.message.reply_text("⏳ Slow down (rate limit).")
        return ST_IMAGES

    d = ctx.user_data.get("sticker_data")
    if not d:
        await update.message.reply_text("Start with /stickers first.")
        return ConversationHandler.END

    if len(d["raw_files"]) >= MAX_FILES_PER_JOB:
        await update.message.reply_text(f"❌ Max files per job reached ({MAX_FILES_PER_JOB}). Send /done or /cancel.")
        return ST_IMAGES

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
            await msg.reply_text("❌ Send a photo or a supported file (.png/.jpg/.webp/.tgs/.webm).")
            return ST_IMAGES

        dest = tmpdir / filename
        await msg.reply_text("⬇️ Downloading...")
        await download_file(ctx.bot, file_id, dest)

        size_mb = dest.stat().st_size / (1024 * 1024)
        if size_mb > MAX_IMAGE_MB:
            await msg.reply_text(f"❌ File too large ({size_mb:.2f} MB). Limit {MAX_IMAGE_MB} MB.")
            shutil.rmtree(tmpdir, ignore_errors=True)
            ctx.user_data["sticker_data"]["tmpdirs"].remove(str(tmpdir))
            return ST_IMAGES

        lower = filename.lower()
        if lower.endswith(".tgs"):
            d["raw_files"].append({"type": "tgs", "path": str(dest)})
            await msg.reply_text("✅ Animated .tgs queued.")
        elif lower.endswith(".webm"):
            d["raw_files"].append({"type": "webm", "path": str(dest)})
            await msg.reply_text("✅ Video .webm queued.")
        elif lower.endswith(ALLOWED_RASTER_EXT):
            d["raw_files"].append({"type": "raster", "path": str(dest)})
            await msg.reply_text("✅ Image queued for background removal.")
        else:
            d["raw_files"].append({"type": "raster", "path": str(dest)})
            await msg.reply_text("✅ File queued (treated as image).")
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
    user = update.effective_user
    user_id = user.id
    if not rate_limit_check(user_id):
        await update.message.reply_text("⏳ Slow down (rate limit).")
        return ConversationHandler.END

    d = ctx.user_data.get("sticker_data")
    if not d or not d.get("raw_files"):
        await update.message.reply_text("⚠️ No files added. Send images first.")
        return ST_IMAGES

    job_id = uuid.uuid4().hex[:10]
    title = d["title"]
    short_name = d["short_name"]
    raw_files = list(d["raw_files"])  # shallow copy
    tmpdirs = list(d.get("tmpdirs", []))

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
        "status": "queued",
        "enqueued_at": now_ts(),
    }

    JOB_REGISTRY[job_id] = job
    await TASK_QUEUE.put(job)
    logger.info("Enqueued job %s for user %s (%d files)", job_id, user.username or user_id, len(raw_files))

    ctx.user_data.pop("sticker_data", None)
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: CallbackContext) -> int:
    d = ctx.user_data.get("sticker_data")
    if d and "tmpdirs" in d:
        for td in d["tmpdirs"]:
            shutil.rmtree(td, ignore_errors=True)
    ctx.user_data.pop("sticker_data", None)
    await update.message.reply_text("❌ Cancelled & cleaned up.")
    return ConversationHandler.END

# -----------------------
# Worker loop
# -----------------------
async def worker_loop(app: Application, worker_id: int):
    bot = app.bot
    logger.info("Worker %d started", worker_id)
    while True:
        job = await TASK_QUEUE.get()
        job_id = job.get("job_id")
        JOB_REGISTRY[job_id]["status"] = "started"
        try:
            chat_id = job.get("chat_id")
            reply_message_id = job.get("reply_message_id")
            title = job.get("title")
            short_name = job.get("short_name")
            raw_files = job.get("raw_files", [])
            tmpdirs = job.get("tmpdirs", [])

            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                            text=f"🔁 Job `{job_id}` started — processing {len(raw_files)} file(s)...", parse_mode="Markdown")
            except Exception:
                await bot.send_message(chat_id=chat_id, text=f"🔁 Job `{job_id}` started — processing...")

            processed: List[Dict[str, Any]] = []
            for idx, rf in enumerate(raw_files, start=1):
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                           text=f"🔁 Job `{job_id}` — processing file {idx}/{len(raw_files)}...", parse_mode="Markdown")
                ftype = rf["type"]
                path = rf["path"]
                if ftype == "raster":
                    async with sem:
                        try:
                            outp = Path(path).with_suffix(".webp")
                            await raster_process_async(Path(path), outp, size=512)
                            processed.append({"type": "static", "path": str(outp)})
                        except Exception as e:
                            logger.exception("Raster processing failed for %s", path)
                            await bot.send_message(chat_id=chat_id, text=f"⚠️ Failed to process {Path(path).name}: {e}")
                elif ftype == "tgs":
                    processed.append({"type": "tgs", "path": str(path)})
                elif ftype == "webm":
                    processed.append({"type": "webm", "path": str(path)})
                else:
                    logger.warning("Unknown file type %s", ftype)

            if not processed:
                await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                           text=f"❌ Job `{job_id}` failed — no valid stickers processed.")
                JOB_REGISTRY[job_id]["status"] = "failed"
                continue

            bot_user = await bot.get_me()
            first = processed[0]
            first_path = first["path"]
            first_type = first["type"]

            await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                       text=f"🚀 Job `{job_id}` — creating sticker pack...")
            created_name = await create_sticker_set_with_retries(bot, bot_user.id, short_name, title, first_path, first_type, max_tries=8)

            for idx, s in enumerate(processed[1:], start=2):
                try:
                    await bot.add_sticker_to_set(user_id=bot_user.id, name=created_name, sticker=InputFile(s["path"]), emojis="😀")
                except Exception as e:
                    logger.exception("Failed adding sticker #%d", idx)
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Could not add sticker #{idx}: {e}")

            await bot.edit_message_text(chat_id=chat_id, message_id=reply_message_id,
                                       text=f"🎉 Job `{job_id}` complete!\n👉 https://t.me/addstickers/{created_name}")
            JOB_REGISTRY[job_id]["status"] = "done"
            JOB_REGISTRY[job_id]["result"] = created_name

        except Exception as e:
            logger.exception("Worker error on job %s", job_id)
            try:
                await bot.edit_message_text(chat_id=job.get("chat_id"), message_id=job.get("reply_message_id"),
                                           text=f"❌ Job `{job_id}` failed: {e}")
            except Exception:
                await bot.send_message(chat_id=job.get("chat_id"), text=f"❌ Job `{job_id}` failed: {e}")
            JOB_REGISTRY[job_id]["status"] = "failed"
            JOB_REGISTRY[job_id]["error"] = str(e)
        finally:
            for td in job.get("tmpdirs", []):
                try:
                    shutil.rmtree(td, ignore_errors=True)
                except Exception:
                    logger.exception("Failed to cleanup %s", td)
            TASK_QUEUE.task_done()

# -----------------------
# Admin: queue status
# -----------------------
async def cmd_queue_status(update: Update, ctx: CallbackContext):
    uid = update.effective_user.id
    if ADMIN_USER_IDS and uid not in ADMIN_USER_IDS:
        await update.message.reply_text("Unauthorized")
        return
    qsize = TASK_QUEUE.qsize()
    jobs = [f"{jid}:{meta.get('status','?')}" for jid, meta in JOB_REGISTRY.items()]
    msg = f"queue={qsize}\njobs={len(JOB_REGISTRY)}\n" + ("\n".join(jobs[:50]) if jobs else "none")
    await update.message.reply_text(f"```{msg}```", parse_mode="Markdown")

# -----------------------
# Graceful shutdown helpers
# -----------------------
def _install_sigterm_handler(loop: asyncio.AbstractEventLoop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown(loop, s)))
        except NotImplementedError:
            pass

async def _shutdown(loop: asyncio.AbstractEventLoop, sig):
    logger.info("Received exit signal %s. Shutting down...", sig)
    try:
        await asyncio.sleep(0.1)
    except Exception:
        pass
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.sleep(0.1)
    loop.stop()

# -----------------------
# Main runner
# -----------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("stickers", start_stickers)],
        states={
            ST_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            ST_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files),
                CommandHandler("done", cmd_done_queue),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("qstatus", cmd_queue_status))

    async def runner():
        loop = asyncio.get_running_loop()
        _install_sigterm_handler(loop)
        worker_tasks = []
        for wid in range(max(1, MAX_WORKERS)):
            worker_tasks.append(loop.create_task(worker_loop(app, wid + 1)))
        logger.info("Spawned %d worker(s) (MAX_CONCURRENT=%d)", len(worker_tasks), MAX_CONCURRENT)
        await app.run_polling(stop_signals=None)
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    # if PORT env set, start a small health server (so Render web services detect open port)
    port_env = os.environ.get("PORT")
    if port_env:
        try:
            port_int = int(port_env)
            start_health_http_server(port_int)
        except Exception as e:
            logger.warning("Health server startup failed: %s", e)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, exiting")
    finally:
        logger.info("Bot stopped")

if __name__ == "__main__":
    main()
