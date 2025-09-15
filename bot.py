# bot.py
import os
import re
import uuid
import shutil
import logging
import tempfile
from pathlib import Path
from typing import Dict, List

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackContext, filters
)

# image processing
from PIL import Image, ImageOps
from rembg import remove

# ─── Logging ──────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("sticker-bot")

# ─── States ───────────────────────────────
(STICKER_TITLE, STICKER_IMAGES) = range(200, 202)

# ─── Helpers ──────────────────────────────
def safe_short_name(title: str, bot_username: str) -> str:
    # Telegram sticker set requirement: short name must be unique and contain only a-zA-Z0-9_
    # We enforce suffix `_by_<botusername>`
    s = re.sub(r"[^a-zA-Z0-9_]", "_", title.lower())
    return f"{s}_by_{bot_username}"

async def download_file(bot, file_id: str, dest_path: Path) -> Path:
    """Download file by file_id to dest_path (async)."""
    f = await bot.get_file(file_id)
    await f.download_to_drive(custom_path=str(dest_path))
    return dest_path

def convert_raster_to_webp(input_path: Path, output_path: Path, size: int = 512):
    """
    - Removes background (rembg) and converts to 512x512 webp as required by Telegram static stickers.
    - Keeps aspect ratio and pads with transparent background.
    """
    # open input
    img = Image.open(input_path).convert("RGBA")

    # remove bg using rembg
    try:
        img_bytes = img.tobytes()
    except Exception:
        # fallback to saving and using remove() on bytes
        pass

    # rembg expects bytes; we'll use remove on raw bytes of file
    with input_path.open("rb") as fh:
        input_bytes = fh.read()
    output_bytes = remove(input_bytes)  # returns bytes (PNG)
    from io import BytesIO
    img = Image.open(BytesIO(output_bytes)).convert("RGBA")

    # Resize with aspect preserved and pad to square 512x512
    img.thumbnail((size, size), Image.LANCZOS)
    # create transparent square
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # center
    x = (size - img.width) // 2
    y = (size - img.height) // 2
    square.paste(img, (x, y), img)
    # save as webp with lossless and alpha
    square.save(output_path, format="WEBP", lossless=True, method=6)

# ─── Sticker Flow Handlers ─────────────────
async def start_sticker_flow(update: Update, ctx: CallbackContext) -> int:
    """Begin sticker creation flow."""
    ctx.user_data["sticker_data"] = {"title": None, "short_name": None, "stickers": []}
    await update.message.reply_text(
        "🎨 *Create sticker pack*\n\n"
        "Send a title for the sticker pack (example: `Cool Cats`) — pack will be created under this bot's name.",
        parse_mode="Markdown"
    )
    return STICKER_TITLE

async def handle_sticker_title(update: Update, ctx: CallbackContext) -> int:
    title = update.message.text.strip()
    bot_username = (await ctx.bot.get_me()).username
    short_name = safe_short_name(title, bot_username)

    # store
    ctx.user_data["sticker_data"]["title"] = title
    ctx.user_data["sticker_data"]["short_name"] = short_name

    await update.message.reply_text(
        f"✅ Title saved: *{title}*\n\n"
        "Now send images (PNG/JPEG/WEBP) and I will auto-remove background and prepare stickers.\n"
        "You can also send animated `.tgs` or `.webm` (video stickers).\n\n"
        "Send multiple images. When finished, send /done",
        parse_mode="Markdown"
    )
    return STICKER_IMAGES

async def handle_sticker_file(update: Update, ctx: CallbackContext) -> int:
    """
    Accepts: photo, document (png/webp/jpeg), tgs, webm
    For raster images: downloads, runs rembg -> convert to 512 webp.
    For tgs/webm: downloads as-is and uses those files directly.
    """
    msg = update.message
    d = ctx.user_data["sticker_data"]
    tmpdir = Path(tempfile.mkdtemp(prefix="sticker_"))
    ctx.user_data.setdefault("_tmpdirs", []).append(str(tmpdir))

    # determine file_id and mime
    file_id = None
    mime = None
    filename = None

    if msg.photo:
        file_id = msg.photo[-1].file_id
        mime = "image/jpeg"
        filename = f"{uuid.uuid4().hex}.jpg"
    elif msg.document:
        file_id = msg.document.file_id
        mime = msg.document.mime_type
        filename = msg.document.file_name or f"{uuid.uuid4().hex}"
    else:
        await msg.reply_text("❌ Send a photo or a document file (png/webp/jpg/tgs/webm).")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return STICKER_IMAGES

    dest_path = tmpdir / filename
    try:
        await download_file(ctx.bot, file_id, dest_path)
    except Exception as e:
        logger.exception("download failed")
        await msg.reply_text(f"❌ Failed to download file: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return STICKER_IMAGES

    # handle types
    lower = filename.lower()
    try:
        if mime == "application/x-tgsticker" or lower.endswith(".tgs"):
            # animated sticker file (Lottie) - use directly
            d["stickers"].append({"type": "tgs", "path": str(dest_path)})
            await msg.reply_text("✅ Animated sticker (.tgs) accepted.")
            return STICKER_IMAGES

        if mime == "video/webm" or lower.endswith(".webm"):
            # video sticker (WEBM) - use directly
            d["stickers"].append({"type": "webm", "path": str(dest_path)})
            await msg.reply_text("✅ Video sticker (.webm) accepted.")
            return STICKER_IMAGES

        # else treat as raster image -> convert + bg removal
        # Accept png/jpg/webp
        # Convert to png first if needed
        input_ext = dest_path.suffix.lower()
        raster_input = dest_path
        # run rembg+convert
        output_webp = tmpdir / f"{uuid.uuid4().hex}.webp"
        try:
            convert_raster_to_webp(raster_input, output_webp, size=512)
        except Exception as e:
            logger.exception("convert failed")
            await msg.reply_text(f"❌ Image processing failed: {e}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return STICKER_IMAGES

        d["stickers"].append({"type": "static", "path": str(output_webp)})
        await msg.reply_text("✅ Image processed and added as sticker.")
        return STICKER_IMAGES

    except Exception as e:
        logger.exception("handle file")
        await msg.reply_text(f"❌ Unexpected error: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return STICKER_IMAGES

async def finish_sticker_pack(update: Update, ctx: CallbackContext) -> int:
    d = ctx.user_data.get("sticker_data", {})
    if not d or not d.get("stickers"):
        await update.message.reply_text("⚠️ You didn't add any stickers.")
        return ConversationHandler.END

    bot = ctx.bot
    bot_user = await bot.get_me()
    short_name = d["short_name"]
    title = d["title"]

    tmpdirs: List[str] = ctx.user_data.get("_tmpdirs", [])

    try:
        # create the set using the bot identity
        first = d["stickers"][0]

        if first["type"] == "static":
            # for create_new_sticker_set, we need to provide a file-like or InputFile path
            await bot.create_new_sticker_set(
                user_id=bot_user.id,
                name=short_name,
                title=title,
                stickers=[
                    {"sticker": InputFile(first["path"]), "emoji_list": ["😀"]}
                ],
                sticker_format="static",
            )
        elif first["type"] == "tgs":
            await bot.create_new_sticker_set(
                user_id=bot_user.id,
                name=short_name,
                title=title,
                stickers=[
                    {"sticker": InputFile(first["path"]), "emoji_list": ["😀"]}
                ],
                sticker_format="animated",
            )
        elif first["type"] == "webm":
            await bot.create_new_sticker_set(
                user_id=bot_user.id,
                name=short_name,
                title=title,
                stickers=[
                    {"sticker": InputFile(first["path"]), "emoji_list": ["😀"]}
                ],
                sticker_format="video",
            )
        else:
            raise RuntimeError("Unknown sticker type for first sticker")

        # add the rest
        for s in d["stickers"][1:]:
            if s["type"] == "static":
                await bot.add_sticker_to_set(
                    user_id=bot_user.id,
                    name=short_name,
                    sticker=InputFile(s["path"]),
                    emojis="😀"
                )
            elif s["type"] == "tgs":
                await bot.add_sticker_to_set(
                    user_id=bot_user.id,
                    name=short_name,
                    sticker=InputFile(s["path"]),
                    emojis="😀"
                )
            elif s["type"] == "webm":
                await bot.add_sticker_to_set(
                    user_id=bot_user.id,
                    name=short_name,
                    sticker=InputFile(s["path"]),
                    emojis="😀"
                )

        await update.message.reply_text(f"🎉 Sticker pack created!\n👉 https://t.me/addstickers/{short_name}")

    except Exception as e:
        logger.exception("create pack failed")
        await update.message.reply_text(f"❌ Failed to create sticker pack:\n{e}")

    finally:
        # cleanup tempdirs
        for td in tmpdirs:
            shutil.rmtree(td, ignore_errors=True)
        ctx.user_data.pop("_tmpdirs", None)
        ctx.user_data.pop("sticker_data", None)

    return ConversationHandler.END

async def cancel_stickers(update: Update, ctx: CallbackContext) -> int:
    # cleanup
    tmpdirs = ctx.user_data.get("_tmpdirs", [])
    for td in tmpdirs:
        shutil.rmtree(td, ignore_errors=True)
    ctx.user_data.pop("_tmpdirs", None)
    ctx.user_data.pop("sticker_data", None)
    await update.message.reply_text("❌ Sticker creation cancelled.")
    return ConversationHandler.END

async def help_command(update: Update, ctx: CallbackContext):
    await update.message.reply_text(
        "🤖 *Sticker Maker (Anonymous)*\n\n"
        "• /stickers — start creating a new sticker pack (bot will own pack)\n"
        "• Upload PNG/JPEG/WEBP for automatic bg removal\n"
        "• Upload .tgs or .webm for animated/video stickers\n"
        "• /done — finish and publish pack\n"
        "• /cancel — cancel and clean up",
        parse_mode="Markdown"
    )

def add_sticker_handlers(app: Application):
    sticker_conv = ConversationHandler(
        entry_points=[CommandHandler("stickers", start_sticker_flow)],
        states={
            STICKER_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sticker_title)],
            STICKER_IMAGES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_sticker_file),
                CommandHandler("done", finish_sticker_pack)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_stickers)],
        per_user=True,
        per_chat=True
    )
    app.add_handler(sticker_conv)
    app.add_handler(CommandHandler("help", help_command))

# ─── Main ─────────────────────────────────────
def main() -> None:
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "").rstrip("/")
    PORT = int(os.environ.get("PORT", "8443"))

    if not BOT_TOKEN or not WEBHOOK_URL_BASE:
        logger.error("BOT_TOKEN and WEBHOOK_URL_BASE must be set in environment")
        raise SystemExit("Missing env vars")

    app = Application.builder().token(BOT_TOKEN).build()
    add_sticker_handlers(app)

    logger.info("Starting webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL_BASE}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
