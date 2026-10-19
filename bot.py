import os
import re
import uuid
import logging
import threading

import telebot
import yt_dlp
from flask import Flask

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

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
    # Flask's built-in server is fine for a simple health-check endpoint.
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
        # Prefer a single mp4 file under 50 MB so no ffmpeg merge is needed.
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }

    # Optional: cookies help with Instagram/Facebook rate limits or login walls.
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

    if not os.path.exists(file_path):
        # Fallback: locate the file by its unique id prefix.
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


# --------------------------------------------------------------------------
# Bot handlers
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 <b>Welcome!</b>\n\n"
        "Send me an <b>Instagram Reel</b> or <b>Facebook video</b> link "
        "and I'll download it for you.",
    )


@bot.message_handler(func=lambda m: m.text is not None)
def handle_link(message):
    match = URL_PATTERN.search(message.text)
    if not match:
        bot.reply_to(
            message,
            "❌ Please send a valid Instagram Reel or Facebook video link.",
        )
        return

    url = match.group(1)
    status = bot.reply_to(message, "⏳ Downloading, please wait...")
    file_path = None

    try:
        file_path = download_video(url)

        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            bot.edit_message_text(
                "⚠️ The video is larger than Telegram's 50 MB bot limit.",
                chat_id=status.chat.id,
                message_id=status.message_id,
            )
            return

        bot.edit_message_text(
            "📤 Uploading...",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

        with open(file_path, "rb") as video:
            bot.send_video(
                message.chat.id,
                video,
                caption="✅ Downloaded successfully!",
                reply_to_message_id=message.message_id,
                supports_streaming=True,
                timeout=120,
            )

        bot.delete_message(status.chat.id, status.message_id)

    except yt_dlp.utils.DownloadError as exc:
        log.warning("Download error for %s: %s", url, exc)
        bot.edit_message_text(
            "❌ Could not download this video. It may be private, "
            "removed, or unsupported.",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error: %s", exc)
        bot.edit_message_text(
            "❌ Something went wrong. Please try again later.",
            chat_id=status.chat.id,
            message_id=status.message_id,
        )
    finally:
        # CLEANUP: always delete the file right after sending (or on failure).
        safe_remove(file_path)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    keep_alive()
    log.info("Bot is starting...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
