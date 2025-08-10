# bot.py — Full Mongo-backed version (PTB 13.15)
# - Single MongoDB for users, products, config, sessions, locks, payments log
# - UPI QR; no UPI link shown; short professional caption; no buttons
# - Random price in a range (integers first; decimals only if needed)
# - Auto-delivery on PhonePe Business messages:
#     * Supports "Received Rs 11", "Received Rs.11", "Money received", fancy digits (𝟙, 1️⃣), commas, decimals
# - Window = 5m + 10s grace
# - First-file admin add flow fixed

import os, logging, time, random, re, unicodedata
from datetime import datetime, timedelta
from urllib.parse import quote

from telegram import Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext,
    ConversationHandler, CallbackQueryHandler
)

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

# ------------ Logging ------------
logging.basicConfig(format="%(asctime)s %(levelname)s:%(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("upi-mongo-bot")

# ------------ Bot Config ------------
TOKEN = "8352423948:AAEP_WHdxNGziUabzMwO9_YiEp24_d0XYVk"
ADMIN_IDS = [7223414109, 6053105336, 7381642564]

STORAGE_CHANNEL_ID = -1002724249292         # where admin-uploaded files are stored
PAYMENT_NOTIF_CHANNEL_ID = -1002865174188   # channel that receives PhonePe Business messages

UPI_ID = "debjyotimondal1010@okhdfcbank"
UPI_PAYEE_NAME = "Seller"

PAY_WINDOW_MINUTES = 5
GRACE_SECONDS = 10  # total window = 5m + 10s

PROTECT_CONTENT_ENABLED = False
FORCE_SUBSCRIBE_ENABLED = True
FORCE_SUBSCRIBE_CHANNEL_IDS = []  # add channel IDs if you want FS

# ------------ Mongo (Atlas URI) ------------
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://Me:Me@cluster0.3lpdrgm.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)
mdb = MongoClient(MONGO_URI)["upi_bot"]

c_users    = mdb["users"]     # {user_id, username}
c_products = mdb["products"]  # {item_id, min_price, max_price, files:[{channel_id, message_id}]}
c_config   = mdb["config"]    # {key, value}
c_sessions = mdb["sessions"]  # {key, user_id, chat_id, item_id, amount, amount_key, created_at, hard_expire_at}
c_locks    = mdb["locks"]     # {amount_key, hard_expire_at, created_at}
c_paylog   = mdb["payments"]  # optional logs {key, ts, raw}

# Indexes (idempotent)
c_users.create_index([("user_id", ASCENDING)], unique=True)
c_products.create_index([("item_id", ASCENDING)], unique=True)
c_config.create_index([("key", ASCENDING)], unique=True)

c_locks.create_index([("amount_key", ASCENDING)], unique=True)
c_locks.create_index([("hard_expire_at", ASCENDING)], expireAfterSeconds=0)

c_sessions.create_index([("key", ASCENDING)], unique=True)
c_sessions.create_index([("amount_key", ASCENDING)])
c_sessions.create_index([("hard_expire_at", ASCENDING)], expireAfterSeconds=0)

c_paylog.create_index([("ts", ASCENDING)])

# ------------ Helpers ------------
def cfg(key: str, default=None):
    doc = c_config.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default

def set_cfg(key: str, value):
    c_config.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def amount_key(x: float) -> str:
    return f"{x:.2f}" if abs(x - int(x)) > 1e-9 else str(int(x))

def build_upi_uri(amount: float, note: str):
    amt = f"{int(amount)}" if abs(amount-int(amount))<1e-9 else f"{amount:.2f}"
    pa = quote(UPI_ID, safe='')
    pn = quote(UPI_PAYEE_NAME, safe='')
    tn = quote(note, safe='')
    return f"upi://pay?pa={pa}&pn={pn}&am={amt}&cu=INR&tn={tn}"

def qr_url(data: str):
    return f"https://api.qrserver.com/v1/create-qr-code/?data={quote(data, safe='')}&size=512x512&qzone=2"

def add_user(uid: int, uname: str):
    c_users.update_one({"user_id": uid}, {"$set": {"username": uname or ""}}, upsert=True)

def get_all_user_ids():
    return list(c_users.distinct("user_id"))

# Amount reservation (global uniqueness via Mongo)
def reserve_amount_key(k: str, hard_expire_at: datetime) -> bool:
    try:
        c_locks.insert_one({
            "amount_key": k,
            "hard_expire_at": hard_expire_at,
            "created_at": datetime.utcnow()
        })
        return True
    except DuplicateKeyError:
        return False

def release_amount_key(k: str):
    c_locks.delete_one({"amount_key": k})

def pick_unique_amount(lo: float, hi: float, hard_expire_at: datetime) -> float:
    lo, hi = int(lo), int(hi)
    ints = list(range(lo, hi+1))
    random.shuffle(ints)
    # try integers first
    for v in ints:
        k = str(v)
        if reserve_amount_key(k, hard_expire_at):
            return float(v)
    # fall back to decimals if all ints are busy
    for base in ints:
        for p in range(1, 100):
            k = f"{base}.{p:02d}"
            if reserve_amount_key(k, hard_expire_at):
                return float(f"{base}.{p:02d}")
    return float(ints[-1])

# ---- PhonePe amount parsing (handles fancy digits like 𝟙 and keycaps 1️⃣) ----
def _normalize_digits(s: str) -> str:
    out = []
    for ch in s:
        # drop combining marks (keycap enclosure, variation selectors)
        if unicodedata.category(ch).startswith('M'):
            continue
        # map any unicode decimal digit to ASCII 0-9
        if ch.isdigit():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except Exception:
                pass
        out.append(ch)
    return "".join(out)

# Allow "received rs" or "you've received rs" and tolerate punctuation after "rs"
PHONEPE_RE = re.compile(
    r"(?:received\s*rs|you['’]ve\s*received\s*rs)\s*[.:₹\s]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.I | re.S
)

def parse_phonepe_amount(text: str):
    norm = _normalize_digits(text or "")
    m = PHONEPE_RE.search(norm)
    if not m:
        return None
    token = m.group(1).replace(",", "")
    try:
        return float(token)
    except Exception:
        return None

# ... [Truncated: rest of code is same as above in last message]
