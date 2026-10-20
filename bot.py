import os
import re
import json
import time
import uuid
import logging
import threading
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import telebot
import yt_dlp
from flask import Flask

# --------------------------------------------------------------------------
# Configuration (Render -> Environment e change kora jay)
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@viral_koci_video")
OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))
CAPTION = os.environ.get("CAPTION", "")

# Bangladesh time
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))

# Daily upload slots: 8, 9, 10, 11 AM, 12 PM, 5, 6, 7 PM
SLOT_TIMES = os.environ.get(
    "SLOT_TIMES", "08:00,09:00,10:00,11:00,12:00,17:00,18:00,19:00"
)
SLOTS = [
    dtime(int(s.split(":")[0]), int(s.split(":")[1]))
    for s in SLOT_TIMES.split(",")
]

PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "downloads"
QUEUE_FILE = "queue.json"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit: 50 MB
MAX_ATTEMPTS = 3
RETRY_DELAY_MIN = 5

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
# Queue storage (queue.json)
# --------------------------------------------------------------------------
queue_lock = threading.Lock()


def now_local() -> datetime:
    return datetime.now(TZ)


def load_queue() -> list:
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read queue: %s", exc)
        return []


def save_queue(items: list) -> None:
    items.sort(key=lambda i: i["time"])
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def next_free_slot(items: list) -> datetime:
    """Return the first upcoming slot that is not already taken."""
    taken = {parse_time(i["time"]) for i in items}
    now = now_local()
    for day_offset in range(0, 365):
        day = now.date() + timedelta(days=day_offset)
        for slot in SLOTS:
            candidate = datetime.combine(day, slot, tzinfo=TZ)
            if candidate > now and candidate not in taken:
                return candidate
    raise RuntimeError("No free slot found.")


def pretty_time(dt: datetime) -> str:
    today = now_local().date()
    if dt.date() == today:
        day = "Aj"
    elif dt.date() == today + timedelta(days=1):
        day = "Agamikal"
    else:
        day = dt.strftime("%d %b")
    return f"{day}, {dt.strftime('%I:%M %p')}"


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


def notify_owner(text: str) -> None:
    try:
        bot.send_message(OWNER_ID, text)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not notify owner: %s", exc)


# --------------------------------------------------------------------------
# Scheduler: slot-er somoy hole download + channel e upload
# --------------------------------------------------------------------------
def post_item(item: dict) -> None:
    # Video age-i download hoye Telegram e save ache (file_id), tai
    # schedule-er somoy notun kore download korar dorkar nei.
    if item.get("file_id"):
        bot.send_video(
            CHANNEL_ID,
            item["file_id"],
            caption=CAPTION or None,
            supports_streaming=True,
            timeout=120,
        )
        return

    # Fallback: file_id na thakle ekhon download kore upload
    file_path = None
    try:
        file_path = download_video(item["url"])

        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            raise ValueError("Video 50 MB-er boro.")

        with open(file_path, "rb") as video:
            bot.send_video(
                CHANNEL_ID,
                video,
                caption=CAPTION or None,
                supports_streaming=True,
                timeout=120,
            )
    finally:
        # CLEANUP: upload shesh hole (ba fail hole) server theke file delete
        safe_remove(file_path)


def process_due() -> None:
    now = now_local()
    with queue_lock:
        due = [
            dict(i)
            for i in load_queue()
            if parse_time(i["time"]) <= now
            and ("next_try" not in i or parse_time(i["next_try"]) <= now)
        ]

    for item in due:
        log.info("Posting scheduled item %s", item["url"])
        try:
            post_item(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("Scheduled post failed: %s", exc)
            with queue_lock:
                items = load_queue()
                for i in items:
                    if i["id"] == item["id"]:
                        i["attempts"] = i.get("attempts", 0) + 1
                        if i["attempts"] >= MAX_ATTEMPTS:
                            items.remove(i)
                            notify_owner(
                                "❌ Ei video upload kora gelo na "
                                f"({MAX_ATTEMPTS} bar try kora hoyeche), "
                                f"queue theke bad dewa holo:\n{item['url']}"
                            )
                        else:
                            i["next_try"] = (
                                now_local() + timedelta(minutes=RETRY_DELAY_MIN)
                            ).isoformat()
                        break
                save_queue(items)
            continue

        with queue_lock:
            items = [i for i in load_queue() if i["id"] != item["id"]]
            save_queue(items)
            remaining = len(items)
        notify_owner(
            f"✅ Channel e upload hoyeche. Queue te baki ache: {remaining} ta."
        )


def scheduler_loop():
    log.info("Scheduler started. Slots: %s", SLOT_TIMES)
    while True:
        try:
            process_due()
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler error: %s", exc)
        time.sleep(20)


# --------------------------------------------------------------------------
# Bot handlers
# --------------------------------------------------------------------------
def is_owner(message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return
    bot.reply_to(
        message,
        "👋 <b>Welcome!</b>\n\n"
        "Instagram Reel ba Facebook video-r link pathan. Ami link ta "
        f"schedule kore rakhbo, ar thik somoy e {CHANNEL_ID} channel e "
        "upload korbo.\n\n"
        "Daily slot: 8, 9, 10, 11 AM, 12 PM, 5, 6, 7 PM\n\n"
        "/queue - schedule dekhen\n"
        "/cancel 2 - 2 number video bad den\n"
        "/clear - shob bad den",
    )


@bot.message_handler(commands=["queue"])
def show_queue(message):
    if not is_owner(message):
        return
    with queue_lock:
        items = load_queue()
    if not items:
        bot.reply_to(message, "📭 Queue khali.")
        return
    lines = ["📅 <b>Schedule:</b>\n"]
    for n, item in enumerate(items, start=1):
        lines.append(f"{n}. {pretty_time(parse_time(item['time']))}\n{item['url']}")
    bot.reply_to(message, "\n".join(lines), disable_web_page_preview=True)


@bot.message_handler(commands=["cancel"])
def cancel_item(message):
    if not is_owner(message):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Likhen: /cancel 2")
        return
    index = int(parts[1])
    with queue_lock:
        items = load_queue()
        if index < 1 or index > len(items):
            bot.reply_to(message, "❌ Ei number e kichu nai. /queue dekhen.")
            return
        removed = items.pop(index - 1)
        save_queue(items)
    bot.reply_to(message, f"🗑 Bad dewa holo:\n{removed['url']}",
                 disable_web_page_preview=True)


@bot.message_handler(commands=["clear"])
def clear_queue(message):
    if not is_owner(message):
        return
    with queue_lock:
        count = len(load_queue())
        save_queue([])
    bot.reply_to(message, f"🗑 {count} ta video queue theke bad dewa holo.")


@bot.message_handler(func=lambda m: m.text is not None)
def handle_link(message):
    if not is_owner(message):
        bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")
        return

    urls = URL_PATTERN.findall(message.text)
    if not urls:
        bot.reply_to(
            message,
            "❌ Valid Instagram Reel ba Facebook video link pathan.",
        )
        return

    for url in urls:
        with queue_lock:
            already = any(i["url"] == url for i in load_queue())
        if already:
            bot.reply_to(
                message,
                f"⚠️ Agei queue te ache:\n{url}",
                disable_web_page_preview=True,
            )
            continue

        status = bot.reply_to(message, "⏳ Video download hocche...")
        file_path = None
        try:
            # 1) Sathe sathe download
            file_path = download_video(url)

            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                bot.edit_message_text(
                    "⚠️ Video 50 MB-er boro, Telegram bot eta pathate pare na.",
                    chat_id=status.chat.id,
                    message_id=status.message_id,
                )
                continue

            # 2) Apnar private chat e save kora (Telegram-er server e thake),
            #    tahole Render-er disk e file rakhar dorkar hoy na
            with open(file_path, "rb") as video:
                sent = bot.send_video(
                    OWNER_ID,
                    video,
                    caption="💾 Save kora hoyeche. Schedule-er somoy channel e jabe.",
                    supports_streaming=True,
                    timeout=120,
                )
            media = sent.video or sent.document
            file_id = media.file_id

            # 3) Porer free slot e schedule
            with queue_lock:
                items = load_queue()
                slot = next_free_slot(items)
                items.append(
                    {
                        "id": uuid.uuid4().hex,
                        "url": url,
                        "file_id": file_id,
                        "time": slot.isoformat(),
                        "attempts": 0,
                    }
                )
                save_queue(items)

            bot.edit_message_text(
                f"✅ Download hoye geche. Channel e jabe: "
                f"<b>{pretty_time(slot)}</b>",
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
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error: %s", exc)
            bot.edit_message_text(
                "❌ Kichu ekta vul hoyeche. Pore abar try koren.",
                chat_id=status.chat.id,
                message_id=status.message_id,
            )
        finally:
            # CLEANUP: server theke file sathe sathe delete
            safe_remove(file_path)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    keep_alive()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("Bot is starting... Channel: %s | Owner: %s", CHANNEL_ID, OWNER_ID)
    # skip_pending intentionally NOT used: infinity_polling retries on its own
    # if an old instance still holds the getUpdates connection (409).
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
