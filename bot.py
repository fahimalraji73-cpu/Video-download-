import os
import re
import json
import time
import uuid
import hmac
import html
import logging
import threading
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import telebot
import yt_dlp
from flask import Flask, request

# --------------------------------------------------------------------------
# Configuration (Render -> Environment e change kora jay)
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@viral_koci_video")
OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))

# Onno app/automation theke link pathanor gopon key (Render -> Environment)
API_KEY = os.environ.get("API_KEY", "")

# Private "inbox" channel: ei channel e link post hole bot auto download kore
INBOX_CHANNEL_ID = os.environ.get("INBOX_CHANNEL_ID", "").strip()

# Shuru-r caption (bot theke bodlano jay). Ekadhik dite "||" diye alada koren:
#   CAPTION = caption1||caption2||caption3
DEFAULT_CAPTION = os.environ.get("CAPTION", "")

# Shuru-r slot (bot theke /setslots diye bodlano jay)
DEFAULT_SLOTS = os.environ.get(
    "SLOT_TIMES", "08:00,09:00,10:00,11:00,12:00,17:00,18:00,19:00"
)

# Bangladesh time
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))

PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "downloads"
QUEUE_FILE = "queue.json"
SETTINGS_FILE = "settings.json"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit: 50 MB
MAX_ATTEMPTS = 3
RETRY_DELAY_MIN = 5
CAPTION_KINDS = {"photo", "video", "animation", "document", "audio", "voice"}
MAX_CAPTIONS = 30

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


@app.route("/add", methods=["GET", "POST"])
def api_add():
    """Onno app theke link pathanor jonno: /add?key=...&text=LINK"""
    key = request.values.get("key", "")
    if not API_KEY or not hmac.compare_digest(key, API_KEY):
        return {"ok": False, "error": "unauthorized"}, 401

    text = request.values.get("text") or request.values.get("url") or ""
    if not text and request.is_json:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or data.get("url") or "")

    urls = URL_PATTERN.findall(text)
    if not urls:
        return {"ok": False, "error": "no valid instagram/facebook link"}, 400

    count = start_bulk(OWNER_ID, urls)
    return {"ok": True, "links": count}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    thread = threading.Thread(target=run_flask, daemon=True)
    thread.start()
    log.info("Keep-alive Flask server started on port %s", PORT)


# --------------------------------------------------------------------------
# Settings (slots + caption), settings.json e save hoy
# --------------------------------------------------------------------------
settings_lock = threading.Lock()
settings = {}


def parse_slot_list(text: str) -> list:
    """'8:00, 09:00 17:30' -> ['08:00', '09:00', '17:30']"""
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    slots = set()
    for part in parts:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m:
            raise ValueError(part)
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(part)
        slots.add(f"{hour:02d}:{minute:02d}")
    if not slots:
        raise ValueError("empty")
    return sorted(slots)


def load_settings() -> None:
    global settings
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not read settings: %s", exc)
    try:
        slots = parse_slot_list(",".join(data.get("slots", []))) \
            if data.get("slots") else parse_slot_list(DEFAULT_SLOTS)
    except ValueError:
        slots = parse_slot_list(DEFAULT_SLOTS)
    captions = data.get("captions")
    if captions is None:
        legacy = data.get("caption")
        if legacy:
            captions = [legacy]
        else:
            captions = [c.strip() for c in DEFAULT_CAPTION.split("||")]
    captions = [c for c in captions if c]
    try:
        index = int(data.get("caption_index", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    settings = {
        "slots": slots,
        "captions": captions,
        "caption_index": index,
    }


def save_settings() -> None:
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def get_slots() -> list:
    with settings_lock:
        return [
            dtime(int(s.split(":")[0]), int(s.split(":")[1]))
            for s in settings["slots"]
        ]


def get_captions() -> list:
    with settings_lock:
        return list(settings["captions"])


def next_caption_info():
    """Porer video-te kon caption jabe: (index, text), na thakle None."""
    with settings_lock:
        caps = settings["captions"]
        if not caps:
            return None
        idx = settings["caption_index"] % len(caps)
        return idx, caps[idx]


def get_caption():
    """Porer video-r caption (dekhe, index bodlay na)."""
    info = next_caption_info()
    return info[1] if info else None


def advance_caption() -> None:
    """Caption bebohar hoye gele porer caption-e jaoa."""
    with settings_lock:
        count = len(settings["captions"])
        if count:
            settings["caption_index"] = (settings["caption_index"] + 1) % count
            save_settings()


def format_captions() -> str:
    caps = get_captions()
    info = next_caption_info()
    lines = []
    for n, text in enumerate(caps, start=1):
        marker = "➡️ " if info and info[0] == n - 1 else ""
        preview = text.replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:80] + "…"
        lines.append(f"{marker}{n}. {html.escape(preview)}")
    return "\n".join(lines)


def format_slots() -> str:
    with settings_lock:
        return ", ".join(
            datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0")
            for s in settings["slots"]
        )


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
    slots = get_slots()
    for day_offset in range(0, 365):
        day = now.date() + timedelta(days=day_offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=TZ)
            if candidate > now and candidate not in taken:
                return candidate
    raise RuntimeError("No free slot found.")


def pretty_time(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    today = now_local().date()
    if dt.date() == today:
        day = "Aj"
    elif dt.date() == today + timedelta(days=1):
        day = "Agamikal"
    else:
        day = dt.strftime("%d %b")
    return f"{day}, {dt.strftime('%I:%M %p')}"


def item_label(item: dict) -> str:
    return item.get("label") or item.get("url") or "post"


def enqueue(entry: dict) -> datetime:
    """Next free slot e entry queue te rakhe, slot return kore."""
    with queue_lock:
        items = load_queue()
        slot = next_free_slot(items)
        entry.update(
            {"id": uuid.uuid4().hex, "time": slot.isoformat(), "attempts": 0}
        )
        items.append(entry)
        save_queue(items)
    return slot


def reschedule_all() -> None:
    """Notun slot onujayi queue-r shob item abar shajano (order thik thake)."""
    with queue_lock:
        old = sorted(load_queue(), key=lambda i: i["time"])
        new_items = []
        for item in old:
            item.pop("next_try", None)
            item["time"] = next_free_slot(new_items).isoformat()
            new_items.append(item)
        save_queue(new_items)


def reschedule_affected(removed: list) -> int:
    """Remove kora slot-er post gulo porer khali slot e soriye dey."""
    with queue_lock:
        items = load_queue()
        keep, moved = [], []
        for item in items:
            hhmm = parse_time(item["time"]).astimezone(TZ).strftime("%H:%M")
            (moved if hhmm in removed else keep).append(item)
        moved.sort(key=lambda i: i["time"])
        for item in moved:
            item.pop("next_try", None)
            item["time"] = next_free_slot(keep).isoformat()
            keep.append(item)
        save_queue(keep)
    return len(moved)


# --------------------------------------------------------------------------
# Download logic (Instagram / Facebook link)
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
# Channel e post kora
# --------------------------------------------------------------------------
def send_item(item: dict) -> None:
    kind = item.get("kind", "video")

    # Text, poll ityadi: original message copy kore pathano
    if kind == "copy":
        bot.copy_message(CHANNEL_ID, item["src_chat"], item["src_msg"])
        return

    file_id = item["file_id"]
    kw = {}
    used_bot_caption = False
    if kind in CAPTION_KINDS:
        # Video-r nijer caption jabe na, shudhu apnar set kora caption jabe.
        original = None if kind == "video" else item.get("orig_caption")
        if original:
            # Onno post-er (photo ityadi) nijer caption thakle sheta-i jabe
            kw["caption"] = original
            kw["parse_mode"] = "HTML"
        else:
            caption = get_caption()
            if caption:
                kw["caption"] = caption
                kw["parse_mode"] = ""  # caption plain text hishebe jabe
                used_bot_caption = True

    if kind == "photo":
        bot.send_photo(CHANNEL_ID, file_id, **kw)
    elif kind == "video":
        bot.send_video(
            CHANNEL_ID, file_id, supports_streaming=True, timeout=120, **kw
        )
    elif kind == "animation":
        bot.send_animation(CHANNEL_ID, file_id, timeout=120, **kw)
    elif kind == "document":
        bot.send_document(CHANNEL_ID, file_id, timeout=120, **kw)
    elif kind == "audio":
        bot.send_audio(CHANNEL_ID, file_id, timeout=120, **kw)
    elif kind == "voice":
        bot.send_voice(CHANNEL_ID, file_id, timeout=120, **kw)
    elif kind == "video_note":
        bot.send_video_note(CHANNEL_ID, file_id, timeout=120)
    elif kind == "sticker":
        bot.send_sticker(CHANNEL_ID, file_id)
    else:
        raise ValueError(f"Unknown kind: {kind}")

    # Post shofol hole porer caption-e jan (serial)
    if used_bot_caption:
        advance_caption()


def post_item(item: dict) -> None:
    if item.get("file_id") or item.get("kind") == "copy":
        send_item(item)
        return

    # Fallback: purono item (file_id nai), ekhon download kore upload
    file_path = None
    try:
        file_path = download_video(item["url"])
        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            raise ValueError("Video 50 MB-er boro.")
        caption = get_caption()
        with open(file_path, "rb") as video:
            kw = {"caption": caption, "parse_mode": ""} if caption else {}
            bot.send_video(
                CHANNEL_ID, video, supports_streaming=True, timeout=120, **kw
            )
        if caption:
            advance_caption()
    finally:
        safe_remove(file_path)


# --------------------------------------------------------------------------
# Scheduler: slot-er somoy hole channel e upload
# --------------------------------------------------------------------------
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
        log.info("Posting scheduled item %s", item_label(item))
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
                                "❌ Ei post upload kora gelo na "
                                f"({MAX_ATTEMPTS} bar try kora hoyeche), "
                                "queue theke bad dewa holo:\n"
                                f"{html.escape(item_label(item))}"
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
    log.info("Scheduler started.")
    while True:
        try:
            process_due()
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler error: %s", exc)
        time.sleep(20)


# --------------------------------------------------------------------------
# Helpers for handlers
# --------------------------------------------------------------------------
def is_owner(message) -> bool:
    return message.from_user is not None and message.from_user.id == OWNER_ID


def deny(message) -> None:
    bot.reply_to(message, "⛔ Ei bot shudhu owner-er jonno.")


def reply_long(message, lines: list) -> None:
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            bot.send_message(
                message.chat.id, chunk, disable_web_page_preview=True
            )
            chunk = ""
        chunk += line + "\n"
    if chunk:
        bot.send_message(message.chat.id, chunk, disable_web_page_preview=True)


def extract_media(message):
    """Media message hole (kind, file_id, unique_id, label), noyto None."""
    if message.photo:
        m = message.photo[-1]
        return "photo", m.file_id, m.file_unique_id, "🖼 Photo"
    if message.video:
        m = message.video
        return "video", m.file_id, m.file_unique_id, "🎬 Video"
    if message.animation:
        m = message.animation
        return "animation", m.file_id, m.file_unique_id, "🎞 GIF"
    if message.document:
        m = message.document
        return "document", m.file_id, m.file_unique_id, "📄 File"
    if message.audio:
        m = message.audio
        return "audio", m.file_id, m.file_unique_id, "🎵 Audio"
    if message.voice:
        m = message.voice
        return "voice", m.file_id, m.file_unique_id, "🎤 Voice"
    if message.video_note:
        m = message.video_note
        return "video_note", m.file_id, m.file_unique_id, "⭕ Video note"
    if message.sticker:
        m = message.sticker
        return "sticker", m.file_id, m.file_unique_id, "🏷 Sticker"
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
@bot.message_handler(commands=["help"])
def send_welcome(message):
    if not is_owner(message):
        return deny(message)
    bot.reply_to(
        message,
        "👋 <b>Welcome!</b>\n\n"
        f"Je kono post forward korun, ba Instagram/Facebook link pathan. "
        f"Ami porer khali slot e {html.escape(str(CHANNEL_ID))} channel e "
        "upload kore dibo.\n\n"
        "/menu - shob kichu button diye korun\n\n"
        "<b>Queue</b>\n"
        "/queue - schedule dekhen\n"
        "/cancel 2 - 2 number post bad den\n"
        "/clear - shob bad den\n\n"
        "<b>Schedule</b>\n"
        "/slots - daily slot dekhen\n"
        "/setslots 08:00,12:00,18:00 - shob slot ekbare set koren\n"
        "/delslot 10:00 - ekta slot bad den\n"
        "/addslot 10:00 - ekta slot abar jog koren\n"
        "/settime 2 17:30 - 2 number post-er somoy bodlan\n\n"
        "<b>Caption</b>\n"
        "/caption Amar caption - caption jog koren (ekadhik dile serial "
        "onujayi jabe)\n"
        "/caption - caption list dekhen\n"
        "/delcaption 2 - 2 number caption bad den\n"
        "/nocaption - shob caption bad den",
    )


@bot.message_handler(commands=["queue"])
def show_queue(message):
    if not is_owner(message):
        return deny(message)
    with queue_lock:
        items = load_queue()
    if not items:
        bot.reply_to(message, "📭 Queue khali.")
        return
    lines = ["📅 <b>Schedule:</b>\n"]
    for n, item in enumerate(items, start=1):
        lines.append(
            f"{n}. {pretty_time(parse_time(item['time']))}\n"
            f"{html.escape(item_label(item))}"
        )
    reply_long(message, lines)


@bot.message_handler(commands=["cancel"])
def cancel_item(message):
    if not is_owner(message):
        return deny(message)
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
    bot.reply_to(
        message,
        f"🗑 Bad dewa holo:\n{html.escape(item_label(removed))}",
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["clear"])
def clear_queue(message):
    if not is_owner(message):
        return deny(message)
    with queue_lock:
        count = len(load_queue())
        save_queue([])
    bot.reply_to(message, f"🗑 {count} ta post queue theke bad dewa holo.")


@bot.message_handler(commands=["slots"])
def show_slots(message):
    if not is_owner(message):
        return deny(message)
    bot.reply_to(
        message,
        f"🕒 <b>Daily slot:</b>\n{format_slots()}\n\n"
        "Ekta bad dite: /delslot 10:00\n"
        "Ekta jog korte: /addslot 10:00\n"
        "Shob ekbare bodlate: /setslots 08:00,12:00,18:00\n"
        "(24 ghonta format)",
    )


@bot.message_handler(commands=["setslots"])
def set_slots(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Likhen: /setslots 08:00,12:00,18:00\n"
            "(24 ghonta format, jemon 17:00 = 5 PM)",
        )
        return
    try:
        slots = parse_slot_list(parts[1])
    except ValueError:
        bot.reply_to(
            message,
            "❌ Somoy-er format thik na. Ei vabe likhen:\n"
            "/setslots 08:00,12:00,18:00",
        )
        return

    with settings_lock:
        settings["slots"] = slots
        save_settings()
    reschedule_all()
    bot.reply_to(
        message,
        f"✅ Notun slot: {format_slots()}\n"
        "Queue-r shob post notun slot onujayi abar shajano hoyeche. "
        "/queue dekhen.",
    )


@bot.message_handler(commands=["addslot"])
def add_slot(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Likhen: /addslot 10:00")
        return
    try:
        new = parse_slot_list(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Somoy-er format thik na. Jemon: /addslot 10:00")
        return

    with settings_lock:
        added = [s for s in new if s not in settings["slots"]]
        if added:
            settings["slots"] = sorted(set(settings["slots"]) | set(new))
            save_settings()

    if not added:
        bot.reply_to(message, f"⚠️ Ei slot agei ache.\n🕒 Slot: {format_slots()}")
        return
    bot.reply_to(
        message,
        f"✅ Slot add hoyeche.\n🕒 Ekhon slot: {format_slots()}\n"
        "Ei slot ekhon theke notun post-er jonno use hobe.",
    )


@bot.message_handler(commands=["delslot", "removeslot"])
def delete_slot(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Likhen: /delslot 10:00")
        return
    try:
        remove = parse_slot_list(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Somoy-er format thik na. Jemon: /delslot 10:00")
        return

    error = None
    with settings_lock:
        missing = [s for s in remove if s not in settings["slots"]]
        remaining = [s for s in settings["slots"] if s not in remove]
        if missing:
            error = "❌ Ei somoy slot e nai: " + ", ".join(missing)
        elif not remaining:
            error = "❌ Shob slot bad deya jabe na, komporkhe ekta thakte hobe."
        else:
            settings["slots"] = remaining
            save_settings()
    if error:
        bot.reply_to(message, error + f"\n🕒 Ekhon slot: {format_slots()}")
        return

    moved = reschedule_affected(remove)
    extra = (
        f"\n{moved} ta post oi somoy e chilo, sheguli porer khali slot e "
        "soriye dewa hoyeche."
        if moved
        else ""
    )
    bot.reply_to(
        message,
        f"✅ Slot bad dewa holo. Oi somoy e ar kono post jabe na.\n"
        f"🕒 Ekhon slot: {format_slots()}{extra}",
    )


@bot.message_handler(commands=["settime"])
def set_time(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", parts[2]) if len(parts) >= 3 else None
    if len(parts) < 3 or not parts[1].isdigit() or not m:
        bot.reply_to(message, "Likhen: /settime 2 17:30")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        bot.reply_to(message, "❌ Somoy thik na. Jemon: /settime 2 17:30")
        return

    index = int(parts[1])
    with queue_lock:
        items = load_queue()
        if index < 1 or index > len(items):
            bot.reply_to(message, "❌ Ei number e kichu nai. /queue dekhen.")
            return
        now = now_local()
        target = datetime.combine(now.date(), dtime(hour, minute), tzinfo=TZ)
        if target <= now:
            target += timedelta(days=1)
        item = items[index - 1]
        item["time"] = target.isoformat()
        item.pop("next_try", None)
        save_queue(items)
    bot.reply_to(
        message,
        f"✅ {index} number post jabe: <b>{pretty_time(target)}</b>",
    )


@bot.message_handler(commands=["caption"])
def caption_cmd(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        if get_captions():
            bot.reply_to(
                message,
                "📝 <b>Caption list</b> (serial onujayi, ekta ekta kore):\n\n"
                + format_captions()
                + "\n\n➡️ = porer video-te ei caption jabe.\n"
                "Jog korte: /caption notun caption\n"
                "Bad dite: /delcaption 2 ba /nocaption (shob)",
            )
        else:
            bot.reply_to(
                message,
                "📝 Ekhon kono caption nai.\n"
                "Jog korte: /caption Amar caption",
            )
        return

    text = parts[1].strip()
    if len(text) > 1024:
        bot.reply_to(message, "❌ Caption 1024 akhor-er moddhe rakhen.")
        return

    with settings_lock:
        full = len(settings["captions"]) >= MAX_CAPTIONS
        if not full:
            settings["captions"].append(text)
            total = len(settings["captions"])
            save_settings()
    if full:
        bot.reply_to(
            message, f"❌ Shorbocho {MAX_CAPTIONS} ta caption rakha jay."
        )
        return
    bot.reply_to(
        message,
        f"✅ Caption #{total} jog hoyeche. Ekhon moto {total} ta caption, "
        "video-te serial onujayi jabe.",
    )


@bot.message_handler(commands=["delcaption"])
def delete_caption(message):
    if not is_owner(message):
        return deny(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Likhen: /delcaption 2")
        return
    number = int(parts[1])
    with settings_lock:
        caps = settings["captions"]
        if number < 1 or number > len(caps):
            ok = False
        else:
            ok = True
            caps.pop(number - 1)
            if settings["caption_index"] > number - 1:
                settings["caption_index"] -= 1
            settings["caption_index"] = (
                settings["caption_index"] % len(caps) if caps else 0
            )
            save_settings()
    if not ok:
        bot.reply_to(message, "❌ Ei number e caption nai. /caption dekhen.")
        return
    bot.reply_to(message, f"🗑 Caption #{number} bad dewa holo.")


@bot.message_handler(commands=["nocaption"])
def no_caption(message):
    if not is_owner(message):
        return deny(message)
    with settings_lock:
        settings["captions"] = []
        settings["caption_index"] = 0
        save_settings()
    bot.reply_to(
        message,
        "✅ Shob caption bad dewa holo. Ekhon theke caption chara jabe.",
    )


# --------------------------------------------------------------------------
# Link pathale: sathe sathe download, slot e upload
# --------------------------------------------------------------------------
download_lock = threading.Lock()  # ekshathe shudhu ekta download/upload
bulk_jobs = {}  # job_id -> threading.Event (stop korar jonno)


def normalize_url(url: str) -> str:
    """Link porishkar kore. Instagram-er tracking part (?igsh=...) bad dey."""
    url = url.rstrip(".,;:!?)]}>\"'")
    if "instagram.com" in url.lower():
        url = url.split("?")[0].split("#")[0]
    return url


def send_with_retry(func):
    """Telegram flood limit (429) hole opekkha kore abar try kore."""
    for attempt in range(4):
        try:
            return func()
        except telebot.apihelper.ApiTelegramException as exc:
            if getattr(exc, "error_code", None) == 429 and attempt < 3:
                wait = 5
                try:
                    wait = int(exc.result_json["parameters"]["retry_after"]) + 1
                except Exception:  # noqa: BLE001
                    pass
                log.warning("Flood limit, waiting %s seconds", wait)
                time.sleep(wait)
                continue
            raise


def send_owner_video(file_path: str):
    def _send():
        with open(file_path, "rb") as video:
            return bot.send_video(
                OWNER_ID,
                video,
                caption="💾 Save kora hoyeche.",
                supports_streaming=True,
                timeout=120,
                disable_notification=True,
            )

    return send_with_retry(_send)


def process_link(url: str):
    """Ekta link download kore queue te rakhe. (status, info) return kore."""
    with queue_lock:
        if any(i.get("url") == url for i in load_queue()):
            return "dup", None

    file_path = None
    try:
        with download_lock:
            # 1) Download
            file_path = download_video(url)
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                return "big", None
            # 2) Apnar private chat e save (Telegram-er server e thake)
            sent = send_owner_video(file_path)

        if sent.video:
            kind, media = "video", sent.video
        else:
            kind, media = "document", sent.document

        # 3) Porer free slot e schedule
        slot = enqueue(
            {
                "url": url,
                "label": url,
                "kind": kind,
                "file_id": media.file_id,
                "unique_id": media.file_unique_id,
            }
        )
        return "ok", slot

    except yt_dlp.utils.DownloadError as exc:
        log.warning("Download error for %s: %s", url, exc)
        return "fail", None
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error for %s: %s", url, exc)
        return "fail", None
    finally:
        # CLEANUP: server theke file sathe sathe delete
        safe_remove(file_path)


def run_bulk(chat_id: int, status, urls: list, job_id: str, stop: threading.Event):
    total = len(urls)
    ok_slots, dup, big, failed = [], 0, 0, []
    stopped = False
    n = 1

    def stop_markup():
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("⏹ Bondho koren", callback_data=f"b:{job_id}"))
        return kb

    def edit(text, markup=None):
        try:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=status.message_id,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            pass

    for n, url in enumerate(urls, start=1):
        if stop.is_set():
            stopped = True
            break
        edit(
            f"⏳ {n}/{total} download hocche...\n"
            f"✅ {len(ok_slots)} ta ready   ❌ {len(failed)} ta fail",
            stop_markup(),
        )
        result, info = process_link(url)
        if result == "ok":
            ok_slots.append(info)
        elif result == "dup":
            dup += 1
        elif result == "big":
            big += 1
            failed.append(url)
        else:
            failed.append(url)
        time.sleep(1.5)  # Telegram-er flood limit theke bachte

    bulk_jobs.pop(job_id, None)

    # Ekta matro link hole choto message
    if total == 1 and not stopped:
        if ok_slots:
            edit(
                "✅ Download hoye geche. Channel e jabe: "
                f"<b>{pretty_time(ok_slots[0])}</b>"
            )
        elif dup:
            edit("⚠️ Ei link agei queue te ache.")
        elif big:
            edit("⚠️ Video 50 MB-er boro, Telegram bot eta pathate pare na.")
        else:
            edit(
                "❌ Video download kora gelo na. Private, delete kora, "
                "ba unsupported hote pare. Onno bot diye download kore "
                "ekhane forward korte paren."
            )
        return

    lines = ["🛑 <b>Bondho kora hoyeche.</b>" if stopped else "🏁 <b>Shesh!</b>", ""]
    lines.append(f"✅ Schedule hoyeche: <b>{len(ok_slots)}</b> ta")
    if dup:
        lines.append(f"⚠️ Agei queue te chilo: {dup} ta")
    if big:
        lines.append(f"⚠️ 50 MB-er boro: {big} ta")
    fail_count = len(failed) - big
    if fail_count:
        lines.append(f"❌ Download hoy nai: {fail_count} ta")
    if stopped:
        lines.append(f"⏹ Baki {total - n + 1} ta link process kora hoyni.")
    if ok_slots:
        ok_slots.sort()
        lines.append("")
        lines.append(f"📅 Prothom post: {pretty_time(ok_slots[0])}")
        lines.append(f"📅 Shesh post: {pretty_time(ok_slots[-1])}")
    edit("\n".join(lines))

    if failed:
        out = ["❌ Ei link gulo hoy nai (abar pathan, ba onno bot diye "
               "download kore forward koren):", ""]
        out += [html.escape(u) for u in failed]
        try:
            reply_long(status, out)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not send failed list: %s", exc)


def handle_links(message, urls: list) -> None:
    # Link porishkar kora + ekoi message e duibar thakle ekbar rakha
    seen, clean = set(), []
    for raw in urls:
        url = normalize_url(raw)
        if url not in seen:
            seen.add(url)
            clean.append(url)

    total = len(clean)
    status = bot.reply_to(
        message,
        f"⏳ {total} ta link paoa gelo. Ekta ekta kore download hobe...",
    )

    job_id = uuid.uuid4().hex[:8]
    stop = threading.Event()
    bulk_jobs[job_id] = stop

    # Alada thread e chalano hoy, jate bot menu/button atke na thake
    threading.Thread(
        target=run_bulk,
        args=(message.chat.id, status, clean, job_id, stop),
        daemon=True,
    ).start()


def start_bulk(chat_id: int, raw_urls: list) -> int:
    """Link gulo download + schedule shuru kore (API theke ashle). Koyta link nilo return kore."""
    seen, clean = set(), []
    for raw in raw_urls:
        url = normalize_url(raw)
        if url not in seen:
            seen.add(url)
            clean.append(url)
    clean = clean[:100]
    if not clean:
        return 0

    status = bot.send_message(
        chat_id,
        f"⏳ {len(clean)} ta link paoa gelo (auto). Ekta ekta kore download hobe...",
    )
    job_id = uuid.uuid4().hex[:8]
    stop = threading.Event()
    bulk_jobs[job_id] = stop
    threading.Thread(
        target=run_bulk,
        args=(chat_id, status, clean, job_id, stop),
        daemon=True,
    ).start()
    return len(clean)


# --------------------------------------------------------------------------
# Button menu (shob kichu button diye)
# --------------------------------------------------------------------------
from telebot import types  # noqa: E402

pending = {}  # owner-er opekkhaman input, jemon {"action": "caption"}
MENU_TEXTS = {"📋 Menu", "📅 Queue", "🕒 Slots", "📝 Caption"}
PAGE_SIZE = 8


def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)


def home_row():
    return [btn("🏠 Menu", "m:main")]


def reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Queue", "🕒 Slots")
    kb.row("📝 Caption", "📋 Menu")
    return kb


def find_item(items, short_id):
    for item in items:
        if item["id"].startswith(short_id):
            return item
    return None


def hhmm_pretty(hhmm):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def render(message, screen):
    """Existing message ta edit kore notun screen dekhay."""
    text, kb = screen
    try:
        bot.edit_message_text(
            text,
            message.chat.id,
            message.message_id,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except telebot.apihelper.ApiTelegramException as exc:
        if "message is not modified" not in str(exc):
            raise


def send_screen(chat_id, screen):
    text, kb = screen
    bot.send_message(
        chat_id, text, reply_markup=kb, disable_web_page_preview=True
    )


# ---- Screens ----------------------------------------------------------------
def screen_main():
    with queue_lock:
        items = load_queue()
    lines = ["📋 <b>Menu</b>\n", f"📅 Queue te: <b>{len(items)}</b> ta post"]
    if items:
        lines.append(f"⏭ Porer post: {pretty_time(parse_time(items[0]['time']))}")
    lines.append(f"🕒 Slot: {format_slots()}")
    caption_count = len(get_captions())
    lines.append(
        f"📝 Caption: {caption_count} ta" if caption_count else "📝 Caption: nai"
    )
    lines.append("\nPost forward korun ba Instagram/Facebook link pathan.")
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("📅 Queue", "q:l:0"), btn("🕒 Slots", "s:list"))
    kb.row(btn("📝 Caption", "c:show"), btn("🗑 Shob bad den", "x:ask"))
    kb.row(btn("ℹ️ Help", "h:help"))
    return "\n".join(lines), kb


def screen_help():
    kb = types.InlineKeyboardMarkup()
    kb.row(*home_row())
    text = (
        "ℹ️ <b>Kibhabe bebohar korben</b>\n\n"
        "• Kono post forward korun ba Instagram/Facebook link pathan. "
        "Bot porer khali slot e channel e dibe.\n"
        "• <b>Queue</b>: post-e tap kore somoy bodlan, ekhoni post korun, "
        "ba bad den.\n"
        "• <b>Slots</b>: daily upload-er somoy add ba bad den.\n"
        "• <b>Caption</b>: ekadhik caption jog korte paren. Video-te caption "
        "serial onujayi jabe (1 number, tarpor 2 number...). Forward kora "
        "video-r nijer caption jabe na. Photo ba onno post-e nijer caption "
        "thakle sheta jabe."
    )
    return text, kb


def screen_queue(page=0):
    with queue_lock:
        items = load_queue()
    kb = types.InlineKeyboardMarkup()
    if not items:
        kb.row(*home_row())
        return "📭 Queue khali.", kb
    pages = (len(items) - 1) // PAGE_SIZE + 1
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    for n, item in enumerate(items[start:start + PAGE_SIZE], start=start + 1):
        when = parse_time(item["time"]).astimezone(TZ)
        text = f"{n}. {when.strftime('%d %b %I:%M %p')} • {item_label(item)}"
        kb.row(btn(text[:60], f"qi:{item['id'][:8]}"))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀️", f"q:l:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("▶️", f"q:l:{page + 1}"))
        kb.row(*nav)
    kb.row(*home_row())
    return f"📅 <b>Queue</b> ({len(items)} ta post)\nPost-e tap korun:", kb


def screen_item(short_id):
    with queue_lock:
        items = load_queue()
    item = find_item(items, short_id)
    kb = types.InlineKeyboardMarkup()
    if not item:
        kb.row(btn("⬅️ Queue", "q:l:0"))
        return "❌ Ei post ta ar nai.", kb
    n = items.index(item) + 1
    text = (
        f"📌 <b>Post #{n}</b>\n"
        f"🕒 {pretty_time(parse_time(item['time']))}\n"
        f"📄 {html.escape(item_label(item))}"
    )
    kb.row(
        btn("🕒 Somoy bodlan", f"qt:{short_id}"),
        btn("⚡ Ekhoni post", f"qn:{short_id}"),
    )
    kb.row(btn("🗑 Bad den", f"qd:{short_id}"))
    kb.row(btn("⬅️ Queue", "q:l:0"), *home_row())
    return text, kb


def screen_time(short_id):
    with queue_lock:
        items = load_queue()
    item = find_item(items, short_id)
    kb = types.InlineKeyboardMarkup()
    if not item:
        kb.row(btn("⬅️ Queue", "q:l:0"))
        return "❌ Ei post ta ar nai.", kb
    taken = {parse_time(i["time"]) for i in items if i is not item}
    now = now_local()
    slots = get_slots()
    options = []
    for offset in range(0, 30):
        day = now.date() + timedelta(days=offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=TZ)
            if candidate > now and candidate not in taken:
                options.append(candidate)
        if len(options) >= 10:
            break
    row = []
    for candidate in options[:10]:
        row.append(
            btn(pretty_time(candidate), f"qs:{short_id}:{int(candidate.timestamp())}")
        )
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(btn("⬅️ Back", f"qi:{short_id}"))
    return "🕒 Kon somoy e jabe? (khali slot gulo):", kb


def screen_slots():
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("➕ Slot add", "s:add"), btn("➖ Slot bad den", "s:del"))
    kb.row(*home_row())
    text = (
        f"🕒 <b>Daily slot:</b>\n{format_slots()}\n\n"
        "Slot bad dile oi somoy e ar kono post jabe na."
    )
    return text, kb


def screen_hours():
    kb = types.InlineKeyboardMarkup(row_width=4)
    buttons = [
        btn(datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0"), f"sh:{h:02d}")
        for h in range(24)
    ]
    kb.add(*buttons)
    kb.row(btn("⬅️ Back", "s:list"))
    return "➕ Kon ghonta-r slot add korben?", kb


def screen_minutes(hour):
    kb = types.InlineKeyboardMarkup(row_width=4)
    buttons = [
        btn(f":{mm}", f"sm:{hour}:{mm}")
        for mm in ("00", "10", "15", "20", "30", "40", "45", "50")
    ]
    kb.add(*buttons)
    kb.row(btn("⬅️ Back", "s:add"))
    label = datetime(2000, 1, 1, int(hour)).strftime("%I %p").lstrip("0")
    return f"➕ {label}: koyta minute e?", kb


def screen_slot_del():
    with settings_lock:
        slots = list(settings["slots"])
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[btn(f"❌ {hhmm_pretty(s)}", f"sd:{s.replace(':', '')}") for s in slots])
    kb.row(btn("⬅️ Back", "s:list"))
    return "➖ Kon slot bad dibo? Tap korun:", kb


def screen_caption():
    caps = get_captions()
    kb = types.InlineKeyboardMarkup(row_width=5)
    if caps:
        text = (
            "📝 <b>Caption list</b> (serial onujayi, ekta ekta kore):\n\n"
            + format_captions()
            + "\n\n➡️ = porer video-te ei caption jabe.\n"
            "Photo ba onno post-e nijer caption thakle sheta jabe."
        )
        kb.row(btn("➕ Caption add", "c:set"))
        kb.add(*[btn(f"🗑 {n}", f"c:del:{n}") for n in range(1, len(caps) + 1)])
        kb.row(
            btn("🔄 1 number theke shuru", "c:reset"),
            btn("🗑 Shob bad den", "c:off"),
        )
    else:
        text = (
            "📝 Ekhon kono caption nai.\n\n"
            "Caption add korle video-te serial onujayi jabe. Photo ba onno "
            "post-e nijer caption thakle sheta jabe."
        )
        kb.row(btn("➕ Caption add", "c:set"))
    kb.row(*home_row())
    return text, kb


def screen_confirm_clear():
    with queue_lock:
        count = len(load_queue())
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("✅ Hya, bad den", "x:yes"), btn("❌ Na", "m:main"))
    return f"⚠️ Queue-r {count} ta post bad hoye jabe. Nishchit?", kb


# ---- Slot helpers -----------------------------------------------------------
def add_slot_value(hhmm: str) -> bool:
    with settings_lock:
        if hhmm in settings["slots"]:
            return False
        settings["slots"] = sorted(set(settings["slots"]) | {hhmm})
        save_settings()
    return True


def remove_slot_value(hhmm: str) -> str:
    with settings_lock:
        if hhmm not in settings["slots"]:
            return "missing"
        if len(settings["slots"]) <= 1:
            return "last"
        settings["slots"] = [s for s in settings["slots"] if s != hhmm]
        save_settings()
    return "ok"


# ---- Entry points -----------------------------------------------------------
@bot.message_handler(commands=["start", "menu"])
def open_menu(message):
    if not is_owner(message):
        return deny(message)
    pending.clear()
    bot.send_message(
        message.chat.id,
        "👋 Neeche-r button diye shob kichu korte parben.\n"
        "Kono post forward korun ba link pathan, bot porer khali slot e "
        "channel e dibe.",
        reply_markup=reply_keyboard(),
    )
    send_screen(message.chat.id, screen_main())


def handle_menu_text(message) -> None:
    screens = {
        "📋 Menu": screen_main,
        "📅 Queue": screen_queue,
        "🕒 Slots": screen_slots,
        "📝 Caption": screen_caption,
    }
    send_screen(message.chat.id, screens[message.text]())


def apply_pending_caption(message) -> None:
    text = message.text.strip()
    if len(text) > 1024:
        bot.reply_to(message, "❌ Caption 1024 akhor-er moddhe rakhen. Abar pathan.")
        return
    with settings_lock:
        full = len(settings["captions"]) >= MAX_CAPTIONS
        if not full:
            settings["captions"].append(text)
            total = len(settings["captions"])
            save_settings()
    pending.clear()
    if full:
        bot.reply_to(message, f"❌ Shorbocho {MAX_CAPTIONS} ta caption rakha jay.")
    else:
        bot.reply_to(message, f"✅ Caption #{total} jog hoyeche.")
    send_screen(message.chat.id, screen_caption())


# ---- Button taps ------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(
            call.id, "⛔ Ei bot shudhu owner-er jonno.", show_alert=True
        )
        return

    parts = (call.data or "").split(":")
    key = parts[0]
    msg = call.message
    toast = None

    try:
        if key == "noop":
            pass

        elif key == "m":
            pending.clear()
            render(msg, screen_main())

        elif key == "h":
            render(msg, screen_help())

        # Queue
        elif key == "q":
            render(msg, screen_queue(int(parts[2])))

        elif key == "qi":
            render(msg, screen_item(parts[1]))

        elif key == "qt":
            render(msg, screen_time(parts[1]))

        elif key == "qs":
            new_time = datetime.fromtimestamp(int(parts[2]), TZ)
            with queue_lock:
                items = load_queue()
                item = find_item(items, parts[1])
                if item:
                    item["time"] = new_time.isoformat()
                    item.pop("next_try", None)
                    save_queue(items)
            toast = "✅ Somoy bodlano hoyeche"
            render(msg, screen_item(parts[1]))

        elif key == "qn":
            with queue_lock:
                items = load_queue()
                item = find_item(items, parts[1])
                if item:
                    item["time"] = (now_local() - timedelta(seconds=1)).isoformat()
                    item.pop("next_try", None)
                    save_queue(items)
            toast = "⚡ Kichukkhon er moddhe channel e jabe"
            render(msg, screen_queue(0))

        elif key == "qd":
            with queue_lock:
                items = load_queue()
                item = find_item(items, parts[1])
                if item:
                    items.remove(item)
                    save_queue(items)
            toast = "🗑 Bad dewa holo"
            render(msg, screen_queue(0))

        # Slots
        elif key == "s":
            if parts[1] == "list":
                render(msg, screen_slots())
            elif parts[1] == "add":
                render(msg, screen_hours())
            elif parts[1] == "del":
                render(msg, screen_slot_del())

        elif key == "sh":
            render(msg, screen_minutes(parts[1]))

        elif key == "sm":
            added = add_slot_value(f"{parts[1]}:{parts[2]}")
            toast = "✅ Slot add hoyeche" if added else "⚠️ Ei slot agei ache"
            render(msg, screen_slots())

        elif key == "sd":
            hhmm = f"{parts[1][:2]}:{parts[1][2:]}"
            result = remove_slot_value(hhmm)
            if result == "ok":
                moved = reschedule_affected([hhmm])
                toast = "✅ Slot bad dewa holo"
                if moved:
                    toast += f" ({moved} ta post porer slot e gelo)"
            elif result == "last":
                toast = "❌ Shesh slot ta bad deya jabe na"
            else:
                toast = "⚠️ Ei slot ar nai"
            render(msg, screen_slots())

        # Caption
        elif key == "c":
            if parts[1] == "show":
                render(msg, screen_caption())
            elif parts[1] == "set":
                pending["action"] = "caption"
                kb = types.InlineKeyboardMarkup()
                kb.row(btn("❌ Bondho koren", "p:cancel"))
                bot.send_message(
                    msg.chat.id,
                    "✏️ Notun caption likhe pathan (ekta ekta kore jog hobe):",
                    reply_markup=kb,
                )
            elif parts[1] == "off":
                with settings_lock:
                    settings["captions"] = []
                    settings["caption_index"] = 0
                    save_settings()
                toast = "🗑 Shob caption bad dewa holo"
                render(msg, screen_caption())
            elif parts[1] == "del":
                number = int(parts[2])
                with settings_lock:
                    caps = settings["captions"]
                    if 1 <= number <= len(caps):
                        caps.pop(number - 1)
                        if settings["caption_index"] > number - 1:
                            settings["caption_index"] -= 1
                        settings["caption_index"] = (
                            settings["caption_index"] % len(caps) if caps else 0
                        )
                        save_settings()
                        toast = f"🗑 Caption #{number} bad dewa holo"
                    else:
                        toast = "⚠️ Ei caption ar nai"
                render(msg, screen_caption())
            elif parts[1] == "reset":
                with settings_lock:
                    settings["caption_index"] = 0
                    save_settings()
                toast = "🔄 Ekhon theke 1 number caption theke shuru"
                render(msg, screen_caption())

        elif key == "b":
            stop_event = bulk_jobs.get(parts[1])
            if stop_event:
                stop_event.set()
                toast = "⏹ Cholmaan link shesh hole bondho hobe"
            else:
                toast = "Ei kaj ta agei shesh hoye geche"

        elif key == "p":
            pending.clear()
            bot.edit_message_text(
                "❌ Bondho kora holo.", msg.chat.id, msg.message_id
            )

        # Clear all
        elif key == "x":
            if parts[1] == "ask":
                render(msg, screen_confirm_clear())
            elif parts[1] == "yes":
                with queue_lock:
                    save_queue([])
                toast = "🗑 Shob bad dewa holo"
                render(msg, screen_main())

    except Exception as exc:  # noqa: BLE001
        log.exception("Callback error: %s", exc)
        toast = "❌ Kichu ekta vul hoyeche."
    finally:
        try:
            bot.answer_callback_query(call.id, toast)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Forward kora (ba sorasori pathano) je kono post: porer slot e schedule
# --------------------------------------------------------------------------
def schedule_post(message) -> None:
    media = extract_media(message)

    if media:
        kind, file_id, unique_id, label = media
        with queue_lock:
            if any(i.get("unique_id") == unique_id for i in load_queue()):
                bot.reply_to(message, "⚠️ Ei post agei queue te ache.")
                return
        entry = {
            "label": "📩 " + label,
            "kind": kind,
            "file_id": file_id,
            "unique_id": unique_id,
            "orig_caption": (
                getattr(message, "html_caption", None)
                if message.caption and kind != "video"
                else None
            ),
            "source": "forward",
        }
    else:
        # Text, poll ityadi: original message copy kore pathano hobe
        if message.text:
            snippet = message.text.strip().replace("\n", " ")[:40]
            label = f"📝 {snippet}"
        else:
            label = "📩 Post"
        entry = {
            "label": label,
            "kind": "copy",
            "src_chat": message.chat.id,
            "src_msg": message.message_id,
            "source": "forward",
        }

    slot = enqueue(entry)
    note = ""
    if entry["kind"] in CAPTION_KINDS:
        if entry.get("orig_caption"):
            note = "\n(post-er nijer caption jabe)"
        elif get_caption():
            note = "\n(apnar caption list theke serial onujayi jabe)"
        else:
            note = "\n(caption jabe na)"
    bot.reply_to(
        message,
        f"✅ Channel e jabe: <b>{pretty_time(slot)}</b>{note}",
    )


@bot.message_handler(
    content_types=[
        "text", "photo", "video", "animation", "document", "audio",
        "voice", "video_note", "sticker", "poll",
    ]
)
def handle_any(message):
    if not is_owner(message):
        return deny(message)

    forwarded = bool(
        getattr(message, "forward_date", None)
        or getattr(message, "forward_origin", None)
    )

    # Neeche-r button (Menu / Queue / Slots / Caption) ba caption-er input
    if message.text and not forwarded:
        if message.text in MENU_TEXTS:
            pending.clear()
            handle_menu_text(message)
            return
        if pending.get("action") == "caption" and not message.text.startswith("/"):
            apply_pending_caption(message)
            return

    # Forward kora na, shudhu text
    if message.text and not forwarded:
        if message.text.startswith("/"):
            bot.reply_to(message, "❓ Command bujhini. /help dekhen.")
            return
        urls = URL_PATTERN.findall(message.text)
        if urls:
            handle_links(message, urls)
        else:
            bot.reply_to(
                message,
                "Instagram/Facebook link pathan, ba kono post forward koren.",
            )
        return

    schedule_post(message)


# --------------------------------------------------------------------------
# Private inbox channel: onno bot/app ei channel e link post korle auto download
# --------------------------------------------------------------------------
announced_channels = set()


def is_main_channel(chat) -> bool:
    main = str(CHANNEL_ID).lower().lstrip("@")
    return str(chat.id) == str(CHANNEL_ID) or (
        bool(chat.username) and chat.username.lower() == main
    )


@bot.channel_post_handler(content_types=["text"])
def handle_channel_post(message):
    chat = message.chat

    # Upload-er main channel-er post ignore
    if is_main_channel(chat):
        return

    # INBOX_CHANNEL_ID set na thakle shudhu channel-er ID ta owner ke janay
    if not INBOX_CHANNEL_ID:
        if chat.id not in announced_channels:
            announced_channels.add(chat.id)
            notify_owner(
                "📡 Ekta channel theke post paoa gelo.\n"
                f"Channel: {html.escape(chat.title or '')}\n"
                f"Channel ID: <code>{chat.id}</code>\n\n"
                "Eta Render Environment e <code>INBOX_CHANNEL_ID</code> "
                "hishebe dien."
            )
        return

    if str(chat.id) != INBOX_CHANNEL_ID:
        return

    urls = URL_PATTERN.findall(message.text or "")
    if urls:
        start_bulk(OWNER_ID, urls)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    load_settings()
    keep_alive()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("Link API (/add): %s", "ON" if API_KEY else "OFF (API_KEY nai)")
    log.info("Inbox channel: %s", INBOX_CHANNEL_ID or "set kora nai")
    log.info(
        "Bot is starting... Channel: %s | Owner: %s | Slots: %s",
        CHANNEL_ID, OWNER_ID, ", ".join(settings["slots"]),
    )
    # skip_pending intentionally NOT used: infinity_polling retries on its own
    # if an old instance still holds the getUpdates connection (409).
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=["message", "callback_query", "channel_post"],
    )
