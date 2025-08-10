# bot.py — Full Mongo-backed version (PTB 13.15)
# - Single MongoDB for users, products, config, sessions, locks, payments log
# - UPI QR; no UPI link shown; short professional caption; no buttons
# - Random price in a range (integers first; decimals only if needed)
# - Auto-delivery on PhonePe Business "Received Rs ..." messages (handles fancy digits like 𝟙 and keycaps 1️⃣)
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

PHONEPE_RE = re.compile(
    r"(?:received\s*rs|you['’]ve\s*received\s*rs)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
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

# ------------ Force Subscribe ------------
def force_subscribe(fn):
    def wrapper(update: Update, context: CallbackContext, *a, **k):
        if (not FORCE_SUBSCRIBE_ENABLED) or (not FORCE_SUBSCRIBE_CHANNEL_IDS) or (update.effective_user.id in ADMIN_IDS):
            return fn(update, context, *a, **k)
        uid = update.effective_user.id
        need = []
        for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
            try:
                mem = context.bot.get_chat_member(ch, uid)
                if mem.status not in ("member", "administrator", "creator"):
                    need.append(ch)
            except Exception:
                need.append(ch)
        if not need:
            return fn(update, context, *a, **k)

        context.user_data['pending_command'] = {'fn': fn, 'update': update}
        btns = []
        for ch in need:
            try:
                chat = context.bot.get_chat(ch)
                link = chat.invite_link or context.bot.export_chat_invite_link(ch)
                btns.append([InlineKeyboardButton(f"Join {chat.title}", url=link)])
            except Exception as e:
                log.warning(f"Invite link fail {ch}: {e}")
        btns.append([InlineKeyboardButton("✅ I have joined", callback_data="check_join")])

        msg = cfg("force_sub_text", "Join required channels to continue.")
        photo = cfg("force_sub_photo_id")
        if photo:
            update.effective_message.reply_photo(photo=photo, caption=msg, reply_markup=InlineKeyboardMarkup(btns))
        else:
            update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(btns))
    return wrapper

def check_join_cb(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    need = []
    for ch in FORCE_SUBSCRIBE_CHANNEL_IDS:
        try:
            mem = context.bot.get_chat_member(ch, uid)
            if mem.status not in ("member","administrator","creator"):
                need.append(ch)
        except Exception:
            need.append(ch)
    if not need:
        try: q.message.delete()
        except: pass
        q.answer("Thank you!", show_alert=True)
        pend = context.user_data.pop('pending_command', None)
        if pend: return pend['fn'](pend['update'], context)
    else:
        q.answer("Still not joined all.", show_alert=True)

# ------------ Purchase / Delivery ------------
def start_purchase(ctx: CallbackContext, chat_id: int, uid: int, item_id: str):
    prod = c_products.find_one({"item_id": item_id})
    if not prod:
        return ctx.bot.send_message(chat_id, "❌ Item not found.")

    mn = prod.get("min_price")
    mx = prod.get("max_price")
    if mn is None or mx is None:
        v = float(prod.get("price", 0))
        if v <= 0:
            return ctx.bot.send_message(chat_id, "❌ Price not set.")
        mn = mx = v

    created = datetime.utcnow()
    hard_expire_at = created + timedelta(minutes=PAY_WINDOW_MINUTES, seconds=GRACE_SECONDS)

    amt = pick_unique_amount(mn, mx, hard_expire_at)
    akey = amount_key(amt)

    # QR is generated from UPI URI, but we do not show the link
    uri = build_upi_uri(amt, f"order_uid_{uid}")
    img = qr_url(uri)

    display_amt = int(amt) if abs(amt-int(amt))<1e-9 else f"{amt:.2f}"
    caption = (
        f"Pay ₹{display_amt} to `{UPI_ID}`.\n\n"
        "Instructions:\n"
        "• Open any UPI app (GPay / PhonePe / Paytm)\n"
        "• Scan this QR or copy the UPI ID\n"
        f"• Pay exactly ₹{display_amt} within {PAY_WINDOW_MINUTES} minutes\n"
        "Verification is automatic. Files arrive after payment."
    )

    ctx.bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode=ParseMode.MARKDOWN)

    sess_key = f"{uid}:{item_id}:{int(time.time())}"
    c_sessions.insert_one({
        "key": sess_key,
        "user_id": uid,
        "chat_id": chat_id,
        "item_id": item_id,
        "amount": float(amt),
        "amount_key": akey,
        "created_at": created,
        "hard_expire_at": hard_expire_at,
    })

def deliver(ctx: CallbackContext, uid: int, item_id: str):
    prod = c_products.find_one({"item_id": item_id})
    if not prod:
        ctx.bot.send_message(uid, "❌ Item missing.")
        return
    files = prod.get("files", [])
    for f in files:
        try:
            ctx.bot.copy_message(chat_id=uid, from_chat_id=f["channel_id"], message_id=f["message_id"],
                                 protect_content=PROTECT_CONTENT_ENABLED)
            time.sleep(0.35)
        except Exception as e:
            log.error(f"Deliver fail: {e}")
    ctx.bot.send_message(uid, "⚠️ Files auto-delete here in 10 minutes. Save now.")

# ------------ Handlers ------------
@force_subscribe
def cmd_start(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    add_user(uid, update.effective_user.username)
    msg = update.message or (update.callback_query and update.callback_query.message)
    chat_id = msg.chat_id
    if context.args:
        item_id = context.args[0]
        return start_purchase(context, chat_id, uid, item_id)
    # Welcome
    photo = cfg("welcome_photo_id")
    text = cfg("welcome_text", "Welcome!")
    if photo:
        msg.reply_photo(photo=photo, caption=text)
    else:
        msg.reply_text(text)

def cancel_conv(update: Update, context: CallbackContext):
    context.user_data.clear()
    update.message.reply_text("Canceled.")
    return ConversationHandler.END

# --- Admin: add product (file-first; supports "10" or "10-30") ---
GET_PRODUCT_FILES, PRICE, \
GET_BROADCAST_FILES, GET_BROADCAST_TEXT, BROADCAST_CONFIRM = range(5)

def add_product_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data['new_files'] = []

    if update.message.effective_attachment:
        try:
            fwd = context.bot.forward_message(STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id)
            context.user_data['new_files'].append({"channel_id": fwd.chat_id, "message_id": fwd.message_id})
            update.message.reply_text("✅ First file added. Send more or /done.")
        except Exception as e:
            log.error(f"Store fail on first file: {e}")
            update.message.reply_text("Failed to store the first file, send again or /cancel.")
    else:
        update.message.reply_text("Send product files now. Use /done when finished.")

    return GET_PRODUCT_FILES

def get_product_files(update: Update, context: CallbackContext):
    if not update.message.effective_attachment:
        update.message.reply_text("Not a file. Send again or /done.")
        return GET_PRODUCT_FILES
    try:
        fwd = context.bot.forward_message(STORAGE_CHANNEL_ID, update.message.chat_id, update.message.message_id)
        context.user_data['new_files'].append({"channel_id": fwd.chat_id, "message_id": fwd.message_id})
        update.message.reply_text("✅ Added. Send more or /done.")
        return GET_PRODUCT_FILES
    except Exception as e:
        log.error(e)
        update.message.reply_text("Store failed.")
        return ConversationHandler.END

def finish_adding_files(update: Update, context: CallbackContext):
    if not context.user_data.get('new_files'):
        update.message.reply_text("No files yet. Send one or /cancel.")
        return GET_PRODUCT_FILES
    update.message.reply_text("Now send price or range (10 or 10-30).")
    return PRICE

def get_price(update: Update, context: CallbackContext):
    txt = update.message.text.strip()
    try:
        if "-" in txt:
            a,b = txt.split("-",1); mn, mx = float(a), float(b); assert mn>0 and mx>=mn
        else:
            v = float(txt); assert v>0; mn=mx=v
    except Exception:
        update.message.reply_text("Invalid. Send like 10 or 10-30.")
        return PRICE

    item_id = f"item_{int(time.time())}"
    doc = {"item_id": item_id, "min_price": mn, "max_price": mx, "files": context.user_data['new_files']}
    if mn == mx:
        doc["price"] = mn
    c_products.insert_one(doc)

    link = f"https://t.me/{context.bot.username}?start={item_id}"
    update.message.reply_text(f"✅ Product added.\nLink:\n`{link}`", parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear()
    return ConversationHandler.END

# --- Broadcast (optional) ---
def bc_start(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data['b_files']=[]; context.user_data['b_text']=None
    update.message.reply_text("Send files for broadcast. /done when finished.")
    return GET_BROADCAST_FILES

def bc_files(update: Update, context: CallbackContext):
    if update.message.effective_attachment:
        context.user_data['b_files'].append(update.message)
        update.message.reply_text("File added. /done when finished.")
    else:
        update.message.reply_text("Send a file or /done.")
    return GET_BROADCAST_FILES

def bc_done_files(update: Update, context: CallbackContext):
    update.message.reply_text("Now send the text (or /skip).")
    return GET_BROADCAST_TEXT

def bc_text(update: Update, context: CallbackContext):
    context.user_data['b_text']=update.message.text
    return bc_confirm(update, context)

def bc_skip(update: Update, context: CallbackContext):
    return bc_confirm(update, context)

def bc_confirm(update: Update, context: CallbackContext):
    total = c_users.count_documents({})
    buttons=[[InlineKeyboardButton("✅ Send", callback_data="send_bc")],
             [InlineKeyboardButton("❌ Cancel", callback_data="cancel_bc")]]
    update.message.reply_text(f"Broadcast to {total} users. Proceed?", reply_markup=InlineKeyboardMarkup(buttons))
    return BROADCAST_CONFIRM

def bc_send(update: Update, context: CallbackContext):
    q=update.callback_query; q.answer(); q.edit_message_text("Broadcasting…")
    files=context.user_data.get('b_files',[]); text=context.user_data.get('b_text')
    ok=fail=0
    for uid in get_all_user_ids():
        try:
            for m in files:
                context.bot.copy_message(uid, m.chat_id, m.message_id); time.sleep(0.1)
            if text: context.bot.send_message(uid, text)
            ok+=1
        except Exception as e:
            log.error(e); fail+=1
    q.message.reply_text(f"Done. Sent:{ok} Fail:{fail}")
    context.user_data.clear()
    return ConversationHandler.END

# --- Callback for FS only ---
def on_cb(update: Update, context: CallbackContext):
    q=update.callback_query; q.answer()
    if q.data == "check_join":
        return check_join_cb(update, context)

# --- Channel payment sniffer (PhonePe Business) ---
def on_channel_post(update: Update, context: CallbackContext):
    msg = update.channel_post
    if not msg or msg.chat_id != PAYMENT_NOTIF_CHANNEL_ID:
        return
    text = msg.text or msg.caption or ""
    low = text.lower()

    # Require the PhonePe Business signature and "Received Rs"
    if ("phonepe business" not in low) or ("received rs" not in low):
        return

    amt = parse_phonepe_amount(text)
    if amt is None:
        return

    ts = (msg.date or datetime.utcnow()).replace(tzinfo=None)
    akey = amount_key(amt)

    # optional log
    try:
        c_paylog.insert_one({"key": akey, "ts": ts, "raw": text[:500]})
    except Exception:
        pass

    # find matching sessions (amount + within window)
    matches = list(c_sessions.find({
        "amount_key": akey,
        "created_at": {"$lte": ts},
        "hard_expire_at": {"$gte": ts}
    }))

    for s in matches:
        try:
            context.bot.send_message(s["chat_id"], "✅ Payment received. Delivering your files…")
        except Exception as e:
            log.warning(f"Notify user fail: {e}")
        deliver(context, s["user_id"], s["item_id"])
        # cleanup session + release lock
        c_sessions.delete_one({"_id": s["_id"]})
        release_amount_key(akey)

# --- Admin toggles / stats ---
def stats(update: Update, context: CallbackContext):
    users = c_users.count_documents({})
    sessions = c_sessions.count_documents({})
    update.message.reply_text(f"Users: {users}\nPending sessions: {sessions}")

def protect_on(update: Update, context: CallbackContext):
    global PROTECT_CONTENT_ENABLED
    PROTECT_CONTENT_ENABLED=True
    update.message.reply_text("Content protection ON.")

def protect_off(update: Update, context: CallbackContext):
    global PROTECT_CONTENT_ENABLED
    PROTECT_CONTENT_ENABLED=False
    update.message.reply_text("Content protection OFF.")

# ------------ Main ------------
def main():
    # Defaults
    set_cfg("welcome_text", cfg("welcome_text", "Welcome!"))
    set_cfg("force_sub_text", cfg("force_sub_text", "Join required channels to continue."))

    # Polling (no webhook)
    os.system(f'curl -s "https://api.telegram.org/bot{TOKEN}/deleteWebhook" >/dev/null')

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    admin = Filters.user(ADMIN_IDS)

    # Add product flow
    add_conv = ConversationHandler(
        entry_points=[MessageHandler((Filters.document | Filters.video | Filters.photo) & admin, add_product_start)],
        states={
            GET_PRODUCT_FILES: [MessageHandler((Filters.document | Filters.video | Filters.photo) & ~Filters.command, get_product_files),
                                CommandHandler('done', finish_adding_files, filters=admin)],
            PRICE: [MessageHandler(Filters.text & ~Filters.command, get_price)]
        },
        fallbacks=[CommandHandler('cancel', cancel_conv, filters=admin)]
    )

    # Broadcast flow
    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", bc_start, filters=admin)],
        states={
            GET_BROADCAST_FILES: [MessageHandler(Filters.all & ~Filters.command, bc_files),
                                  CommandHandler('done', bc_done_files, filters=admin)],
            GET_BROADCAST_TEXT: [MessageHandler(Filters.text & ~Filters.command, bc_text),
                                 CommandHandler('skip', bc_skip, filters=admin)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(bc_send, pattern="^send_bc$"),
                                CallbackQueryHandler(cancel_conv, pattern="^cancel_bc$")]
        },
        fallbacks=[]
    )

    dp.add_handler(add_conv)
    dp.add_handler(bc_conv)

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("stats", stats, filters=admin))
    dp.add_handler(CommandHandler("protect_on", protect_on, filters=admin))
    dp.add_handler(CommandHandler("protect_off", protect_off, filters=admin))

    dp.add_handler(CallbackQueryHandler(on_cb, pattern="^(check_join)$"))

    # Channel listener (PhonePe Business)
    dp.add_handler(MessageHandler(
        Filters.update.channel_post & Filters.chat(PAYMENT_NOTIF_CHANNEL_ID) & Filters.text,
        on_channel_post
    ))

    log.info("Bot running…")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
