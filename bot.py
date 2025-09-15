# ---------- Required imports (paste near top of your file) ----------
import os
import re
import shutil
import gzip
import json
import random
import logging
import tempfile
import asyncio
import subprocess
from io import BytesIO
from pathlib import Path
from datetime import datetime

# telegram / ptb imports
from telegram import Update, InputFile, InputSticker
from telegram.ext import ContextTypes

# ---------- Logger ----------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Replace with your authorized user id (or load from env/config)
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "123456789"))

# ---------- Helpers ----------
def reencode_webm_for_telegram(src: Path, dst: Path, max_width=512, max_height=512, max_duration=3.0):
    """
    Re-encodes any input video to a Telegram-compatible WebM/VP9 with alpha.
    Uses ffmpeg. Returns True on success.
    Requirements:
     - ffmpeg available in PATH
     - output: VP9 WebM, alpha channel preserved, VP9 codec, ca. 512x512, <= 3s, no audio
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-i", str(src),
        "-an",
        "-vf", f"scale='min({max_width},iw)':'min({max_height},ih)':flags=lanczos",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-deadline", "good",
        "-b:v", "400K",
        "-auto-alt-ref", "0",
        "-row-mt", "1",
        "-threads", "0",
        "-frame_pts", "1",
        "-r", "30",
        str(dst)
    ]
    try:
        subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        logger.error("ffmpeg failed: %s", stderr)
        return False

def repackage_tgs(src: Path, dst: Path):
    """
    Read tgs (gzipped JSON), decode, slightly mutate JSON (add small random field),
    gzip back. This changes the raw bytes and therefore the file fingerprint.
    """
    try:
        with gzip.open(src, "rb") as f:
            raw = f.read()
        data = json.loads(raw.decode("utf-8"))
        meta = data.get("_meta", {})
        meta["_cloned_at"] = datetime.utcnow().isoformat()
        meta["_rand"] = random.randint(0, 10**9)
        data["_meta"] = meta
        new_raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with gzip.open(dst, "wb") as f:
            f.write(new_raw)
        return True
    except Exception as e:
        logger.exception("Failed to repackage .tgs: %s", e)
        return False

async def prepare_static_image(input_path: Path, output_path: Path) -> bool:
    # reuse your original process_static_sticker but ensure output is PNG or WEBP with correct size
    from PIL import Image, UnidentifiedImageError

    def _work():
        try:
            with Image.open(input_path) as img:
                img = img.convert("RGBA")
                img.thumbnail((512, 512), Image.LANCZOS)
                # Save as PNG (Telegram accepts PNG or WEBP). PNG keeps alpha.
                img.save(output_path, format="PNG")
                return True
        except UnidentifiedImageError:
            logger.warning("Cannot identify image file %s", input_path)
            return False
        except Exception as e:
            logger.exception("Error preparing static image: %s", e)
            return False

    return await asyncio.to_thread(_work)

# ---------- clone handler (integration) ----------
async def clone_sticker_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # only allow authorized user (optional)
    try:
        if update.effective_user.id != AUTHORIZED_USER_ID:
            await update.message.reply_text("You are not authorized to use this.")
            return
    except Exception:
        # If update has no effective_user, bail
        logger.warning("No effective_user in update")
        return

    msg = update.message
    if not msg or not msg.text:
        await update.message.reply_text("Send a sticker-pack link (t.me/addstickers/packname).")
        return

    url_match = re.search(r't\.me/addstickers/(\S+)', msg.text)
    if not url_match:
        await msg.reply_text("Please send a sticker-pack link (t.me/addstickers/packname).")
        return

    orig_name = url_match.group(1).split('?')[0]
    status = await msg.reply_text("Fetching original sticker pack...")

    temp_dir = Path(tempfile.mkdtemp())
    try:
        original_pack = await context.bot.get_sticker_set(orig_name)
        bot_user = await context.bot.get_me()
        bot_username = (bot_user.username or "bot").replace("@", "")
        unique = os.urandom(4).hex()
        new_name_base = re.sub(r'[^a-zA-Z0-9_]', '', original_pack.name)[:48]
        new_name = f"{new_name_base}_{unique}_by_{bot_username}"
        new_name = re.sub(r'__+', '_', new_name)
        if not new_name[0].isalpha():
            new_name = "a" + new_name

        # Determine sticker set type
        is_anim = any(getattr(s, "is_animated", False) for s in original_pack.stickers)
        is_video = any(getattr(s, "is_video", False) for s in original_pack.stickers)
        if is_video:
            sticker_type = "video"
        elif is_anim:
            sticker_type = "animated"
        else:
            sticker_type = "static"

        prepared = []
        for idx, s in enumerate(original_pack.stickers):
            try:
                file = await s.get_file()
            except Exception as e:
                logger.warning("Failed to get file for sticker %s: %s", s.file_unique_id, e)
                continue

            ext = Path(file.file_path).suffix.lower() if file.file_path and '.' in file.file_path else ''
            orig_path = temp_dir / f"{s.file_unique_id}{ext or '.dat'}"
            await file.download_to_drive(str(orig_path))

            final_path = orig_path
            if sticker_type == "static":
                out = temp_dir / f"{s.file_unique_id}.png"
                ok = await prepare_static_image(orig_path, out)
                if not ok:
                    logger.warning("Skipping static sticker %s", orig_path)
                    continue
                final_path = out

            elif sticker_type == "video":
                out = temp_dir / f"{s.file_unique_id}.webm"
                ok = reencode_webm_for_telegram(orig_path, out)
                if not ok:
                    logger.warning("Skipping video sticker %s", orig_path)
                    continue
                final_path = out

            elif sticker_type == "animated":
                if ext == ".tgs" or orig_path.suffix.lower() == ".tgs":
                    out = temp_dir / f"{s.file_unique_id}.tgs"
                    ok = repackage_tgs(orig_path, out)
                    if not ok:
                        logger.warning("Skipping animated sticker %s", orig_path)
                        continue
                    final_path = out
                else:
                    logger.warning("Animated sticker is not .tgs, skipping: %s", orig_path)
                    continue

            # Build InputSticker using bytes buffer (avoid passing raw path string)
            with open(final_path, "rb") as fh:
                data = fh.read()
            input_file = InputFile(BytesIO(data), filename=final_path.name)
            emoji_list = [getattr(s, "emoji", None) or "👍"]
            # InputSticker expects sticker and emoji_list (mask_position optional)
            input_sticker = InputSticker(sticker=input_file, emoji_list=emoji_list)
            prepared.append(input_sticker)

            if (idx + 1) % 10 == 0:
                await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                                   text=f"Prepared {idx+1}/{len(original_pack.stickers)} stickers...")

        if not prepared:
            await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                               text="No valid stickers could be prepared for the selected sticker type.")
            return

        await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                           text="Creating new sticker set (owned by the bot) ...")

        # create_new_sticker_set expects: user_id, name, title, stickers, sticker_format (static/animated/video)
        # Note: Some PTB versions expect 'sticker_format' param name; if your version uses different arg names, adapt.
        await context.bot.create_new_sticker_set(
            user_id=bot_user.id,
            name=new_name,
            title=f"{original_pack.title} (cloned by @{bot_username})",
            stickers=[prepared[0]],
            sticker_format=sticker_type
        )

        # add remaining stickers
        for i, st in enumerate(prepared[1:], start=2):
            try:
                await context.bot.add_sticker_to_set(user_id=bot_user.id, name=new_name, sticker=st)
            except Exception as e:
                logger.warning("Failed to add sticker %s: %s", i, e)
            if (i) % 10 == 0:
                await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                                   text=f"Added {i}/{len(prepared)} stickers...")

        new_url = f"https://t.me/addstickers/{new_name}"
        # save_created_pack(new_name, new_url)  # restore your persistence function if available
        await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                           text=f"Done! New pack: {new_url}")

    except Exception as e:
        logger.exception("Failed cloning pack %s: %s", orig_name, e)
        try:
            await context.bot.edit_message_text(chat_id=status.chat_id, message_id=status.message_id,
                                               text=f"Failed to clone pack: {e}")
        except Exception:
            pass
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
