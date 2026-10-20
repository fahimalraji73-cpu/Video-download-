import os
import re
import uuid
import logging
import threading

import telebot
import yt_dlp
from flask import Flask

# --------------------------------------------------------------------------
# Configuration (set these in Render -> Environment)
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

# Channel jekhane video upload hobe (username ba -100... numeric ID)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@viral_koci_video")

# Shudhu ei user bot use korte parbe (apnar Telegram user ID)
OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))

# Channel post-er caption (khali rakhle caption thakbe na)
CAPTION = os.environ.get("CAPTION", "")

PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit: 50 MB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("video-bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

URL_PATTERN = re.compile(
    r"(https?://(?:www\.|m\.|web\.|mbasic\.)?"
    r"(?:instagram\.com|facebook\.com|fb\.watch|fb\.com)/[^\s]+)",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Flask keep-alive server (Render)
# --------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    thread = threading.Thread(target=run_flask, daemon=True)
    thread.start()
    log.info("Keep-alive Flask server started on port %s", PORT)


# --------------------------------------------------------------------------
# Download logic
# --------------------------------------------------------------------------
def download_video(url: str) -> str:
    """Download the video with yt-dlp and return the local file path."""
    file_id = uuid.uuid4().hex
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }

    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

    if not os.path.exists(file_path):
        for name in os.listdir(DOWNLOAD_DIR):
            if name.startswith(file_id):
                file_path = os.path.join(DOWNLOAD_DIR, name)
                break

    return file_path


def safe_remove(path):
    """Delete a file from the server, ignoring errors."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            log.info("Deleted %s", path)
    except OSError as exc:
        log.error("Could not delete %s: %s", path, exc)


def is_owner(message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


# --------------------------------------------------------------------------
# Bot handlers
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return
    bot.reply_to(
        message,
        "👋 <b>Welcome!</b>\n\n"
        "Instagram Reel ba Facebook video-r link pathan. "
        f"Ami video download kore {CHANNEL_ID} channel e upload kore dibo.",
    )


@bot.message_handler(func=lambda m: m.text is not None)
def handle_link(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return

    match = URL_PATTERN.search(message.text)
    if not match:
        bot.reply_to(
            message,
            "❌ Valid Instagram Reel ba Facebook video link pathan.",
        )
        return

    url = match.group(1)
    status = bot.reply_to(message, "⏳ Download hocche, wait koren...")
    file_path = None

    try:
        file_path = download_video(url)

        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            bot.edit_message_text(
                "⚠️ Video 50 MB-er boro, Telegram bot eta pathate pare na.",
                chat_id=status.chat.id,
                message_id=status.message_id,
            )
            return

        bot.edit_message_text(
            f"📤 {CHANNEL_ID} channel e upload hocche...",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

        with open(file_path, "rb") as video:
            bot.send_video(
                CHANNEL_ID,
                video,
                caption=CAPTION or None,
                supports_streaming=True,
                timeout=120,
            )

        bot.edit_message_text(
            f"✅ Channel e upload hoye geche: {CHANNEL_ID}",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

    except yt_dlp.utils.DownloadError as exc:
        log.warning("Download error for %s: %s", url, exc)
        bot.edit_message_text(
            "❌ Video download kora gelo na. Private, delete kora, "
            "ba unsupported hote pare.",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except telebot.apihelper.ApiTelegramException as exc:
        log.error("Telegram error: %s", exc)
        bot.edit_message_text(
            "❌ Channel e upload hoy nai. Bot ta channel er Admin ache kina "
            "ar CHANNEL_ID thik ache kina check koren.",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error: %s", exc)
        bot.edit_message_text(
            "❌ Kichu ekta vul hoyeche. Pore abar try koren.",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    finally:
        # CLEANUP: upload shesh hole (ba fail hole) server theke file delete
        safe_remove(file_path)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    keep_alive()
    log.info("Bot is starting... Channel: %s | Owner: %s", CHANNEL_ID, OWNER_ID)
    # skip_pending intentionally NOT used: it makes one extra getUpdates call
    # outside the retry loop, which crashes the deploy on a 409 conflict.
    # infinity_polling retries automatically until the old instance is gone.
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
