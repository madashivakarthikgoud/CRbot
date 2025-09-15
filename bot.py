#!/usr/bin/env python3
"""
Production-ready sticker maker bot for Render.com web services.

Key improvements:
1. Fixed Application.run_polling() API usage
2. Enhanced error handling and logging
3. Better resource cleanup
4. Optimized for Render's free tier limitations
5. Added comprehensive health checks
6. Improved rate limiting
7. Memory optimization for image processing

Set environment variables:
- BOT_TOKEN: Your Telegram bot token
- PORT: Set by Render (required for web service)
- REMBG_ENABLED: "true" to enable background removal (default false)
- MAX_WORKERS: Thread workers (default: 2)
- MAX_CONCURRENT: Concurrent processing slots (default: 2)
- ADMIN_USER_IDS: Comma-separated list of admin user IDs
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
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from io import BytesIO
from dataclasses import dataclass
import json

from telegram import Update, InputFile
from telegram.error import TelegramError, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackContext,
    filters,
    ContextTypes,
)

from PIL import Image, ImageFile

# Allow truncated image loading
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Optional rembg import (only if REMBG_ENABLED=true)
REMBG_ENABLED = os.getenv("REMBG_ENABLED", "").lower() in ("1", "true", "yes")
if REMBG_ENABLED:
    try:
        from rembg import remove  # type: ignore
        REMBG_AVAILABLE = True
    except ImportError:
        logger.warning("REMBG_ENABLED is true but rembg not installed")
        REMBG_AVAILABLE = False
    except Exception as e:
        logger.warning("REMBG_ENABLED is true but rembg failed to load: %s", e)
        REMBG_AVAILABLE = False
else:
    REMBG_AVAILABLE = False

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("sticker-bot")

# Health server for Render web service compatibility
class HealthHandler:
    def __init__(self, port: int):
        self.port = port
        self.server = None
        self.thread = None
    
    def start(self):
        """Start health check HTTP server"""
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            
            class _HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK\n")
                def log_message(self, format, *args):
                    return
            
            self.server = HTTPServer(("0.0.0.0", self.port), _HealthHandler)
            self.thread = threading.Thread(
                target=self.server.serve_forever, 
                name="health-server", 
                daemon=True
            )
            self.thread.start()
            logger.info("Health server listening on 0.0.0.0:%d", self.port)
            return True
        except Exception as e:
            logger.error("Failed to start health server: %s", e)
            return False
    
    def stop(self):
        """Stop health server"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            logger.info("Health server stopped")

# Import threading after logging is configured
import threading

# ---------------------
# Configuration
# ---------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is required")
    raise ValueError("BOT_TOKEN environment variable is required")

# Configuration with defaults
MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "2")))
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT", "2")))
MAX_IMAGE_MB = max(0.1, float(os.getenv("MAX_IMAGE_MB", "10")))
MAX_FILES_PER_JOB = max(1, int(os.getenv("MAX_FILES_PER_JOB", "20")))
RATE_LIMIT_SECONDS = max(0.1, float(os.getenv("RATE_LIMIT_SECONDS", "0.5")))
TEMP_ROOT = os.getenv("TEMP_ROOT", "")

# Parse admin user IDs
try:
    ADMIN_USER_IDS: Set[int] = set(
        int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") 
        if x.strip()
    )
except ValueError:
    ADMIN_USER_IDS = set()
    logger.warning("Invalid ADMIN_USER_IDS format")

# Conversation states
ST_TITLE, ST_IMAGES = range(2)

# File type constants
ALLOWED_RASTER_EXT = (".png", ".jpg", ".jpeg", ".webp")
ALLOWED_ANIM_EXT = (".tgs", ".webm")

# ---------------------
# Data Structures
# ---------------------
@dataclass
class StickerJob:
    """Container for sticker processing job"""
    job_id: str
    user_id: int
    chat_id: int
    reply_message_id: int
    title: str
    short_name: str
    raw_files: List[Dict[str, Any]]
    tmpdirs: List[str]
    status: str = "queued"
    enqueued_at: float = 0.0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None

class StickerBot:
    """Main bot class with improved error handling and resource management"""
    
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS, 
            thread_name_prefix="sticker_worker"
        )
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self.task_queue: asyncio.Queue[StickerJob] = asyncio.Queue()
        self.job_registry: Dict[str, StickerJob] = {}
        self.user_last_action: Dict[int, float] = {}
        self.health_server = None
        self.application = None
        self.worker_tasks = []
        
        # Start health server if PORT is provided
        port = os.getenv("PORT")
        if port:
            try:
                self.health_server = HealthHandler(int(port))
                self.health_server.start()
            except ValueError:
                logger.warning("Invalid PORT value: %s", port)
        
        # Register signal handlers for graceful shutdown
        self.setup_signal_handlers()
    
    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.initiate_shutdown)
        except (RuntimeError, NotImplementedError):
            # Handle cases where signal handling isn't available
            pass
    
    def initiate_shutdown(self):
        """Initiate graceful shutdown"""
        logger.info("Shutdown signal received")
        asyncio.create_task(self.shutdown())
    
    async def shutdown(self):
        """Graceful shutdown procedure"""
        logger.info("Starting graceful shutdown")
        
        # Stop health server
        if self.health_server:
            self.health_server.stop()
        
        # Cancel worker tasks
        for task in self.worker_tasks:
            task.cancel()
        
        # Wait for tasks to complete
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        
        # Shutdown executor
        self.executor.shutdown(wait=True)
        
        # Cleanup temporary directories
        self.cleanup_all_tmpdirs()
        
        logger.info("Shutdown completed")
        os._exit(0)
    
    def cleanup_all_tmpdirs(self):
        """Cleanup all temporary directories from jobs"""
        for job in self.job_registry.values():
            for tmpdir in job.tmpdirs:
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception as e:
                    logger.warning("Failed to cleanup %s: %s", tmpdir, e)
    
    def rate_limit_check(self, user_id: int) -> bool:
        """Check if user is rate limited"""
        current_time = time.time()
        last_action = self.user_last_action.get(user_id, 0)
        
        if current_time - last_action < RATE_LIMIT_SECONDS:
            return False
        
        self.user_last_action[user_id] = current_time
        return True
    
    def ensure_tmpdir(self) -> Path:
        """Create a temporary directory with optional root"""
        if TEMP_ROOT:
            base = Path(TEMP_ROOT)
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="sticker_", dir=base))
        return Path(tempfile.mkdtemp(prefix="sticker_"))
    
    def sanitize_short_name(self, title: str, bot_username: str) -> str:
        """Sanitize sticker pack name"""
        s = re.sub(r"[^a-zA-Z0-9_]", "_", title.strip().lower())
        s = re.sub(r"_+", "_", s).strip("_")
        if not s:
            s = uuid.uuid4().hex[:8]
        name = f"{s}_by_{bot_username}"
        return name[:64]
    
    async def download_file(self, bot, file_id: str, dest_path: Path) -> Path:
        """Download file with size validation"""
        try:
            tf = await bot.get_file(file_id)
            file_size = getattr(tf, 'file_size', 0)
            
            if file_size > 0:
                size_mb = file_size / (1024 * 1024)
                if size_mb > MAX_IMAGE_MB:
                    raise ValueError(
                        f"File too large: {size_mb:.2f} MB (limit {MAX_IMAGE_MB} MB)"
                    )
            
            await tf.download_to_drive(custom_path=str(dest_path))
            return dest_path
            
        except TelegramError as e:
            logger.error("Telegram error downloading file: %s", e)
            raise
        except Exception as e:
            logger.error("Unexpected error downloading file: %s", e)
            raise
    
    def raster_process_sync(self, input_path: Path, output_path: Path, size: int = 512) -> None:
        """Process raster image (with or without background removal)"""
        try:
            with input_path.open("rb") as fh:
                input_bytes = fh.read()
            
            # Process image based on rembg availability and setting
            if REMBG_ENABLED and REMBG_AVAILABLE:
                out_bytes = remove(input_bytes)
                img = Image.open(BytesIO(out_bytes)).convert("RGBA")
            else:
                img = Image.open(BytesIO(input_bytes)).convert("RGBA")
            
            # Resize and center image
            img.thumbnail((size, size), Image.LANCZOS)
            
            # Create square canvas with transparent background
            square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            square.paste(
                img, 
                ((size - img.width) // 2, (size - img.height) // 2), 
                img if img.mode == 'RGBA' else None
            )
            
            # Save as WEBP with optimization
            square.save(
                output_path, 
                format="WEBP", 
                lossless=True, 
                method=6,
                optimize=True
            )
            
        except Exception as e:
            logger.error("Error processing image %s: %s", input_path, e)
            raise
    
    async def raster_process_async(self, input_path: Path, output_path: Path, size: int = 512):
        """Process image in thread pool"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self.executor, 
            self.raster_process_sync, 
            input_path, 
            output_path, 
            size
        )
    
    async def create_sticker_set_with_retries(self, bot, user_id: int, base_name: str, 
                                            title: str, first_path: str, first_type: str, 
                                            max_tries: int = 6) -> str:
        """Create sticker set with retry logic for name conflicts"""
        attempt = 0
        name = base_name
        last_exception = None
        
        while attempt < max_tries:
            try:
                # Determine sticker format
                if first_type == "static":
                    sticker_format = "static"
                elif first_type == "tgs":
                    sticker_format = "animated"
                elif first_type == "webm":
                    sticker_format = "video"
                else:
                    sticker_format = "static"
                
                # Create the sticker set
                await bot.create_new_sticker_set(
                    user_id=user_id,
                    name=name,
                    title=title,
                    stickers=[{"sticker": InputFile(first_path), "emoji_list": ["😀"]}],
                    sticker_format=sticker_format
                )
                
                return name
                
            except TelegramError as e:
                last_exception = e
                error_msg = str(e).lower()
                
                if any(phrase in error_msg for phrase in [
                    "already exists", 
                    "name is already occupied", 
                    "is already occupied"
                ]):
                    # Generate new name and retry
                    suffix = uuid.uuid4().hex[:4]
                    base_cut = base_name[:48]
                    name = f"{base_cut}_{suffix}_by_{user_id}"[:64]
                    attempt += 1
                    await asyncio.sleep(0.5)
                    continue
                else:
                    # Other Telegram errors should be raised
                    raise
            except NetworkError as e:
                # Network errors might be transient, retry
                attempt += 1
                await asyncio.sleep(1.0)
                continue
        
        # If all retries failed
        raise last_exception or RuntimeError(
            f"Failed to create sticker set after {max_tries} attempts"
        )
    
    async def add_sticker_to_set_with_retry(self, bot, user_id: int, set_name: str, 
                                          sticker_path: str, emojis: str = "😀", 
                                          max_retries: int = 3):
        """Add sticker to set with retry logic"""
        for attempt in range(max_retries):
            try:
                await bot.add_sticker_to_set(
                    user_id=user_id,
                    name=set_name,
                    sticker=InputFile(sticker_path),
                    emojis=emojis
                )
                return True
            except (TelegramError, NetworkError) as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

# ---------------------
# Bot Command Handlers
# ---------------------
bot_instance = StickerBot()

async def cmd_start(update: Update, context: CallbackContext):
    """Start command handler"""
    await update.message.reply_text(
        "🎨 Welcome to Sticker Maker Bot!\n\n"
        "Use /stickers to create a new sticker pack\n"
        "Use /help for more information"
    )

async def cmd_help(update: Update, context: CallbackContext):
    """Help command handler"""
    help_text = (
        "🤖 *Sticker Maker Bot*\n\n"
        "*/start* - Start the bot\n"
        "*/stickers* - Create a new sticker pack\n"
        "*/done* - Finish and process your sticker pack\n"
        "*/cancel* - Cancel current operation\n"
        "*/health* - Check bot status\n"
        "*/qstatus* - Queue status (admin only)\n\n"
        "Supported formats:\n"
        "• Images: PNG, JPEG, WEBP\n"
        "• Animated: TGS (Telegram stickers), WEBM\n\n"
        f"Max files per pack: {MAX_FILES_PER_JOB}\n"
        f"Max file size: {MAX_IMAGE_MB}MB\n"
        f"Rate limit: {RATE_LIMIT_SECONDS}s between actions"
    )
    
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cmd_health(update: Update, context: CallbackContext):
    """Health check command"""
    queue_size = bot_instance.task_queue.qsize()
    active_jobs = sum(1 for j in bot_instance.job_registry.values() 
                     if j.status in ['started', 'queued'])
    
    health_status = (
        f"✅ *Bot Status*\n\n"
        f"• Queue size: {queue_size}\n"
        f"• Active jobs: {active_jobs}\n"
        f"• Total jobs: {len(bot_instance.job_registry)}\n"
        f"• Workers: {MAX_WORKERS}\n"
        f"• Concurrent: {MAX_CONCURRENT}\n"
        f"• Rembg: {'Enabled' if REMBG_ENABLED and REMBG_AVAILABLE else 'Disabled'}"
    )
    
    await update.message.reply_text(health_status, parse_mode="Markdown")

async def start_stickers(update: Update, context: CallbackContext) -> int:
    """Start sticker creation conversation"""
    user = update.effective_user
    
    if not bot_instance.rate_limit_check(user.id):
        await update.message.reply_text("⏳ Please wait a moment between actions.")
        return ConversationHandler.END
    
    # Initialize user data
    context.user_data["sticker_data"] = {
        "title": None,
        "short_name": None,
        "raw_files": [],
        "tmpdirs": []
    }
    
    await update.message.reply_text(
        "🎨 Let's create a sticker pack!\n\n"
        "Please send a title for your sticker pack (e.g., `Cute Cats`):",
        parse_mode="Markdown"
    )
    
    return ST_TITLE

async def handle_title(update: Update, context: CallbackContext) -> int:
    """Handle sticker pack title"""
    user = update.effective_user
    
    if not bot_instance.rate_limit_check(user.id):
        await update.message.reply_text("⏳ Please wait a moment between actions.")
        return ConversationHandler.END
    
    title = update.message.text.strip()
    if len(title) > 64:
        await update.message.reply_text(
            "❌ Title too long. Please use 64 characters or less."
        )
        return ST_TITLE
    
    # Generate short name
    bot_username = (await context.bot.get_me()).username
    short_name = bot_instance.sanitize_short_name(title, bot_username)
    
    # Store in user data
    context.user_data["sticker_data"]["title"] = title
    context.user_data["sticker_data"]["short_name"] = short_name
    
    await update.message.reply_text(
        f"✅ Title set: *{title}*\n\n"
        "Now send me your images/stickers:\n"
        "• PNG/JPEG/WEBP for static stickers\n"
        "• TGS for animated stickers\n"
        "• WEBM for video stickers\n\n"
        f"Maximum {MAX_FILES_PER_JOB} files per pack.\n"
        f"Maximum {MAX_IMAGE_MB}MB per file.\n\n"
        "Send /done when finished or /cancel to abort.",
        parse_mode="Markdown"
    )
    
    return ST_IMAGES

async def handle_files(update: Update, context: CallbackContext) -> int:
    """Handle file uploads"""
    user = update.effective_user
    
    if not bot_instance.rate_limit_check(user.id):
        await update.message.reply_text("⏳ Please wait a moment between actions.")
        return ST_IMAGES
    
    sticker_data = context.user_data.get("sticker_data")
    if not sticker_data:
        await update.message.reply_text("❌ Please start with /stickers first.")
        return ConversationHandler.END
    
    # Check file limit
    if len(sticker_data["raw_files"]) >= MAX_FILES_PER_JOB:
        await update.message.reply_text(
            f"❌ Maximum {MAX_FILES_PER_JOB} files reached. "
            "Send /done to process or /cancel to abort."
        )
        return ST_IMAGES
    
    # Create temp directory if needed
    if not sticker_data.get("tmpdirs"):
        tmpdir = bot_instance.ensure_tmpdir()
        sticker_data["tmpdirs"] = [str(tmpdir)]
    else:
        tmpdir = Path(sticker_data["tmpdirs"][0])
    
    msg = update.message
    try:
        # Determine file source and ID
        if msg.photo:
            file_id = msg.photo[-1].file_id
            filename = f"{uuid.uuid4().hex}.jpg"
        elif msg.document:
            file_id = msg.document.file_id
            filename = msg.document.file_name or f"{uuid.uuid4().hex}"
        else:
            await msg.reply_text(
                "❌ Please send a photo or document file.\n\n"
                "Supported formats:\n"
                "• Images: PNG, JPEG, WEBP\n"
                "• Animated: TGS, WEBM"
            )
            return ST_IMAGES
        
        # Check file extension
        file_ext = Path(filename).suffix.lower()
        if (file_ext not in ALLOWED_RASTER_EXT and 
            file_ext not in ALLOWED_ANIM_EXT):
            await msg.reply_text(
                f"❌ Unsupported file type: {file_ext}\n\n"
                "Supported formats:\n"
                "• Images: PNG, JPEG, WEBP\n"
                "• Animated: TGS, WEBM"
            )
            return ST_IMAGES
        
        # Download file
        dest_path = tmpdir / filename
        await bot_instance.download_file(context.bot, file_id, dest_path)
        
        # Determine file type
        if file_ext in ALLOWED_ANIM_EXT:
            file_type = "tgs" if file_ext == ".tgs" else "webm"
            await msg.reply_text(f"✅ {file_type.upper()} sticker added to queue!")
        else:
            file_type = "raster"
            await msg.reply_text("✅ Image added to queue!")
        
        # Store file info
        sticker_data["raw_files"].append({
            "type": file_type,
            "path": str(dest_path),
            "original_name": filename
        })
        
        # Send progress update
        remaining = MAX_FILES_PER_JOB - len(sticker_data["raw_files"])
        if remaining > 0:
            await msg.reply_text(
                f"📊 {len(sticker_data['raw_files'])} files queued. "
                f"{remaining} slots remaining."
            )
        
        return ST_IMAGES
        
    except ValueError as e:
        await msg.reply_text(f"❌ {e}")
        return ST_IMAGES
    except Exception as e:
        logger.error("Error handling file: %s", e)
        await msg.reply_text("❌ Failed to process file. Please try again.")
        return ST_IMAGES

async def cmd_done_queue(update: Update, context: CallbackContext) -> int:
    """Finish and queue sticker pack for processing"""
    user = update.effective_user
    
    if not bot_instance.rate_limit_check(user.id):
        await update.message.reply_text("⏳ Please wait a moment between actions.")
        return ConversationHandler.END
    
    sticker_data = context.user_data.get("sticker_data")
    if not sticker_data or not sticker_data.get("raw_files"):
        await update.message.reply_text("❌ No files added. Please send some files first.")
        return ST_IMAGES
    
    # Create job
    job_id = uuid.uuid4().hex[:10]
    job = StickerJob(
        job_id=job_id,
        user_id=user.id,
        chat_id=update.effective_chat.id,
        reply_message_id=update.message.message_id,
        title=sticker_data["title"],
        short_name=sticker_data["short_name"],
        raw_files=sticker_data["raw_files"],
        tmpdirs=sticker_data["tmpdirs"],
        enqueued_at=time.time()
    )
    
    # Add to registry and queue
    bot_instance.job_registry[job_id] = job
    await bot_instance.task_queue.put(job)
    
    # Send confirmation
    queue_msg = await update.message.reply_text(
        f"⏳ Job `{job_id}` queued for processing.\n"
        f"Position in queue: {bot_instance.task_queue.qsize()}\n\n"
        "I'll notify you when it's done!",
        parse_mode="Markdown"
    )
    
    job.reply_message_id = queue_msg.message_id
    
    # Clear user data
    context.user_data.pop("sticker_data", None)
    
    logger.info("Job %s queued by user %s (%d files)", 
                job_id, user.username or user.id, len(job.raw_files))
    
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: CallbackContext) -> int:
    """Cancel current operation"""
    sticker_data = context.user_data.get("sticker_data")
    
    if sticker_data and "tmpdirs" in sticker_data:
        for tmpdir in sticker_data["tmpdirs"]:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                logger.warning("Failed to cleanup %s: %s", tmpdir, e)
    
    context.user_data.pop("sticker_data", None)
    await update.message.reply_text("❌ Operation cancelled and cleaned up.")
    
    return ConversationHandler.END

async def cmd_queue_status(update: Update, context: CallbackContext):
    """Queue status (admin only)"""
    user = update.effective_user
    
    if ADMIN_USER_IDS and user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Admin access required.")
        return
    
    qsize = bot_instance.task_queue.qsize()
    jobs = []
    
    for job_id, job in list(bot_instance.job_registry.items())[:20]:  # Limit output
        jobs.append(
            f"{job_id}: {job.status} "
            f"(files: {len(job.raw_files)}, "
            f"user: {job.user_id})"
        )
    
    status_text = (
        f"Queue size: {qsize}\n"
        f"Total jobs: {len(bot_instance.job_registry)}\n"
        f"Active workers: {len(bot_instance.worker_tasks)}\n\n"
        "Recent jobs:\n" + "\n".join(jobs) if jobs else "No jobs in registry"
    )
    
    await update.message.reply_text(f"```\n{status_text}\n```", parse_mode="Markdown")

# ---------------------
# Worker System
# ---------------------
async def worker_loop(worker_id: int):
    """Worker process for handling sticker jobs"""
    logger.info("Worker %d started", worker_id)
    
    while True:
        try:
            # Get job from queue
            job = await bot_instance.task_queue.get()
            job.started_at = time.time()
            job.status = "processing"
            
            logger.info("Worker %d processing job %s", worker_id, job.job_id)
            
            # Process the job
            await process_job(job)
            
            # Mark task as done
            bot_instance.task_queue.task_done()
            
        except asyncio.CancelledError:
            logger.info("Worker %d cancelled", worker_id)
            break
        except Exception as e:
            logger.error("Worker %d error: %s", worker_id, e)
            try:
                # Try to mark failed job
                if job:
                    job.status = "failed"
                    job.error = str(e)
                    job.completed_at = time.time()
            except:
                pass

async def process_job(job: StickerJob):
    """Process a single sticker job"""
    bot = bot_instance.application.bot
    
    try:
        # Update status
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.reply_message_id,
            text=f"🔨 Processing job `{job.job_id}`:\n"
                 f"Converting {len(job.raw_files)} files...",
            parse_mode="Markdown"
        )
        
        processed_files = []
        
        # Process each file
        for idx, file_info in enumerate(job.raw_files, 1):
            file_type = file_info["type"]
            file_path = Path(file_info["path"])
            
            # Update progress
            if idx % 3 == 0 or idx == len(job.raw_files):  # Don't update too frequently
                try:
                    await bot.edit_message_text(
                        chat_id=job.chat_id,
                        message_id=job.reply_message_id,
                        text=f"🔨 Processing job `{job.job_id}`:\n"
                             f"File {idx}/{len(job.raw_files)}",
                        parse_mode="Markdown"
                    )
                except:
                    pass  # Don't fail job if edit fails
            
            # Process based on file type
            if file_type == "raster":
                output_path = file_path.with_suffix(".webp")
                try:
                    async with bot_instance.semaphore:
                        await bot_instance.raster_process_async(
                            file_path, output_path, 512
                        )
                    processed_files.append({
                        "type": "static",
                        "path": str(output_path)
                    })
                except Exception as e:
                    logger.error("Failed to process %s: %s", file_path, e)
                    await bot.send_message(
                        job.chat_id,
                        f"⚠️ Failed to process {file_info['original_name']}: {e}"
                    )
            else:
                # Animated stickers (TGS, WEBM) don't need processing
                processed_files.append({
                    "type": file_type,
                    "path": str(file_path)
                })
        
        if not processed_files:
            raise ValueError("No files were successfully processed")
        
        # Create sticker set
        bot_user = await bot.get_me()
        first_file = processed_files[0]
        
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.reply_message_id,
            text=f"🚀 Creating sticker pack for job `{job.job_id}`...",
            parse_mode="Markdown"
        )
        
        sticker_set_name = await bot_instance.create_sticker_set_with_retries(
            bot, bot_user.id, job.short_name, job.title,
            first_file["path"], first_file["type"]
        )
        
        # Add remaining stickers
        success_count = 1  # First sticker already added
        
        for idx, sticker_file in enumerate(processed_files[1:], 2):
            try:
                await bot_instance.add_sticker_to_set_with_retry(
                    bot, bot_user.id, sticker_set_name,
                    sticker_file["path"]
                )
                success_count += 1
            except Exception as e:
                logger.error("Failed to add sticker %d: %s", idx, e)
                await bot.send_message(
                    job.chat_id,
                    f"⚠️ Could not add sticker #{idx}: {e}"
                )
        
        # Send success message
        success_text = (
            f"🎉 Sticker pack completed!\n\n"
            f"• Pack: {job.title}\n"
            f"• Added: {success_count}/{len(processed_files)} stickers\n"
            f"• URL: https://t.me/addstickers/{sticker_set_name}"
        )
        
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.reply_message_id,
            text=success_text
        )
        
        # Update job status
        job.status = "completed"
        job.result = sticker_set_name
        job.completed_at = time.time()
        
        logger.info("Job %s completed successfully", job.job_id)
        
    except Exception as e:
        logger.error("Job %s failed: %s", job.job_id, e)
        
        # Update job status
        job.status = "failed"
        job.error = str(e)
        job.completed_at = time.time()
        
        # Notify user
        error_msg = (
            f"❌ Job `{job.job_id}` failed:\n"
            f"Error: {str(e)[:200]}"
        )
        
        try:
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.reply_message_id,
                text=error_msg,
                parse_mode="Markdown"
            )
        except:
            try:
                await bot.send_message(
                    job.chat_id,
                    error_msg,
                    parse_mode="Markdown"
                )
            except:
                pass  # Final fallback
        
    finally:
        # Cleanup temporary files
        for tmpdir in job.tmpdirs:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                logger.warning("Failed to cleanup %s: %s", tmpdir, e)

# ---------------------
# Application Setup
# ---------------------
def main():
    """Main application entry point"""
    logger.info("Starting Sticker Bot")
    
    # Create application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    
    bot_instance.application = application
    
    # Add conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("stickers", start_stickers)],
        states={
            ST_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            ST_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_files),
                CommandHandler("done", cmd_done_queue)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("help", cmd_help)
        ],
        per_user=True,
        per_chat=True,
        conversation_timeout=300  # 5 minutes timeout
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("qstatus", cmd_queue_status))
    application.add_handler(conv_handler)
    
    # Start worker tasks
    async def post_init(application: Application):
        """Post-initialization setup"""
        # Start worker tasks
        for i in range(MAX_WORKERS):
            task = asyncio.create_task(worker_loop(i + 1))
            bot_instance.worker_tasks.append(task)
        
        logger.info("Started %d worker tasks", MAX_WORKERS)
    
    async def post_shutdown(application: Application):
        """Post-shutdown cleanup"""
        # Initiate graceful shutdown
        await bot_instance.shutdown()
    
    # Set up application callbacks
    application.post_init = post_init
    application.post_shutdown = post_shutdown
    
    # Start the bot
    logger.info("Starting bot with polling...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()