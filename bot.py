import os, re, json, time, uuid, hmac, html, logging, threading
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
import telebot
import yt_dlp
from flask import Flask, request
from telebot import types

# ---------- Supabase Setup ----------
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE = "bot_storage"

def load_json(name):
    try:
        res = supabase.table(TABLE).select("data").eq("key", name).execute()
        if res.data:
            return res.data[0]["data"]
    except Exception as e:
        logging.error(f"❌ Supabase LOAD error ({name}): {e}")
    return None

def save_json(name, data):
    try:
        # ডেটা যেনো ঠিকমতো JSON এ কনভার্ট হয়
        clean_data = json.loads(json.dumps(data))
        # স্পেসিফিক key এর ওপর আপডেট হবে
        supabase.table(TABLE).upsert(
            {
                "key": name, 
                "data": clean_data, 
                "updated_at": datetime.now(ZoneInfo("UTC")).isoformat()
            }, 
            on_conflict="key"
        ).execute()
        return True
    except Exception as e:
        logging.error(f"❌ Supabase SAVE error ({name}): {e}")
        return False

# ---------- Config ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")
DEFAULT_CHANNEL = os.environ.get("CHANNEL_ID", "@viral_koci_video")
OWNER_ID = int(os.environ.get("OWNER_ID", "1749461380"))
API_KEY = os.environ.get("API_KEY", "")
INBOX_CHANNEL_ID = os.environ.get("INBOX_CHANNEL_ID", "").strip()
DEFAULT_CAPTION = os.environ.get("CAPTION", "")
DEFAULT_SLOTS = os.environ.get("SLOT_TIMES", "08:00,09:00,10:00,11:00,12:00,17:00,18:00,19:00")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Dhaka"))
PORT = int(os.environ.get("PORT", 10000))

DOWNLOAD_DIR = "downloads"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_ATTEMPTS = 3
RETRY_DELAY_MIN = 5
CAPTION_KINDS = {"photo", "video", "animation", "document", "audio", "voice"}
MAX_CAPTIONS = 30
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("video-bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
URL_PATTERN = re.compile(r"(https?://(?:www\.|m\.|web\.|mbasic\.)?(?:instagram\.com|facebook\.com|fb\.watch|fb\.com)/[^\s]+)", re.IGNORECASE)

# ---------- Flask ----------
app = Flask(__name__)

@app.route("/")
def home(): return "Bot is alive!", 200

@app.route("/health")
def health(): return {"status": "ok"}, 200

@app.route("/add", methods=["GET", "POST"])
def api_add():
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

def run_flask(): app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Keep-alive Flask server started on port %s", PORT)

# ---------- Slots parser ----------
def parse_slot_list(text: str) -> list:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    slots = set()
    for part in parts:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m: raise ValueError(part)
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59: raise ValueError(part)
        slots.add(f"{h:02d}:{mi:02d}")
    if not slots: raise ValueError("empty")
    return sorted(slots)

# ---------- Channels ----------
channels_lock = threading.Lock()
channels = {}
active_channels = {}

def new_channel_entry(cid, title=""):
    return {
        "id": str(cid), "title": title or str(cid),
        "slots": parse_slot_list(DEFAULT_SLOTS),
        "captions": [c.strip() for c in DEFAULT_CAPTION.split("||") if c.strip()],
        "caption_index": 0,
    }

def save_channels():
    with channels_lock:
        save_json("channels", list(channels.values()))

def load_channels():
    global channels
    data_list = load_json("channels")
    data = {}
    if isinstance(data_list, list):
        for ch in data_list: data[ch["id"]] = ch
    if not data: 
        data[DEFAULT_CHANNEL] = new_channel_entry(DEFAULT_CHANNEL)
    channels = data
    save_channels()

def load_active():
    global active_channels
    data = load_json("active_channel")
    active_channels = {int(k): v for k, v in data.items()} if isinstance(data, dict) else {}

def save_active():
    save_json("active_channel", {str(k): v for k, v in active_channels.items()})

def get_active_channel_id(uid):
    with channels_lock:
        cid = active_channels.get(uid)
        if cid and cid in channels: return cid
        if channels:
            first = next(iter(channels.keys()))
            active_channels[uid] = first
            save_active()
            return first
        return None

def get_active_channel(uid):
    cid = get_active_channel_id(uid)
    with channels_lock: return channels.get(cid) if cid else None

def set_active_channel(uid, cid):
    with channels_lock:
        if cid in channels:
            active_channels[uid] = cid
            save_active()
            return True
        return False

def channel_slot_times(cid):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return []
        return [dtime(int(s.split(":")[0]), int(s.split(":")[1])) for s in ch["slots"]]

def channel_captions(cid):
    with channels_lock:
        ch = channels.get(cid)
        return list(ch["captions"]) if ch else []

def get_channel_caption(cid):
    with channels_lock:
        ch = channels.get(cid)
        if not ch or not ch["captions"]: return None
        return ch["captions"][ch["caption_index"] % len(ch["captions"])]

def advance_channel_caption(cid):
    with channels_lock:
        ch = channels.get(cid)
        if ch and ch["captions"]:
            ch["caption_index"] = (ch["caption_index"] + 1) % len(ch["captions"])
            save_channels()

def channel_caption_info(cid):
    with channels_lock:
        ch = channels.get(cid)
        if not ch or not ch["captions"]: return None
        idx = ch["caption_index"] % len(ch["captions"])
        return idx, ch["captions"][idx]

def format_channel_slots(cid):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return ""
        return ", ".join(datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0") for s in ch["slots"])

def format_channel_captions(cid):
    caps = channel_captions(cid); info = channel_caption_info(cid); lines = []
    for n, t in enumerate(caps, start=1):
        marker = "➡️ " if info and info[0] == n - 1 else ""
        preview = t.replace("\n", " ")
        if len(preview) > 80: preview = preview[:80] + "…"
        lines.append(f"{marker}{n}. {html.escape(preview)}")
    return "\n".join(lines)

def add_caption(cid, text):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return False, "channel"
        if len(ch["captions"]) >= MAX_CAPTIONS: return False, "limit"
        ch["captions"].append(text); n = len(ch["captions"]); save_channels()
    return True, n

def remove_caption(cid, num):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return False
        caps = ch["captions"]
        if num < 1 or num > len(caps): return False
        caps.pop(num - 1)
        if ch["caption_index"] > num - 1: ch["caption_index"] -= 1
        ch["caption_index"] = (ch["caption_index"] % len(caps)) if caps else 0
        save_channels()
    return True

def clear_captions(cid):
    with channels_lock:
        ch = channels.get(cid)
        if ch: ch["captions"] = []; ch["caption_index"] = 0; save_channels()

def reset_caption_index(cid):
    with channels_lock:
        ch = channels.get(cid)
        if ch: ch["caption_index"] = 0; save_channels()

def add_slot_value(cid, hhmm):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return False
        if hhmm in ch["slots"]: return False
        ch["slots"] = sorted(set(ch["slots"]) | {hhmm}); save_channels()
    return True

def remove_slot_value(cid, hhmm):
    with channels_lock:
        ch = channels.get(cid)
        if not ch: return "missing"
        if hhmm not in ch["slots"]: return "missing"
        if len(ch["slots"]) <= 1: return "last"
        ch["slots"] = [s for s in ch["slots"] if s != hhmm]; save_channels()
    return "ok"

# ---------- Admins ----------
admins_lock = threading.Lock()
admins = set()

def load_admins():
    global admins
    data = load_json("admins")
    admins = set(data) if isinstance(data, list) else set()
    admins.add(OWNER_ID)

def save_admins():
    with admins_lock:
        save_json("admins", sorted(admins))

def is_owner(uid): return uid == OWNER_ID
def is_admin(uid):
    with admins_lock: return uid == OWNER_ID or uid in admins

# ---------- Queue ----------
queue_lock = threading.Lock()
def now_local(): return datetime.now(TZ)
def parse_time(v): return datetime.fromisoformat(v)

def load_queue():
    data = load_json("queue")
    if not isinstance(data, list): return []
    with channels_lock: default_ch = next(iter(channels.keys())) if channels else DEFAULT_CHANNEL
    for i in data:
        if "channel" not in i: i["channel"] = default_ch
    return data

def save_queue(items):
    with queue_lock:
        items.sort(key=lambda i: i["time"])
        save_json("queue", items)

def next_free_slot(items, cid):
    taken = {parse_time(i["time"]) for i in items if i.get("channel") == cid}
    now = now_local(); slots = channel_slot_times(cid)
    if not slots: raise RuntimeError(f"No slots for {cid}")
    for d in range(0, 365):
        day = now.date() + timedelta(days=d)
        for slot in slots:
            cand = datetime.combine(day, slot, tzinfo=TZ)
            if cand > now and cand not in taken: return cand
    raise RuntimeError("No free slot")

def pretty_time(dt):
    dt = dt.astimezone(TZ); today = now_local().date()
    if dt.date() == today: day = "Aj"
    elif dt.date() == today + timedelta(days=1): day = "Agamikal"
    else: day = dt.strftime("%d %b")
    return f"{day}, {dt.strftime('%I:%M %p')}"

def item_label(it): return it.get("label") or it.get("url") or "post"

def enqueue(entry, cid):
    with queue_lock:
        items = load_queue()
        slot = next_free_slot(items, cid)
        entry.update({"id": uuid.uuid4().hex, "time": slot.isoformat(), "attempts": 0, "channel": cid})
        items.append(entry); save_queue(items)
    return slot

def reschedule_all(cid):
    with queue_lock:
        all_items = load_queue()
        other = [i for i in all_items if i.get("channel") != cid]
        mine = sorted([i for i in all_items if i.get("channel") == cid], key=lambda i: i["time"])
        new_mine = []
        for it in mine:
            it.pop("next_try", None)
            it["time"] = next_free_slot(new_mine, cid).isoformat(); new_mine.append(it)
        save_queue(other + new_mine)

def reschedule_affected(removed, cid):
    with queue_lock:
        all_items = load_queue()
        other = [i for i in all_items if i.get("channel") != cid]
        mine = [i for i in all_items if i.get("channel") == cid]
        keep, moved = [], []
        for it in mine:
            hhmm = parse_time(it["time"]).astimezone(TZ).strftime("%H:%M")
            (moved if hhmm in removed else keep).append(it)
        moved.sort(key=lambda i: i["time"])
        for it in moved:
            it.pop("next_try", None)
            it["time"] = next_free_slot(keep, cid).isoformat(); keep.append(it)
        save_queue(other + keep)
    return len(moved)

# ---------- Download & Posting ----------
def download_video(url):
    fid = uuid.uuid4().hex
    tmpl = os.path.join(DOWNLOAD_DIR, f"{fid}.%(ext)s")
    opts = {"outtmpl": tmpl, "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "socket_timeout": 30, "retries": 3}
    if os.path.exists("cookies.txt"): opts["cookiefile"] = "cookies.txt"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = ydl.prepare_filename(info)
    if not os.path.exists(p):
        for n in os.listdir(DOWNLOAD_DIR):
            if n.startswith(fid): p = os.path.join(DOWNLOAD_DIR, n); break
    return p

def safe_remove(path):
    try:
        if path and os.path.exists(path): os.remove(path); log.info("Deleted %s", path)
    except OSError as exc: log.error("del %s: %s", path, exc)

def notify_owner(text):
    try: bot.send_message(OWNER_ID, text)
    except Exception as exc: log.error("notify owner: %s", exc)

def build_caption_kw(cid):
    cap = get_channel_caption(cid)
    if not cap: return {}
    return {"caption": f"<b>{html.escape(cap)}</b>", "parse_mode": "HTML"}

def send_item(item):
    cid = item.get("channel") or DEFAULT_CHANNEL
    kind = item.get("kind", "video")
    if kind == "copy":
        bot.copy_message(cid, item["src_chat"], item["src_msg"]); return
    file_id = item["file_id"]; kw = {}; used_bot_cap = False
    if kind in CAPTION_KINDS:
        orig = None if kind == "video" else item.get("orig_caption")
        if orig:
            kw["caption"] = orig; kw["parse_mode"] = "HTML"
        else:
            cap = get_channel_caption(cid)
            if cap:
                kw["caption"] = f"<b>{html.escape(cap)}</b>"
                kw["parse_mode"] = "HTML"; used_bot_cap = True
    if kind == "photo": bot.send_photo(cid, file_id, **kw)
    elif kind == "video": bot.send_video(cid, file_id, supports_streaming=True, timeout=120, **kw)
    elif kind == "animation": bot.send_animation(cid, file_id, timeout=120, **kw)
    elif kind == "document": bot.send_document(cid, file_id, timeout=120, **kw)
    elif kind == "audio": bot.send_audio(cid, file_id, timeout=120, **kw)
    elif kind == "voice": bot.send_voice(cid, file_id, timeout=120, **kw)
    elif kind == "video_note": bot.send_video_note(cid, file_id, timeout=120)
    elif kind == "sticker": bot.send_sticker(cid, file_id)
    else: raise ValueError(f"Unknown kind: {kind}")
    if used_bot_cap: advance_channel_caption(cid)

def post_item(item):
    if item.get("file_id") or item.get("kind") == "copy":
        send_item(item); return
    cid = item.get("channel") or DEFAULT_CHANNEL; fp = None
    try:
        fp = download_video(item["url"])
        if os.path.getsize(fp) > MAX_FILE_SIZE: raise ValueError("Video 50 MB-er boro.")
        kw = build_caption_kw(cid); used = bool(kw)
        with open(fp, "rb") as v:
            bot.send_video(cid, v, supports_streaming=True, timeout=120, **kw)
        if used: advance_channel_caption(cid)
    finally: safe_remove(fp)

def process_due():
    now = now_local()
    with queue_lock:
        due = [dict(i) for i in load_queue()
               if parse_time(i["time"]) <= now
               and ("next_try" not in i or parse_time(i["next_try"]) <= now)]
    for item in due:
        log.info("Posting %s -> %s", item_label(item), item.get("channel"))
        try: post_item(item)
        except Exception as exc:
            log.warning("post failed: %s", exc)
            with queue_lock:
                items = load_queue()
                for i in items:
                    if i["id"] == item["id"]:
                        i["attempts"] = i.get("attempts", 0) + 1
                        if i["attempts"] >= MAX_ATTEMPTS:
                            items.remove(i)
                            notify_owner(f"❌ Upload fail ({MAX_ATTEMPTS}x), queue theke bad:\n{html.escape(item_label(item))}")
                        else:
                            i["next_try"] = (now_local() + timedelta(minutes=RETRY_DELAY_MIN)).isoformat()
                        break
                save_queue(items)
            continue
        with queue_lock:
            items = [i for i in load_queue() if i["id"] != item["id"]]
            save_queue(items)
            rem = len([i for i in items if i.get("channel") == item.get("channel")])
        notify_owner(f"✅ Upload hoyeche ({html.escape(str(item.get('channel')))}). Queue baki: {rem} ta.")

def scheduler_loop():
    log.info("Scheduler started.")
    while True:
        try: process_due()
        except Exception as exc: log.exception("sched: %s", exc)
        time.sleep(20)

# ---------- Helpers ----------
def is_admin_message(m): return m.from_user is not None and is_admin(m.from_user.id)
def is_owner_message(m): return m.from_user is not None and is_owner(m.from_user.id)
def deny(m): bot.reply_to(m, "⛔ Ei bot shudhu admin-der jonno.")

def reply_long(m, lines):
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            bot.send_message(m.chat.id, chunk, disable_web_page_preview=True); chunk = ""
        chunk += line + "\n"
    if chunk: bot.send_message(m.chat.id, chunk, disable_web_page_preview=True)

def extract_media(m):
    if m.photo: x = m.photo[-1]; return "photo", x.file_id, x.file_unique_id, "🖼 Photo"
    if m.video: x = m.video; return "video", x.file_id, x.file_unique_id, "🎬 Video"
    if m.animation: x = m.animation; return "animation", x.file_id, x.file_unique_id, "🎞 GIF"
    if m.document: x = m.document; return "document", x.file_id, x.file_unique_id, "📄 File"
    if m.audio: x = m.audio; return "audio", x.file_id, x.file_unique_id, "🎵 Audio"
    if m.voice: x = m.voice; return "voice", x.file_id, x.file_unique_id, "🎤 Voice"
    if m.video_note: x = m.video_note; return "video_note", x.file_id, x.file_unique_id, "⭕ Video note"
    if m.sticker: x = m.sticker; return "sticker", x.file_id, x.file_unique_id, "🏷 Sticker"
    return None

# ---------- Menu UI ----------
pending = {}
MENU_TEXTS = {"📋 Menu", "📅 Queue", "🕒 Slots", "📝 Caption"}
PAGE_SIZE = 8

def btn(text, data): return types.InlineKeyboardButton(text, callback_data=data)
def home_row(): return [btn("🏠 Menu", "m:main")]

def reply_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Queue", "🕒 Slots"); kb.row("📝 Caption", "📋 Menu")
    return kb

def find_item_any(items, short_id):
    for it in items:
        if it["id"].startswith(short_id): return it
    return None

def hhmm_pretty(h): return datetime.strptime(h, "%H:%M").strftime("%I:%M %p").lstrip("0")

def render(message, screen):
    text, kb = screen
    try: bot.edit_message_text(text, message.chat.id, message.message_id,
                                reply_markup=kb, disable_web_page_preview=True)
    except telebot.apihelper.ApiTelegramException as exc:
        if "message is not modified" not in str(exc): raise

def send_screen(chat_id, screen):
    text, kb = screen
    bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)

def screen_main(uid):
    cid = get_active_channel_id(uid); ch = get_active_channel(uid)
    with queue_lock: items = [i for i in load_queue() if i.get("channel") == cid]
    name = html.escape(ch["title"]) if ch else "—"
    lines = [f"📋 <b>Menu</b>\n📡 Channel: <b>{name}</b>", f"📅 Queue: <b>{len(items)}</b>"]
    if items: lines.append(f"⏭ Porer: {pretty_time(parse_time(items[0]['time']))}")
    lines.append(f"🕒 Slot: {format_channel_slots(cid)}")
    cc = len(channel_captions(cid))
    lines.append(f"📝 Caption: {cc} ta (bold)" if cc else "📝 Caption: nai")
    lines.append("\nPost forward korun ba link pathan.")
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("📅 Queue", "q:l:0"), btn("🕒 Slots", "s:list"))
    kb.row(btn("📝 Caption", "c:show"), btn("📡 Channel", "ch:list"))
    kb.row(btn("🗑 Shob bad den", "x:ask"), btn("ℹ️ Help", "h:help"))
    return "\n".join(lines), kb

def screen_help(uid):
    kb = types.InlineKeyboardMarkup(); kb.row(*home_row())
    return ("ℹ️ <b>Help</b>\n\n• Post forward / link pathan — active channel e porer slot e jabe.\n"
            "• <b>Channel</b> → active channel select.\n• <b>Caption</b> serial onujayi bold hoye jabe.\n"
            "• Owner: /addchannel /delchannel /addadmin /deladmin"), kb

def screen_queue(uid, page=0):
    cid = get_active_channel_id(uid)
    with queue_lock: items = [i for i in load_queue() if i.get("channel") == cid]
    kb = types.InlineKeyboardMarkup()
    if not items: kb.row(*home_row()); return "📭 Queue khali.", kb
    pages = (len(items) - 1) // PAGE_SIZE + 1
    page = max(0, min(page, pages - 1)); start = page * PAGE_SIZE
    for n, it in enumerate(items[start:start+PAGE_SIZE], start=start+1):
        when = parse_time(it["time"]).astimezone(TZ)
        text = f"{n}. {when.strftime('%d %b %I:%M %p')} • {item_label(it)}"
        kb.row(btn(text[:60], f"qi:{it['id'][:8]}"))
    if pages > 1:
        nav = []
        if page > 0: nav.append(btn("◀️", f"q:l:{page-1}"))
        nav.append(btn(f"{page+1}/{pages}", "noop"))
        if page < pages - 1: nav.append(btn("▶️", f"q:l:{page+1}"))
        kb.row(*nav)
    kb.row(*home_row())
    return f"📅 <b>Queue</b> ({len(items)}) — <code>{html.escape(str(cid))}</code>", kb

def screen_item(uid, sid):
    with queue_lock: items = load_queue()
    it = find_item_any(items, sid); kb = types.InlineKeyboardMarkup()
    if not it: kb.row(btn("⬅️ Queue", "q:l:0")); return "❌ Nai.", kb
    mine = [i for i in items if i.get("channel") == it.get("channel")]
    n = mine.index(it) + 1
    text = (f"📌 <b>Post #{n}</b>\n📡 {html.escape(str(it.get('channel')))}\n"
            f"🕒 {pretty_time(parse_time(it['time']))}\n📄 {html.escape(item_label(it))}")
    kb.row(btn("🕒 Somoy", f"qt:{sid}"), btn("⚡ Ekhoni", f"qn:{sid}"))
    kb.row(btn("🗑 Bad", f"qd:{sid}")); kb.row(btn("⬅️ Queue", "q:l:0"), *home_row())
    return text, kb

def screen_time(uid, sid):
    with queue_lock: items = load_queue()
    it = find_item_any(items, sid); kb = types.InlineKeyboardMarkup()
    if not it: kb.row(btn("⬅️ Queue", "q:l:0")); return "❌ Nai.", kb
    cid = it.get("channel")
    taken = {parse_time(i["time"]) for i in items if i["id"] != it["id"] and i.get("channel") == cid}
    now = now_local(); slots = channel_slot_times(cid); opts = []
    for off in range(0, 30):
        day = now.date() + timedelta(days=off)
        for s in slots:
            c = datetime.combine(day, s, tzinfo=TZ)
            if c > now and c not in taken: opts.append(c)
        if len(opts) >= 10: break
    row = []
    for c in opts[:10]:
        row.append(btn(pretty_time(c), f"qs:{sid}:{int(c.timestamp())}"))
        if len(row) == 2: kb.row(*row); row = []
    if row: kb.row(*row)
    kb.row(btn("⬅️ Back", f"qi:{sid}"))
    return "🕒 Khali slot:", kb

def screen_slots(uid):
    cid = get_active_channel_id(uid); kb = types.InlineKeyboardMarkup()
    kb.row(btn("➕ Add", "s:add"), btn("➖ Bad", "s:del")); kb.row(*home_row())
    return f"🕒 <b>Slot</b> — {html.escape(str(cid))}:\n{format_channel_slots(cid)}", kb

def screen_hours(uid):
    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[btn(datetime(2000,1,1,h).strftime("%I %p").lstrip("0"), f"sh:{h:02d}") for h in range(24)])
    kb.row(btn("⬅️ Back", "s:list"))
    return "➕ Ghonta:", kb

def screen_minutes(uid, hour):
    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[btn(f":{mm}", f"sm:{hour}:{mm}") for mm in ("00","10","15","20","30","40","45","50")])
    kb.row(btn("⬅️ Back", "s:add"))
    return f"➕ {datetime(2000,1,1,int(hour)).strftime('%I %p').lstrip('0')} — minute:", kb

def screen_slot_del(uid):
    cid = get_active_channel_id(uid)
    with channels_lock: slots = list(channels.get(cid, {}).get("slots", []))
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[btn(f"❌ {hhmm_pretty(s)}", f"sd:{s.replace(':', '')}") for s in slots])
    kb.row(btn("⬅️ Back", "s:list"))
    return "➖ Slot bad:", kb

def screen_caption(uid):
    cid = get_active_channel_id(uid); caps = channel_captions(cid)
    kb = types.InlineKeyboardMarkup(row_width=5)
    if caps:
        text = (f"📝 <b>Caption</b> — {html.escape(str(cid))}\n\n"
                + format_channel_captions(cid)
                + "\n\n➡️ = porer video-te ei caption jabe (<b>bold hoye</b>).")
        kb.row(btn("➕ Add", "c:set"))
        kb.add(*[btn(f"🗑 {n}", f"c:del:{n}") for n in range(1, len(caps)+1)])
        kb.row(btn("🔄 Reset", "c:reset"), btn("🗑 Shob", "c:off"))
    else:
        text = "📝 Caption nai.\nAdd korle video-te serial onujayi <b>bold</b> hoye jabe."
        kb.row(btn("➕ Add", "c:set"))
    kb.row(*home_row())
    return text, kb

def screen_channels(uid):
    with channels_lock: items = list(channels.items())
    active = get_active_channel_id(uid)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (cid, ch) in enumerate(items, start=1):
        mark = "✅" if cid == active else "▫️"
        kb.add(btn(f"{mark} {ch['title']}"[:60], f"ch:set:{i}"))
    if is_owner(uid):
        kb.row(btn("➕ Channel add", "ch:add"), btn("➖ Bad", "ch:del"))
    kb.row(*home_row())
    return "📡 <b>Channels</b>\nEkta tap korle active hobe:", kb

def screen_channel_del(uid):
    with channels_lock: items = list(channels.items())
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (cid, ch) in enumerate(items, start=1):
        kb.add(btn(f"❌ {ch['title']}"[:60], f"ch:del:{i}"))
    kb.row(btn("⬅️ Back", "ch:list"))
    return "➖ Kon channel bad dibo?", kb

def screen_confirm_clear(uid):
    cid = get_active_channel_id(uid)
    with queue_lock: cnt = len([i for i in load_queue() if i.get("channel") == cid])
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("✅ Hya", "x:yes"), btn("❌ Na", "m:main"))
    return f"⚠️ {cid} er {cnt} ta post bad hobe. Nishchit?", kb

# ---------- Bot Handlers ----------
@bot.message_handler(commands=["help", "start", "menu"])
def open_menu(m):
    if not is_admin_message(m): return deny(m)
    pending.clear()
    bot.send_message(m.chat.id, "👋 Button diye shob kichu.\nPost forward ba link pathan.",
                     reply_markup=reply_keyboard())
    send_screen(m.chat.id, screen_main(m.from_user.id))

@bot.message_handler(commands=["testdb"])
def test_db(m):
    if not is_owner_message(m): return deny(m)
    if save_json("test_ping", {"status": "ok", "time": now_local().isoformat()}):
        if load_json("test_ping"):
            bot.reply_to(m, "✅ Supabase 100% ঠিক আছে! ডেটা সেভ এবং লোড হচ্ছে।")
        else:
            bot.reply_to(m, "⚠️ সেভ হয়েছে কিন্তু লোড হচ্ছে না।")
    else:
        bot.reply_to(m, "❌ সেভ হচ্ছে না! Render-এর লগ চেক করুন।")

@bot.message_handler(commands=["channels"])
def channels_cmd(m):
    if not is_admin_message(m): return deny(m)
    send_screen(m.chat.id, screen_channels(m.from_user.id))

@bot.message_handler(commands=["admins"])
def list_admins(m):
    if not is_owner_message(m): return deny(m)
    with admins_lock: lst = sorted(admins)
    lines = ["👥 <b>Admins:</b>\n"]
    for uid in lst:
        tag = "👑 Owner" if uid == OWNER_ID else "👤 Admin"
        lines.append(f"{tag} — <code>{uid}</code>")
    reply_long(m, lines)

@bot.message_handler(commands=["addadmin", "deladmin"])
def manage_admin(m):
    if not is_owner_message(m): return deny(m)
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return bot.reply_to(m, "Likhen: /addadmin 123456789")
    uid = int(parts[1])
    is_add = m.text.startswith("/addadmin")
    with admins_lock:
        if is_add:
            if uid in admins: return bot.reply_to(m, "⚠️ Agei admin.")
            admins.add(uid); save_admins()
            bot.reply_to(m, f"✅ Admin add: <code>{uid}</code>")
        else:
            if uid == OWNER_ID: return bot.reply_to(m, "❌ Owner bad jabe na.")
            if uid not in admins: return bot.reply_to(m, "⚠️ Admin na.")
            admins.discard(uid); save_admins()
            bot.reply_to(m, f"🗑 Admin bad: <code>{uid}</code>")

def handle_menu_text(m):
    uid = m.from_user.id
    if m.text == "📋 Menu": send_screen(m.chat.id, screen_main(uid))
    elif m.text == "📅 Queue": send_screen(m.chat.id, screen_queue(uid))
    elif m.text == "🕒 Slots": send_screen(m.chat.id, screen_slots(uid))
    elif m.text == "📝 Caption": send_screen(m.chat.id, screen_caption(uid))

def apply_pending_caption(m):
    text = m.text.strip()
    if len(text) > 1024: return bot.reply_to(m, "❌ 1024 akhor max.")
    cid = get_active_channel_id(m.from_user.id)
    ok, info = add_caption(cid, text); pending.clear()
    if not ok: bot.reply_to(m, "❌ Caption add fail.")
    else: bot.reply_to(m, f"✅ Caption #{info} jog.")
    send_screen(m.chat.id, screen_caption(m.from_user.id))

def apply_pending_channel(m):
    parts = m.text.strip().split(maxsplit=1)
    raw = parts[0]; title = parts[1].strip() if len(parts) > 1 else ""
    pending.clear()
    if not is_owner(m.from_user.id): return bot.reply_to(m, "❌ Owner only.")
    try:
        chat = bot.get_chat(raw); real = str(chat.id); title = title or chat.title or real
        try:
            bm = bot.get_chat_member(chat.id, bot.get_me().id)
            if bm.status not in ("administrator", "creator"):
                return bot.reply_to(m, "❌ Bot ke channel admin banate hobe.")
        except Exception: pass
        with channels_lock:
            if real in channels: return bot.reply_to(m, "⚠️ Agei ache.")
            channels[real] = new_channel_entry(real, title); save_channels()
        bot.reply_to(m, f"✅ Add: {html.escape(title)}")
        send_screen(m.chat.id, screen_channels(m.from_user.id))
    except Exception as exc: bot.reply_to(m, f"❌ {exc}")

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Admin only.", show_alert=True); return
    uid = call.from_user.id; cid = get_active_channel_id(uid)
    parts = (call.data or "").split(":"); key = parts[0]; msg = call.message; toast = None
    try:
        if key == "noop": pass
        elif key == "m": pending.clear(); render(msg, screen_main(uid))
        elif key == "h": render(msg, screen_help(uid))
        elif key == "q": render(msg, screen_queue(uid, int(parts[2])))
        elif key == "qi": render(msg, screen_item(uid, parts[1]))
        elif key == "qt": render(msg, screen_time(uid, parts[1]))
        elif key == "qs":
            nt = datetime.fromtimestamp(int(parts[2]), TZ)
            with queue_lock:
                items = load_queue()
                for it in items:
                    if it["id"].startswith(parts[1]):
                        it["time"] = nt.isoformat(); it.pop("next_try", None); break
                save_queue(items)
            toast = "✅ Somoy bodlano"; render(msg, screen_item(uid, parts[1]))
        elif key == "qn":
            with queue_lock:
                items = load_queue()
                for it in items:
                    if it["id"].startswith(parts[1]):
                        it["time"] = (now_local() - timedelta(seconds=1)).isoformat()
                        it.pop("next_try", None); break
                save_queue(items)
            toast = "⚡ Ekhoni jabe"; render(msg, screen_queue(uid, 0))
        elif key == "qd":
            with queue_lock:
                items = [i for i in load_queue() if not i["id"].startswith(parts[1])]
                save_queue(items)
            toast = "🗑 Bad"; render(msg, screen_queue(uid, 0))
        elif key == "s":
            if parts[1] == "list": render(msg, screen_slots(uid))
            elif parts[1] == "add": render(msg, screen_hours(uid))
            elif parts[1] == "del": render(msg, screen_slot_del(uid))
        elif key == "sh": render(msg, screen_minutes(uid, parts[1]))
        elif key == "sm":
            added = add_slot_value(cid, f"{parts[1]}:{parts[2]}")
            toast = "✅ Add" if added else "⚠️ Agei ache"; render(msg, screen_slots(uid))
        elif key == "sd":
            hhmm = f"{parts[1][:2]}:{parts[1][2:]}"
            r = remove_slot_value(cid, hhmm)
            if r == "ok":
                moved = reschedule_affected([hhmm], cid)
                toast = f"✅ Bad" + (f" ({moved} moved)" if moved else "")
            elif r == "last": toast = "❌ Shesh slot bad jabe na"
            else: toast = "⚠️ Nai"
            render(msg, screen_slots(uid))
        elif key == "c":
            if parts[1] == "show": render(msg, screen_caption(uid))
            elif parts[1] == "set":
                pending["action"] = "caption"
                kb = types.InlineKeyboardMarkup(); kb.row(btn("❌ Cancel", "p:cancel"))
                bot.send_message(msg.chat.id, "✏️ Caption likhe pathan (bold hoye jabe):", reply_markup=kb)
            elif parts[1] == "off": clear_captions(cid); toast = "🗑 Clear"; render(msg, screen_caption(uid))
            elif parts[1] == "del":
                n = int(parts[2])
                if remove_caption(cid, n): toast = f"🗑 Caption #{n}"
                else: toast = "⚠️ Nai"
                render(msg, screen_caption(uid))
            elif parts[1] == "reset": reset_caption_index(cid); toast = "🔄 Reset"; render(msg, screen_caption(uid))
        elif key == "ch":
            if parts[1] == "list": render(msg, screen_channels(uid))
            elif parts[1] == "set":
                with channels_lock: keys = list(channels.keys())
                idx = int(parts[2]) - 1
                if 0 <= idx < len(keys):
                    set_active_channel(uid, keys[idx]); toast = "✅ Select hoyeche"
                render(msg, screen_channels(uid))
            elif parts[1] == "add":
                if not is_owner(uid): toast = "❌ Owner only"
                else:
                    pending["action"] = "channel"
                    kb = types.InlineKeyboardMarkup(); kb.row(btn("❌ Cancel", "p:cancel"))
                    bot.send_message(msg.chat.id,
                        "✏️ Pathan: <code>@channel username [Title]</code>\n"
                        "ba <code>-1001234567890 [Title]</code>\n"
                        "(Bot ke oi channel e admin banate hobe)", reply_markup=kb)
            elif parts[1] == "del":
                if not is_owner(uid): toast = "❌ Owner only"
                else: render(msg, screen_channel_del(uid))
        elif key == "b":
            ev = bulk_jobs.get(parts[1])
            if ev: ev.set(); toast = "⏹ Stop cholche"
            else: toast = "Agei shesh"
        elif key == "p":
            pending.clear()
            bot.edit_message_text("❌ Cancel.", msg.chat.id, msg.message_id)
        elif key == "x":
            if parts[1] == "ask": render(msg, screen_confirm_clear(uid))
            elif parts[1] == "yes":
                with queue_lock:
                    items = [i for i in load_queue() if i.get("channel") != cid]
                    save_queue(items)
                toast = "🗑 Clear"; render(msg, screen_main(uid))
        elif key == "ch" and parts[1] == "del" and len(parts) >= 3:
            if not is_owner(uid): toast = "❌ Owner only"
            else:
                with channels_lock: keys = list(channels.keys())
                idx = int(parts[2]) - 1
                if 0 <= idx < len(keys):
                    target = keys[idx]
                    with channels_lock:
                        if len(channels) <= 1: toast = "❌ Shesh channel"
                        else:
                            channels.pop(target, None); save_channels()
                            for k, v in list(active_channels.items()):
                                if v == target and channels:
                                    active_channels[k] = next(iter(channels.keys()))
                            save_active()
                            with queue_lock:
                                items = [i for i in load_queue() if i.get("channel") != target]
                                save_queue(items)
                            toast = "🗑 Channel bad"
                render(msg, screen_channels(uid))
    except Exception as exc:
        log.exception("cb: %s", exc); toast = "❌ Error"
    finally:
        try: bot.answer_callback_query(call.id, toast)
        except Exception: pass

def schedule_post(m):
    media = extract_media(m); cid = get_active_channel_id(m.from_user.id)
    if media:
        kind, fid, uid_, label = media
        with queue_lock:
            if any(i.get("unique_id") == uid_ and i.get("channel") == cid for i in load_queue()):
                return bot.reply_to(m, "⚠️ Agei queue te ache.")
        entry = {"label": "📩 " + label, "kind": kind, "file_id": fid, "unique_id": uid_,
                 "orig_caption": getattr(m, "html_caption", None) if m.caption and kind != "video" else None,
                 "source": "forward"}
    else:
        if m.text:
            snip = m.text.strip().replace("\n", " ")[:40]
            label = f"📝 {snip}"
        else: label = "📩 Post"
        entry = {"label": label, "kind": "copy", "src_chat": m.chat.id, "src_msg": m.message_id,
                 "source": "forward"}
    slot = enqueue(entry, cid)
    note = ""
    if entry["kind"] in CAPTION_KINDS:
        if entry.get("orig_caption"): note = "\n(post-er nijer caption)"
        elif get_channel_caption(cid): note = "\n(caption bold hoye serial onujayi jabe)"
        else: note = "\n(caption jabe na)"
    bot.reply_to(m, f"✅ <b>{html.escape(str(cid))}</b> e jabe: <b>{pretty_time(slot)}</b>{note}")

@bot.message_handler(content_types=["text","photo","video","animation","document","audio","voice","video_note","sticker","poll"])
def handle_any(m):
    if not is_admin_message(m): return deny(m)
    forwarded = bool(getattr(m, "forward_date", None) or getattr(m, "forward_origin", None))
    if m.text and not forwarded:
        if m.text in MENU_TEXTS: pending.clear(); handle_menu_text(m); return
        if pending.get("action") == "caption" and not m.text.startswith("/"):
            apply_pending_caption(m); return
        if pending.get("action") == "channel" and not m.text.startswith("/"):
            apply_pending_channel(m); return
        if m.text.startswith("/"):
            bot.reply_to(m, "❓ Command bujhini. /help dekhen."); return
        urls = URL_PATTERN.findall(m.text)
        if urls: start_bulk(m.from_user.id, urls)
        else: bot.reply_to(m, "Instagram/Facebook link pathan, ba post forward korun.")
        return
    schedule_post(m)

# ---------- Inbox channel ----------
announced = set()

def is_any_main_channel(chat):
    with channels_lock:
        for cid in channels:
            if str(chat.id) == cid: return True
            if chat.username and cid.lower().lstrip("@") == chat.username.lower(): return True
    return False

@bot.channel_post_handler(content_types=["text"])
def handle_channel_post(m):
    chat = m.chat
    if is_any_main_channel(chat): return
    if not INBOX_CHANNEL_ID:
        if chat.id not in announced:
            announced.add(chat.id)
            notify_owner("📡 Inbox channel paoa gelo.\n"
                         f"Channel: {html.escape(chat.title or '')}\nID: <code>{chat.id}</code>\n"
                         "Render env e <code>INBOX_CHANNEL_ID</code> set korun.\n"
                         "Ba /addchannel diye channel hishebe add korun.")
        return
    if str(chat.id) != INBOX_CHANNEL_ID: return
    urls = URL_PATTERN.findall(m.text or "")
    if urls: start_bulk(OWNER_ID, urls)

# ---------- Bulk Downloader ----------
download_lock = threading.Lock()
bulk_jobs = {}

def normalize_url(url):
    url = url.rstrip(".,;:!?)]}>\"'")
    if "instagram.com" in url.lower(): url = url.split("?")[0].split("#")[0]
    return url

def send_with_retry(func):
    for attempt in range(4):
        try: return func()
        except telebot.apihelper.ApiTelegramException as exc:
            if getattr(exc, "error_code", None) == 429 and attempt < 3:
                wait = 5
                try: wait = int(exc.result_json["parameters"]["retry_after"]) + 1
                except Exception: pass
                log.warning("Flood wait %ss", wait); time.sleep(wait); continue
            raise

def send_saved_video(chat_id, fp):
    def _():
        with open(fp, "rb") as v:
            return bot.send_video(chat_id, v, caption="💾 Saved.", supports_streaming=True,
                                  timeout=120, disable_notification=True)
    return send_with_retry(_)

def process_link(url, cid, user_id):
    with queue_lock:
        if any(i.get("url") == url and i.get("channel") == cid for i in load_queue()):
            return "dup", None
    fp = None
    try:
        with download_lock:
            fp = download_video(url)
            if os.path.getsize(fp) > MAX_FILE_SIZE: return "big", None
            sent = send_saved_video(user_id, fp)
        if sent.video: kind, media = "video", sent.video
        else: kind, media = "document", sent.document
        slot = enqueue({"url": url, "label": url, "kind": kind,
                        "file_id": media.file_id, "unique_id": media.file_unique_id}, cid)
        return "ok", slot
    except yt_dlp.utils.DownloadError as exc:
        log.warning("DL error %s: %s", url, exc); return "fail", None
    except Exception as exc:
        log.exception("Unexpected %s: %s", url, exc); return "fail", None
    finally: safe_remove(fp)

def run_bulk(user_id, cid, status, urls, job_id, stop):
    total = len(urls); ok_slots, dup, big, failed = [], 0, 0, []
    stopped = False; n = 1
    def stop_markup():
        kb = types.InlineKeyboardMarkup(); kb.row(types.InlineKeyboardButton("⏹ Bondho", callback_data=f"b:{job_id}")); return kb
    def edit(text, markup=None):
        try: bot.edit_message_text(text, chat_id=user_id, message_id=status.message_id,
                                    reply_markup=markup, disable_web_page_preview=True)
        except Exception: pass
    for n, url in enumerate(urls, start=1):
        if stop.is_set(): stopped = True; break
        edit(f"⏳ {n}/{total} download...\n✅ {len(ok_slots)} ready  ❌ {len(failed)} fail", stop_markup())
        r, info = process_link(url, cid, user_id)
        if r == "ok": ok_slots.append(info)
        elif r == "dup": dup += 1
        elif r == "big": big += 1; failed.append(url)
        else: failed.append(url)
        time.sleep(1.5)
    bulk_jobs.pop(job_id, None)
    if total == 1 and not stopped:
        if ok_slots: edit(f"✅ Ready. <b>{html.escape(str(cid))}</b> e jabe: <b>{pretty_time(ok_slots[0])}</b>")
        elif dup: edit("⚠️ Agei queue te ache.")
        elif big: edit("⚠️ 50 MB boro.")
        else: edit("❌ Download fail. Private/deleted/unsupported.")
        return
    lines = ["🛑 <b>Bondho.</b>" if stopped else "🏁 <b>Shesh!</b>", ""]
    lines.append(f"✅ Schedule: <b>{len(ok_slots)}</b>")
    if dup: lines.append(f"⚠️ Duplicate: {dup}")
    if big: lines.append(f"⚠️ 50MB+: {big}")
    if len(failed) - big: lines.append(f"❌ Fail: {len(failed) - big}")
    if stopped: lines.append(f"⏹ Baki: {total - n + 1}")
    if ok_slots:
        ok_slots.sort()
        lines.append(""); lines.append(f"📅 Prothom: {pretty_time(ok_slots[0])}")
        lines.append(f"📅 Shesh: {pretty_time(ok_slots[-1])}")
    edit("\n".join(lines))
    if failed:
        out = ["❌ Fail gulo:", ""] + [html.escape(u) for u in failed]
        try: reply_long(status, out)
        except Exception as exc: log.error("failed list: %s", exc)

def start_bulk(user_id, raw_urls):
    cid = get_active_channel_id(user_id)
    if not cid:
        try: bot.send_message(user_id, "❌ Kono channel nai. /addchannel diye add korun.")
        except Exception: pass
        return 0
    seen, clean = set(), []
    for raw in raw_urls:
        u = normalize_url(raw)
        if u not in seen: seen.add(u); clean.append(u)
    clean = clean[:100]
    if not clean: return 0
    status = bot.send_message(user_id, f"⏳ {len(clean)} ta link paoa gelo → {html.escape(str(cid))}")
    job_id = uuid.uuid4().hex[:8]; stop = threading.Event(); bulk_jobs[job_id] = stop
    threading.Thread(target=run_bulk, args=(user_id, cid, status, clean, job_id, stop), daemon=True).start()
    return len(clean)

# ---------- Entry ----------
if __name__ == "__main__":
    load_channels()
    load_active()
    load_admins()
    keep_alive()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("API: %s | Inbox: %s", "ON" if API_KEY else "OFF", INBOX_CHANNEL_ID or "not set")
    log.info("Channels: %s | Admins: %s", list(channels.keys()), sorted(admins))
    bot.infinity_polling(timeout=30, long_polling_timeout=30,
                         allowed_updates=["message","callback_query","channel_post"])
