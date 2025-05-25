import os
import re
import logging
from datetime import datetime
from typing import List
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    ChatMember,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackContext,
    CallbackQueryHandler,
    filters,
    JobQueue,
)

# ─── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
CHANNEL_ID    = int(os.getenv("CHANNEL_ID", "0"))
DISCUSSION_ID = int(os.getenv("DISCUSSION_ID", "0"))
if not BOT_TOKEN or CHANNEL_ID == 0 or DISCUSSION_ID == 0:
    raise EnvironmentError("Missing BOT_TOKEN, CHANNEL_ID, or DISCUSSION_ID")

# ─── States ────────────────────────────────────────────────────────────────────
(
    BANNER,
    TAGS,
    TITLE,
    DEVICE,
    MAINTAINER,
    SCREENSHOTS,
    CHANGELOG,
    DEVICE_CHANGELOG,
    DOWNLOAD_LINKS,
    DONATE_LINK,
    README,
    NOTES,
    CONFIRM,
) = range(13)

URL_REGEX = re.compile(r"https?://\S+")

# ─── Data Manager ───────────────────────────────────────────────────────────────
class PostDataManager:
    @staticmethod
    def initialize(ctx: CallbackContext):
        # reset and set dynamic build_date
        ctx.user_data["post_data"] = {
            "banner": None,
            "tags": [],
            "title": None,
            "device": None,
            "maintainer": None,
            "screenshots": [],
            "changelog": None,
            "device_changelog": None,
            "download_links": {},
            "donate": None,
            "readme": None,
            "notes": None,
            "build_date": datetime.now().strftime("%d/%m/%Y"),
        }

    @staticmethod
    async def post_text_discussion(ctx: CallbackContext, heading: str, text: str) -> str:
        # Post long text with heading to discussion
        msg = await ctx.bot.send_message(
            chat_id=DISCUSSION_ID,
            text=f"<b>{heading}</b>\n\n{text}",
            parse_mode="HTML",
            disable_notification=True
        )
        return f"https://t.me/c/{msg.chat.id}/{msg.message_id}".replace("-100", "")

    @staticmethod
    async def post_photo_discussion(ctx: CallbackContext, heading: str, file_id: str) -> str:
        # Post photo with caption heading to discussion
        msg = await ctx.bot.send_photo(
            chat_id=DISCUSSION_ID,
            photo=file_id,
            caption=f"<b>{heading}</b>",
            parse_mode="HTML",
            disable_notification=True
        )
        return f"https://t.me/c/{msg.chat.id}/{msg.message_id}".replace("-100", "")

    @staticmethod
    def build_caption(ctx: CallbackContext) -> str:
        d = ctx.user_data["post_data"]
        parts: List[str] = []
        # Hashtags
        if d["tags"]:
            parts.append(" ".join(d["tags"]))
        # Header
        parts.append(f"<b>{d['title']}</b>")
        parts.append(f"for {d['device']} is now available!")
        parts.append(f"By {d['maintainer']}\n")
        # Links
        links: List[str] = []
        if d["screenshots"]:
            links.append(f"▫️ Screenshots: <a href=\"{d['screenshots'][0]}\">Here</a>")
        if d["changelog"]:
            links.append(f"▫️ Changelog: <a href=\"{d['changelog']}\">Here</a>")
        if d["device_changelog"]:
            links.append(f"▫️ Device Changelog: <a href=\"{d['device_changelog']}\">Here</a>")
        if d["download_links"]:
            dl = " | ".join(
                f"<a href=\"{url}\">{variant}</a>" for variant, url in d["download_links"].items()
            )
            links.append(f"▫️ Download: {dl}")
        if d["readme"]:
            links.append(f"▫️ Read: <a href=\"{d['readme']}\">Here</a>")
        # support
        links.append("▫️ Support: <a href=\"https://t.me/POCOHUB_X3ChatEN\">Here</a>")
        if d["donate"]:
            links.append(f"▫️ Donate: <a href=\"{d['donate']}\">Here</a>")
        parts.extend(links)
        parts.append("")
        # Notes
        if d["notes"]:
            parts.append("📝 Notes:")
            for line in d["notes"].split("\n"):
                parts.append(f"- {line}")
            parts.append("")
        # Footer
        parts.append("Follow: @POCOHUB_X3EN")
        parts.append("Join: @POCOHUB_X3ChatEN")
        parts.append("Gcam: @SuryaKarna_GcamDiscussion")
        parts.append("TG Mirror: @suryarom")
        parts.append(f"Updated - {d['build_date']}")
        return "\n".join(parts)

# ─── Handlers ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: CallbackContext) -> int:
    member = await ctx.bot.get_chat_member(CHANNEL_ID, update.effective_user.id)
    if member.status not in ("creator", "administrator"):
        await update.message.reply_text("❌ You must be a channel admin.")
        return ConversationHandler.END
    PostDataManager.initialize(ctx)
    await update.message.reply_text(
        "🤖 *ROM Post Bot* by *Shiva Karthik*\nGitHub: https://github.com/madashivakarthikgoud\n\n📸 Send banner image or /skip to omit:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return BANNER

async def handle_banner(update: Update, ctx: CallbackContext) -> int:
    if update.message.photo:
        ctx.user_data["post_data"]["banner"] = update.message.photo[-1].file_id
    await update.message.reply_text("📝 Enter hashtags (#tag1 #tag2) or /skip:")
    return TAGS

async def handle_tags(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip":
        ctx.user_data["post_data"]["tags"] = txt.split()
    await update.message.reply_text("🔤 Enter ROM Title:")
    return TITLE

async def handle_title(update: Update, ctx: CallbackContext) -> int:
    ctx.user_data["post_data"]["title"] = update.message.text.strip()
    await update.message.reply_text("📱 Enter Device Name:")
    return DEVICE

async def handle_device(update: Update, ctx: CallbackContext) -> int:
    ctx.user_data["post_data"]["device"] = update.message.text.strip()
    await update.message.reply_text("👤 Enter Maintainer (@username):")
    return MAINTAINER

async def handle_maintainer(update: Update, ctx: CallbackContext) -> int:
    ctx.user_data["post_data"]["maintainer"] = update.message.text.strip()
    await update.message.reply_text(
        "📸 Send screenshot (photo) or URL; URL advances to changelog."
    )
    return SCREENSHOTS

async def handle_screenshots(update: Update, ctx: CallbackContext) -> int:
    d = ctx.user_data["post_data"]
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        link = await PostDataManager.post_photo_discussion(ctx, "Screenshots", fid)
        d["screenshots"].append(link)
        await update.message.reply_text("✅ Screenshot saved! More or /done")
        return SCREENSHOTS
    txt = update.message.text.strip()
    if txt.lower() == "/done":
        if not d["screenshots"]:
            await update.message.reply_text("⚠️ Add at least one screenshot.")
            return SCREENSHOTS
        await update.message.reply_text("📝 Enter changelog text, URL, or /skip:")
        return CHANGELOG
    if URL_REGEX.match(txt):
        d["screenshots"].append(txt)
        await update.message.reply_text("✅ URL saved! Now changelog or /skip")
        return CHANGELOG
    await update.message.reply_text("❌ Send photo, valid URL, or /done.")
    return SCREENSHOTS

async def handle_changelog(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip":
        if URL_REGEX.match(txt):
            ctx.user_data["post_data"]["changelog"] = txt
        else:
            link = await PostDataManager.post_text_discussion(ctx, "Changelog", txt)
            ctx.user_data["post_data"]["changelog"] = link
    await update.message.reply_text("📝 Enter device changelog text, URL, or /skip:")
    return DEVICE_CHANGELOG

async def handle_device_changelog(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip":
        if URL_REGEX.match(txt):
            ctx.user_data["post_data"]["device_changelog"] = txt
        else:
            link = await PostDataManager.post_text_discussion(ctx, "Device Changelog", txt)
            ctx.user_data["post_data"]["device_changelog"] = link
    await update.message.reply_text("🔗 Enter download links (Variant|URL). /done when finished:")
    return DOWNLOAD_LINKS

async def handle_download_links(update: Update, ctx: CallbackContext) -> int:
    d = ctx.user_data["post_data"]
    txt = update.message.text.strip()
    if txt.lower() == "/done":
        if not d["download_links"]:
            await update.message.reply_text("⚠️ At least one download link required.")
            return DOWNLOAD_LINKS
        await update.message.reply_text("💰 Enter donation link or /skip:")
        return DONATE_LINK
    if "|" in txt:
        var, url = map(str.strip, txt.split("|", 1))
        if URL_REGEX.match(url):
            d["download_links"][var] = url
            await update.message.reply_text(f"✅ {var} saved. More or /done?")
            return DOWNLOAD_LINKS
    await update.message.reply_text("❌ Format Variant|URL or /done.")
    return DOWNLOAD_LINKS

async def handle_donate(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip" and URL_REGEX.match(txt):
        ctx.user_data["post_data"]["donate"] = txt
    await update.message.reply_text("📖 Enter readme text, URL, or /skip:")
    return README

async def handle_readme(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip":
        if URL_REGEX.match(txt):
            ctx.user_data["post_data"]["readme"] = txt
        else:
            link = await PostDataManager.post_text_discussion(ctx, "Readme", txt)
            ctx.user_data["post_data"]["readme"] = link
    await update.message.reply_text("📝 Any notes? Send lines or /skip:")
    return NOTES

async def handle_notes(update: Update, ctx: CallbackContext) -> int:
    txt = update.message.text.strip()
    if txt.lower() != "/skip":
        ctx.user_data["post_data"]["notes"] = txt
    caption = PostDataManager.build_caption(ctx)
    await update.message.reply_text(
        caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish", callback_data="publish"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]]),
        disable_web_page_preview=True,
    )
    return CONFIRM

async def publish_post(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    d = ctx.user_data["post_data"]
    caption = PostDataManager.build_caption(ctx)
    send_args = dict(chat_id=CHANNEL_ID, caption=caption, parse_mode="HTML")
    if d["banner"]:
        await ctx.bot.send_photo(photo=d["banner"], **send_args)
    else:
        await ctx.bot.send_message(text=caption, **send_args)
    await query.edit_message_text("✅ Successfully published!")
    return ConversationHandler.END

async def cancel_post(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Operation cancelled.")
    return ConversationHandler.END

async def help_command(update: Update, ctx: CallbackContext) -> None:
    msg = (
        "📚 <b>Bot Help</b>\n\n"
        "/start – begin new ROM post\n"
        "/cancel – cancel at any time\n\n"
        "Flow:\n"
        "1. Banner → 2. Tags → 3. Title → 4. Device → 5. Maintainer\n"
        "6. Screenshots → 7. Changelog → 8. Device Changelog\n"
        "9. Download Links → 10. Donate → 11. Readme → 12. Notes\n"
        "Then Preview → Publish"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

# ─── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).job_queue(JobQueue()).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BANNER:       [MessageHandler(filters.PHOTO, handle_banner), CommandHandler("skip", handle_banner)],
            TAGS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tags)],
            TITLE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title)],
            DEVICE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device)],
            MAINTAINER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_maintainer)],
            SCREENSHOTS:  [
                MessageHandler(filters.PHOTO, handle_screenshots),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_screenshots),
                CommandHandler("done", handle_screenshots),
            ],
            CHANGELOG:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_changelog), CommandHandler("skip", handle_changelog)],
            DEVICE_CHANGELOG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_device_changelog), CommandHandler("skip", handle_device_changelog)],
            DOWNLOAD_LINKS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_download_links), CommandHandler("done", handle_download_links)],
            DONATE_LINK:      [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_donate), CommandHandler("skip", handle_donate)],
            README:           [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_readme), CommandHandler("skip", handle_readme)],
            NOTES:            [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_notes), CommandHandler("skip", handle_notes)],
            CONFIRM:          [
                CallbackQueryHandler(publish_post, pattern="^publish$"),
                CallbackQueryHandler(cancel_post, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_post)],
        per_user=True,
        per_chat=True,
        conversation_timeout=1800,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_post))
    app.run_polling()

if __name__ == "__main__":
    main()
# ─── Entry Point ────────────────────────────────────────────────────────────────
import os
import threading
from aiohttp import web

async def _handle_root(request):
    return web.Response(text="OK")

def _start_http_server():
    app = web.Application()
    app.router.add_get("/", _handle_root)
    port = int(os.environ.get("PORT", 8000))
    web.run_app(app, port=port)

if __name__ == "__main__":
    # Launch your Telegram bot in a background thread
    threading.Thread(target=main, daemon=True).start()
    # Then start the HTTP server on $PORT for Render health checks
    _start_http_server()
