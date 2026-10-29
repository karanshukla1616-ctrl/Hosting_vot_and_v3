"""
Telegram Group & Channel Manager Bot
Features: welcome/goodbye, owner panel, chat relay, voting system, moderation.
"""
import asyncio
import html
import logging
import os
import re
import threading
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import (ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup,
                      MessageEntity, Update)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

load_dotenv()
import db  # noqa: E402  (after load_dotenv so DB_PATH env works)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
OWNER = next(iter(sorted(ADMIN_IDS)), None)
ANON_ADMIN = 1087968824  # GroupAnonymousBot

DEFAULT_WELCOME = "👋 Welcome {mention} to <b>{group}</b>!\nAap hamare {count}th member ho."
DEFAULT_GOODBYE = "👋 {fullname} ne group chhod diya."

HELP_TEXT = """<b>📖 Bot Commands</b>

<b>👋 Welcome</b>
/setwelcome &lt;text&gt; – welcome message set
/setgoodbye &lt;text&gt; – goodbye message set
/welcome on|off · /goodbye on|off
/showwelcome · /resetwelcome
Placeholders: {first} {last} {fullname} {username} {mention} {id} {group} {count}
(HTML tags like &lt;b&gt;bold&lt;/b&gt; chalte hain)

<b>🗳 Vote / Election</b>
/election &lt;title&gt; – naya vote shuru
/candidate – reply karke candidate add (ya user id)
/rmcandidate – candidate hatao
/addvotes &lt;n&gt; – reply karke candidate ke votes badhao (n negative = kam)
/results · /endelection

<b>🛡 Moderation</b>
/ban /unban /kick /mute [10m|2h|1d] /unmute
/warn /warns /resetwarns /setwarnlimit &lt;n&gt;
/del (reply) · /pin · /unpin · /promote · /demote
/antilink on|off · /setrules &lt;text&gt; · /rules

<b>ℹ️ Other</b>
/id · /info · /start · /help
"""

OWNER_HELP = """<b>👑 Owner Commands</b>
/stats – bot stats
/chats – sab groups/channels
/say &lt;chat_id&gt; &lt;text&gt; – kisi bhi chat me bot se message bhejo
/replyto &lt;chat_id&gt; &lt;msg_id&gt; &lt;text&gt; – kisi message ka reply
/broadcast &lt;text&gt; – sab users + chats ko (ya kisi message ko reply karke)
/banuser &lt;id&gt; · /unbanuser &lt;id&gt; – bot se block
/leave &lt;chat_id&gt; – chat chhodo

User ka private message tumhe forward hota hai – us par <b>reply</b> karo to user ko bot se jawab chala jata hai."""


# ----------------------------- helpers -----------------------------
def esc(s):
    return html.escape(str(s or ""))


def mention(u):
    return f'<a href="tg://user?id={u.id}">{esc(u.first_name or u.full_name or u.id)}</a>'


def is_owner(uid):
    return uid in ADMIN_IDS


async def is_admin(context, chat_id, uid):
    if is_owner(uid) or uid == ANON_ADMIN:
        return True
    try:
        m = await context.bot.get_chat_member(chat_id, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


def group_admin(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat, user = update.effective_chat, update.effective_user
        if chat.type == ChatType.PRIVATE:
            return await update.message.reply_text("⚠️ Ye command group me use karo.")
        if not await is_admin(context, chat.id, user.id):
            return await update.message.reply_text("🚫 Sirf admins ye command chala sakte hain.")
        db.upsert_chat(chat.id, chat.title, chat.type)
        return await fn(update, context)
    return wrapper


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            return
        return await fn(update, context)
    return wrapper


def arg_text(update: Update) -> str:
    parts = (update.message.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def safe_send(bot, chat_id, text, **kw):
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **kw)
    except BadRequest:
        return await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text), **kw)


async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, args = update.message, list(context.args or [])
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.full_name, args
    if args and args[0].isdigit():
        uid = int(args.pop(0))
        try:
            name = (await context.bot.get_chat(uid)).full_name or str(uid)
        except TelegramError:
            name = str(uid)
        return uid, name, args
    return None, None, args


def parse_duration(s):
    m = re.fullmatch(r"(\d+)([mhdw])", (s or "").lower())
    if not m:
        return None
    n, unit = int(m[1]), m[2]
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def fmt_welcome(text, user, chat, count):
    rep = {
        "{first}": esc(user.first_name), "{last}": esc(user.last_name),
        "{fullname}": esc(user.full_name),
        "{username}": f"@{user.username}" if user.username else esc(user.full_name),
        "{mention}": mention(user), "{id}": str(user.id),
        "{group}": esc(chat.title), "{count}": str(count),
    }
    for k, v in rep.items():
        text = text.replace(k, v)
    return text


# ----------------------------- start / help -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type != ChatType.PRIVATE:
        return await update.message.reply_text("👋 Main active hoon! Commands ke liye /help.")
    db.upsert_user(user)
    me = await context.bot.get_me()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Group me add karo", url=f"https://t.me/{me.username}?startgroup=true&admin=delete_messages+restrict_members+pin_messages+invite_users+promote_members")],
        [InlineKeyboardButton("📢 Channel me add karo", url=f"https://t.me/{me.username}?startchannel=true&admin=post_messages+edit_messages")],
        [InlineKeyboardButton("📖 Help", callback_data="help")],
    ])
    await update.message.reply_text(
        f"👋 Namaste <b>{esc(user.first_name)}</b>!\n\n"
        "Main ek powerful group manager bot hoon:\n"
        "• Welcome / Goodbye messages\n• Vote / Election system\n• Moderation (ban, mute, warn, anti-link)\n"
        "• Admin se direct chat\n\nMujhe apne group ya channel me <b>admin</b> bana kar add karo.",
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_TEXT + ("\n" + OWNER_HELP if is_owner(update.effective_user.id) else "")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    target = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    await msg.reply_text(f"👤 User ID: <code>{target.id}</code>\n💬 Chat ID: <code>{chat.id}</code>",
                         parse_mode=ParseMode.HTML)


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    u = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    text = (f"<b>👤 User Info</b>\nName: {esc(u.full_name)}\nID: <code>{u.id}</code>\n"
            f"Username: {'@' + u.username if u.username else '—'}")
    if chat.type != ChatType.PRIVATE:
        try:
            m = await context.bot.get_chat_member(chat.id, u.id)
            text += f"\nStatus: {m.status}\nWarns: {db.get_warns(chat.id, u.id)}"
        except TelegramError:
            pass
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


# ----------------------------- welcome / goodbye -----------------------------
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    db.upsert_chat(chat.id, chat.title, chat.type)
    cfg = db.get_chat(chat.id)
    if not cfg["welcome_on"]:
        return
    try:
        count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError:
        count = 0
    for member in update.message.new_chat_members:
        if member.is_bot and member.id == context.bot.id:
            continue
        text = fmt_welcome(cfg["welcome"] or DEFAULT_WELCOME, member, chat, count)
        await safe_send(context.bot, chat.id, text)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    cfg = db.get_chat(chat.id)
    left = update.message.left_chat_member
    if not cfg or not cfg["goodbye_on"] or left.id == context.bot.id:
        return
    text = fmt_welcome(cfg["goodbye"] or DEFAULT_GOODBYE, left, chat, 0)
    await safe_send(context.bot, chat.id, text)


@group_admin
async def set_welcome(update, context):
    text = arg_text(update)
    if not text:
        return await update.message.reply_text("Use: /setwelcome Welcome {mention} to {group}!")
    db.set_chat(update.effective_chat.id, "welcome", text)
    db.set_chat(update.effective_chat.id, "welcome_on", 1)
    await update.message.reply_text("✅ Welcome message set ho gaya!")


@group_admin
async def set_goodbye(update, context):
    text = arg_text(update)
    if not text:
        return await update.message.reply_text("Use: /setgoodbye {fullname} left the group")
    db.set_chat(update.effective_chat.id, "goodbye", text)
    db.set_chat(update.effective_chat.id, "goodbye_on", 1)
    await update.message.reply_text("✅ Goodbye message set ho gaya!")


def _toggle(field, label):
    @group_admin
    async def handler(update, context):
        arg = (context.args[0].lower() if context.args else "")
        if arg not in ("on", "off"):
            return await update.message.reply_text(f"Use: /{label} on  ya  /{label} off")
        db.set_chat(update.effective_chat.id, field, 1 if arg == "on" else 0)
        await update.message.reply_text(f"✅ {label.capitalize()} {arg.upper()} kar diya.")
    return handler


@group_admin
async def show_welcome(update, context):
    cfg = db.get_chat(update.effective_chat.id)
    await update.message.reply_text(
        f"<b>Welcome ({'ON' if cfg['welcome_on'] else 'OFF'}):</b>\n<code>{esc(cfg['welcome'] or DEFAULT_WELCOME)}</code>\n\n"
        f"<b>Goodbye ({'ON' if cfg['goodbye_on'] else 'OFF'}):</b>\n<code>{esc(cfg['goodbye'] or DEFAULT_GOODBYE)}</code>",
        parse_mode=ParseMode.HTML)


@group_admin
async def reset_welcome(update, context):
    cid = update.effective_chat.id
    db.set_chat(cid, "welcome", None)
    db.set_chat(cid, "goodbye", None)
    await update.message.reply_text("♻️ Welcome/Goodbye default par reset.")


@group_admin
async def set_rules(update, context):
    text = arg_text(update)
    if not text:
        return await update.message.reply_text("Use: /setrules <rules text>")
    db.set_chat(update.effective_chat.id, "rules", text)
    await update.message.reply_text("✅ Rules save ho gaye.")


async def rules_cmd(update, context):
    cfg = db.get_chat(update.effective_chat.id)
    if not cfg or not cfg["rules"]:
        return await update.message.reply_text("Abhi koi rules set nahi hain.")
    await safe_send(context.bot, update.effective_chat.id, "<b>📜 Rules</b>\n\n" + cfg["rules"])


# ----------------------------- bot added / removed -----------------------------
async def on_my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.my_chat_member
    chat = ev.chat
    if chat.type == ChatType.PRIVATE:
        if ev.new_chat_member.status == ChatMemberStatus.BANNED:
            pass
        return
    new = ev.new_chat_member.status
    if new in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        db.upsert_chat(chat.id, chat.title, chat.type)
        if OWNER:
            await safe_send(context.bot, OWNER,
                            f"➕ Bot add hua: <b>{esc(chat.title)}</b>\nID: <code>{chat.id}</code>\nType: {chat.type}\n"
                            f"By: {esc(ev.from_user.full_name)} (<code>{ev.from_user.id}</code>)")
        if chat.type != ChatType.CHANNEL:
            await safe_send(context.bot, chat.id,
                            "👋 Thanks for adding me! Mujhe <b>admin</b> banao taaki main sab features chala sakun.\n"
                            "Welcome set karne ke liye: /setwelcome — Help: /help")
    elif new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        db.set_chat(chat.id, "active", 0)


# ----------------------------- moderation -----------------------------
async def _need_target(update, name):
    await update.message.reply_text(f"Use: reply karke /{name} ya /{name} <user_id>")


@group_admin
async def ban(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "ban")
    if await is_admin(context, update.effective_chat.id, uid):
        return await update.message.reply_text("❌ Admin ko ban nahi kar sakta.")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text(f"🔨 {esc(name)} ban ho gaya.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Error: {e.message}")


@group_admin
async def unban(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "unban")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        await update.message.reply_text(f"✅ {esc(name)} unban ho gaya.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Error: {e.message}")


@group_admin
async def kick(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "kick")
    if await is_admin(context, update.effective_chat.id, uid):
        return await update.message.reply_text("❌ Admin ko kick nahi kar sakta.")
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text(f"👢 {esc(name)} ko kick kar diya.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Error: {e.message}")


@group_admin
async def mute(update, context):
    uid, name, rest = await get_target(update, context)
    if not uid:
        return await _need_target(update, "mute")
    if await is_admin(context, update.effective_chat.id, uid):
        return await update.message.reply_text("❌ Admin ko mute nahi kar sakta.")
    secs = parse_duration(rest[0]) if rest else None
    until = None
    if secs:
        import time
        until = int(time.time()) + secs
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid,
                                               ChatPermissions(can_send_messages=False), until_date=until)
        await update.message.reply_text(
            f"🔇 {esc(name)} mute ({rest[0] if secs else 'permanent'}).", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Error: {e.message}")


@group_admin
async def unmute(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "unmute")
    perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                            can_add_web_page_previews=True, can_invite_users=True)
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, uid, perms)
        await update.message.reply_text(f"🔊 {esc(name)} unmute ho gaya.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Error: {e.message}")


async def _apply_warn(context, chat_id, uid, name, reason=""):
    cfg = db.get_chat(chat_id)
    limit = cfg["warn_limit"] if cfg else 3
    count = db.add_warn(chat_id, uid)
    text = f"⚠️ {esc(name)} ko warning ({count}/{limit}). {esc(reason)}"
    if count >= limit:
        try:
            await context.bot.ban_chat_member(chat_id, uid)
            db.reset_warns(chat_id, uid)
            text += "\n🔨 Limit poori – ban kar diya."
        except TelegramError:
            text += "\n(Ban nahi kar paya – mujhe admin rights do.)"
    await safe_send(context.bot, chat_id, text)


@group_admin
async def warn(update, context):
    uid, name, rest = await get_target(update, context)
    if not uid:
        return await _need_target(update, "warn")
    if await is_admin(context, update.effective_chat.id, uid):
        return await update.message.reply_text("❌ Admin ko warn nahi kar sakta.")
    await _apply_warn(context, update.effective_chat.id, uid, name, " ".join(rest))


@group_admin
async def warns(update, context):
    uid, name, _ = await get_target(update, context)
    uid = uid or update.effective_user.id
    cfg = db.get_chat(update.effective_chat.id)
    await update.message.reply_text(f"⚠️ Warns: {db.get_warns(update.effective_chat.id, uid)}/{cfg['warn_limit']}")


@group_admin
async def resetwarns(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "resetwarns")
    db.reset_warns(update.effective_chat.id, uid)
    await update.message.reply_text("✅ Warns reset.")


@group_admin
async def setwarnlimit(update, context):
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 20:
        return await update.message.reply_text("Use: /setwarnlimit 3  (1-20)")
    db.set_chat(update.effective_chat.id, "warn_limit", int(context.args[0]))
    await update.message.reply_text("✅ Warn limit set.")


@group_admin
async def delete_cmd(update, context):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Jis message ko delete karna hai us par reply karo.")
    try:
        await r.delete()
        await update.message.delete()
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e.message}")


@group_admin
async def pin(update, context):
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Pin karne ke liye message par reply karo.")
    try:
        await r.pin()
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e.message}")


@group_admin
async def unpin(update, context):
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        await update.message.reply_text("📌 Unpinned.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e.message}")


@group_admin
async def promote(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "promote")
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, uid, can_delete_messages=True, can_restrict_members=True,
            can_pin_messages=True, can_invite_users=True, can_manage_chat=True)
        await update.message.reply_text(f"⭐ {esc(name)} ab admin hai.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e.message}")


@group_admin
async def demote(update, context):
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "demote")
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, uid, can_delete_messages=False, can_restrict_members=False,
            can_pin_messages=False, can_invite_users=False, can_manage_chat=False,
            can_promote_members=False, can_change_info=False)
        await update.message.reply_text(f"⬇️ {esc(name)} ab admin nahi hai.", parse_mode=ParseMode.HTML)
    except TelegramError as e:
        await update.message.reply_text(f"❌ {e.message}")


antilink_toggle = _toggle("antilink", "antilink")


async def antilink_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.message
    cfg = db.get_chat(chat.id)
    if not cfg or not cfg["antilink"] or not user:
        return
    if await is_admin(context, chat.id, user.id):
        return
    try:
        await msg.delete()
    except TelegramError:
        return
    await _apply_warn(context, chat.id, user.id, user.full_name, "(link allowed nahi)")


# ----------------------------- voting -----------------------------
def poll_markup_text(poll_id):
    poll = db.one("SELECT * FROM polls WHERE id=?", (poll_id,))
    rows = db.standings(poll_id)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🗳 <b>{esc(poll['title'])}</b>", ""]
    if not rows:
        lines.append("Abhi koi candidate nahi hai. Admin /candidate se add kare.")
    for i, r in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else '▫️'} {esc(r['name'])} — <b>{r['total']}</b> votes")
    lines.append("\n" + ("✅ Vote karne ke liye neeche button dabao (ek hi vote)." if poll["active"] else "🏁 Election band ho chuka hai."))
    kb = None
    if poll["active"] and rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗳 {r['name']} ({r['total']})",
                                    callback_data=f"v:{poll_id}:{r['user_id']}")] for r in rows])
    return "\n".join(lines), kb


async def refresh_poll(context, poll_id):
    poll = db.one("SELECT * FROM polls WHERE id=?", (poll_id,))
    if not poll or not poll["message_id"]:
        return
    text, kb = poll_markup_text(poll_id)
    try:
        await context.bot.edit_message_text(text, poll["chat_id"], poll["message_id"],
                                            parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest:
        pass


@group_admin
async def election(update, context):
    chat = update.effective_chat
    title = arg_text(update)
    if not title:
        return await update.message.reply_text("Use: /election Best Admin 2026")
    if db.active_poll(chat.id):
        return await update.message.reply_text("⚠️ Pehle se ek election chal raha hai. /endelection se band karo.")
    pid = db.run("INSERT INTO polls(chat_id,title,created_at) VALUES(?,?,strftime('%s','now'))", (chat.id, title))
    text, kb = poll_markup_text(pid)
    m = await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    db.run("UPDATE polls SET message_id=? WHERE id=?", (m.message_id, pid))
    await update.message.reply_text("✅ Election start! Ab /candidate se candidates add karo (reply karke).")


@group_admin
async def candidate(update, context):
    poll = db.active_poll(update.effective_chat.id)
    if not poll:
        return await update.message.reply_text("Pehle /election <title> se vote start karo.")
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "candidate")
    db.run("INSERT OR IGNORE INTO candidates(poll_id,user_id,name) VALUES(?,?,?)", (poll["id"], uid, name))
    await refresh_poll(context, poll["id"])
    await update.message.reply_text(f"✅ {esc(name)} candidate ban gaya.", parse_mode=ParseMode.HTML)


@group_admin
async def rmcandidate(update, context):
    poll = db.active_poll(update.effective_chat.id)
    if not poll:
        return await update.message.reply_text("Koi active election nahi.")
    uid, name, _ = await get_target(update, context)
    if not uid:
        return await _need_target(update, "rmcandidate")
    db.run("DELETE FROM candidates WHERE poll_id=? AND user_id=?", (poll["id"], uid))
    db.run("DELETE FROM votes WHERE poll_id=? AND candidate_id=?", (poll["id"], uid))
    await refresh_poll(context, poll["id"])
    await update.message.reply_text("🗑 Candidate hata diya.")


@group_admin
async def addvotes(update, context):
    poll = db.active_poll(update.effective_chat.id)
    if not poll:
        return await update.message.reply_text("Koi active election nahi.")
    uid, name, rest = await get_target(update, context)
    if not uid or not rest or not re.fullmatch(r"-?\d+", rest[0]):
        return await update.message.reply_text("Use: candidate ke message par reply karke /addvotes 50")
    n = int(rest[0])
    if not db.one("SELECT 1 FROM candidates WHERE poll_id=? AND user_id=?", (poll["id"], uid)):
        return await update.message.reply_text("❌ Ye user candidate nahi hai.")
    db.run("UPDATE candidates SET bonus=bonus+? WHERE poll_id=? AND user_id=?", (n, poll["id"], uid))
    await refresh_poll(context, poll["id"])
    await update.message.reply_text(f"✅ {esc(name)} ke votes {n:+d} kar diye.", parse_mode=ParseMode.HTML)


async def results(update, context):
    poll = db.one("SELECT * FROM polls WHERE chat_id=? ORDER BY id DESC LIMIT 1", (update.effective_chat.id,))
    if not poll:
        return await update.message.reply_text("Koi election nahi mila.")
    text, _ = poll_markup_text(poll["id"])
    await safe_send(context.bot, update.effective_chat.id, text)


@group_admin
async def endelection(update, context):
    poll = db.active_poll(update.effective_chat.id)
    if not poll:
        return await update.message.reply_text("Koi active election nahi.")
    db.run("UPDATE polls SET active=0 WHERE id=?", (poll["id"],))
    await refresh_poll(context, poll["id"])
    rows = db.standings(poll["id"])
    msg = "🏁 Election khatam!"
    if rows:
        msg += f"\n🏆 Winner: <b>{esc(rows[0]['name'])}</b> ({rows[0]['total']} votes)"
    await safe_send(context.bot, update.effective_chat.id, msg)


async def vote_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, pid, cid = q.data.split(":")
    pid, cid = int(pid), int(cid)
    poll = db.one("SELECT * FROM polls WHERE id=?", (pid,))
    if not poll or not poll["active"]:
        return await q.answer("Ye election band ho chuka hai.", show_alert=True)
    voter = q.from_user.id
    prev = db.one("SELECT candidate_id FROM votes WHERE poll_id=? AND voter_id=?", (pid, voter))
    if prev and prev["candidate_id"] == cid:
        return await q.answer("Aap pehle hi isko vote de chuke ho.")
    db.run("INSERT OR REPLACE INTO votes(poll_id,voter_id,candidate_id) VALUES(?,?,?)", (pid, voter, cid))
    await q.answer("✅ Aapka vote mil gaya!" if not prev else "🔁 Vote change ho gaya!")
    await refresh_poll(context, pid)


# ----------------------------- owner panel -----------------------------
@owner_only
async def stats(update, context):
    users = db.one("SELECT COUNT(*) c FROM users")["c"]
    groups = db.one("SELECT COUNT(*) c FROM chats WHERE active=1 AND type IN ('group','supergroup')")["c"]
    chans = db.one("SELECT COUNT(*) c FROM chats WHERE active=1 AND type='channel'")["c"]
    polls = db.one("SELECT COUNT(*) c FROM polls")["c"]
    await update.message.reply_text(f"📊 Users: {users}\n👥 Groups: {groups}\n📢 Channels: {chans}\n🗳 Elections: {polls}")


@owner_only
async def chats_cmd(update, context):
    rows = db.all_("SELECT * FROM chats WHERE active=1 ORDER BY added_at DESC LIMIT 60")
    if not rows:
        return await update.message.reply_text("Koi chat nahi.")
    text = "\n".join(f"• {esc(r['title'])} [{r['type']}] <code>{r['chat_id']}</code>" for r in rows)
    await safe_send(context.bot, update.effective_chat.id, "<b>💬 Chats</b>\n" + text)


@owner_only
async def say(update, context):
    parts = (update.message.text or "").split(None, 2)
    if len(parts) < 3:
        return await update.message.reply_text("Use: /say <chat_id> <text>")
    try:
        await safe_send(context.bot, int(parts[1]), parts[2])
        await update.message.reply_text("✅ Bhej diya.")
    except (ValueError, TelegramError) as e:
        await update.message.reply_text(f"❌ {e}")


@owner_only
async def replyto(update, context):
    parts = (update.message.text or "").split(None, 3)
    if len(parts) < 4:
        return await update.message.reply_text("Use: /replyto <chat_id> <message_id> <text>")
    try:
        await safe_send(context.bot, int(parts[1]), parts[3], reply_to_message_id=int(parts[2]))
        await update.message.reply_text("✅ Reply bhej diya.")
    except (ValueError, TelegramError) as e:
        await update.message.reply_text(f"❌ {e}")


@owner_only
async def broadcast(update, context):
    msg = update.message
    text = arg_text(update)
    src = msg.reply_to_message
    if not text and not src:
        return await msg.reply_text("Use: /broadcast <text>  ya kisi message par reply karke /broadcast")
    targets = [r["user_id"] for r in db.all_("SELECT user_id FROM users WHERE banned=0")]
    targets += [r["chat_id"] for r in db.all_("SELECT chat_id FROM chats WHERE active=1")]
    ok = fail = 0
    status = await msg.reply_text(f"📣 {len(targets)} jagah bhej raha hoon...")
    for t in targets:
        try:
            if src:
                await src.copy(t)
            else:
                await safe_send(context.bot, t, text)
            ok += 1
        except Forbidden:
            fail += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Done. Success: {ok} | Failed: {fail}")


@owner_only
async def banuser(update, context):
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /banuser <user_id>")
    db.run("INSERT INTO users(user_id,banned) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET banned=1",
           (int(context.args[0]),))
    await update.message.reply_text("🚫 User bot se block.")


@owner_only
async def unbanuser(update, context):
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Use: /unbanuser <user_id>")
    db.run("UPDATE users SET banned=0 WHERE user_id=?", (int(context.args[0]),))
    await update.message.reply_text("✅ Unblock ho gaya.")


@owner_only
async def leave(update, context):
    if not context.args:
        return await update.message.reply_text("Use: /leave <chat_id>")
    try:
        await context.bot.leave_chat(int(context.args[0]))
        db.set_chat(int(context.args[0]), "active", 0)
        await update.message.reply_text("👋 Chat chhod di.")
    except (ValueError, TelegramError) as e:
        await update.message.reply_text(f"❌ {e}")


# ----------------------------- private relay -----------------------------
async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    db.upsert_user(user)
    if is_owner(user.id):
        r = msg.reply_to_message
        row = db.one("SELECT user_id FROM relay WHERE owner_msg_id=?", (r.message_id,)) if r else None
        if row:
            try:
                await msg.copy(row["user_id"])
                await msg.reply_text("✅ User ko bhej diya.")
            except TelegramError as e:
                await msg.reply_text(f"❌ {e.message}")
        else:
            await msg.reply_text("ℹ️ Kisi user ke forwarded message par reply karo, ya /help dekho.")
        return
    if db.is_bot_banned(user.id):
        return
    if OWNER is None:
        return
    try:
        head = await context.bot.send_message(
            OWNER, f"📩 <b>{esc(user.full_name)}</b> (<code>{user.id}</code>) "
                   f"{'@' + user.username if user.username else ''}", parse_mode=ParseMode.HTML)
        copy = await msg.copy(OWNER)
        for mid in (head.message_id, copy.message_id):
            db.run("INSERT OR REPLACE INTO relay(owner_msg_id,user_id) VALUES(?,?)", (mid, user.id))
        await msg.reply_text("✅ Aapka message admin ko bhej diya gaya hai.")
    except TelegramError:
        await msg.reply_text("❌ Abhi message nahi bhej paya, baad me try karo.")


# ----------------------------- errors / health / main -----------------------------
async def error_handler(update, context):
    log.error("Error: %s", context.error, exc_info=context.error)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health server on port %s", port)


async def post_init(app: Application):
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(aid, f"✅ @{me.username} online ho gaya!")
        except TelegramError:
            pass


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN set nahi hai (.env ya Render Environment me daalo).")
    start_health_server()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    cmds = {
        "start": start, "help": help_cmd, "id": id_cmd, "info": info_cmd, "rules": rules_cmd,
        "setwelcome": set_welcome, "setgoodbye": set_goodbye,
        "welcome": _toggle("welcome_on", "welcome"), "goodbye": _toggle("goodbye_on", "goodbye"),
        "showwelcome": show_welcome, "resetwelcome": reset_welcome, "setrules": set_rules,
        "ban": ban, "unban": unban, "kick": kick, "mute": mute, "unmute": unmute,
        "warn": warn, "warns": warns, "resetwarns": resetwarns, "setwarnlimit": setwarnlimit,
        "del": delete_cmd, "pin": pin, "unpin": unpin, "promote": promote, "demote": demote,
        "antilink": antilink_toggle,
        "election": election, "candidate": candidate, "rmcandidate": rmcandidate,
        "addvotes": addvotes, "results": results, "endelection": endelection,
        "stats": stats, "chats": chats_cmd, "say": say, "replyto": replyto,
        "broadcast": broadcast, "banuser": banuser, "unbanuser": unbanuser, "leave": leave,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(vote_cb, pattern=r"^v:\d+:-?\d+$"))
    app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    from telegram.ext import ChatMemberHandler
    app.add_handler(ChatMemberHandler(on_my_status, ChatMemberHandler.MY_CHAT_MEMBER))
    link_filter = filters.ChatType.GROUPS & (
        filters.Entity(MessageEntity.URL) | filters.Entity(MessageEntity.TEXT_LINK)
        | filters.CaptionEntity(MessageEntity.URL) | filters.CaptionEntity(MessageEntity.TEXT_LINK))
    app.add_handler(MessageHandler(link_filter & ~filters.COMMAND, antilink_filter), group=1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
