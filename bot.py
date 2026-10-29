import sqlite3
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# ================= CONFIGURATION =================
TOKEN = "9240925761:AAH2xgqZxQqo8Ray7Hi3IE2VadVrHY55EQg"  # Apna BotFather ka token yahan dalein
ADMIN_ID = 8680144387  # Apna Telegram Numeric User ID yahan dalein

bot = telebot.TeleBot(TOKEN)

# ================= DATABASE SETUP =================
conn = sqlite3.connect("advanced_bot.db", check_same_thread=False)
cursor = conn.cursor()

# 1. Custom Welcomes Table
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS group_welcomes (
    chat_id INTEGER PRIMARY KEY,
    welcome_text TEXT
)
"""
)

# 2. Link Blocker Status Table
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS link_blocker (
    chat_id INTEGER PRIMARY KEY,
    status TEXT
)
"""
)

# 3. File Hosting / File Store Table
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS hosted_files (
    file_code INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT,
    file_type TEXT,
    file_name TEXT
)
"""
)

# 4. Users Table for Broadcast
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT
)
"""
)
conn.commit()


# ================= HELPER: CHECK ADMIN =================
def is_admin(chat_id, user_id):
  if user_id == ADMIN_ID:
    return True
  try:
    member = bot.get_chat_member(chat_id, user_id)
    return member.status in ["creator", "administrator"]
  except:
    return False


# ================= START & HELP COMMANDS =================
@bot.message_handler(commands=["start"])
def send_start(message):
  user = message.from_user
  cursor.execute(
      "INSERT OR IGNORE INTO bot_users (user_id, username) VALUES (?, ?)",
      (user.id, user.username or user.first_name),
  )
  conn.commit()

  # Agar start ke sath koi file code aaya ho (File hosting download link)
  if " " in message.text:
    file_code = message.text.split()[1]
    cursor.execute(
        "SELECT file_id, file_type FROM hosted_files WHERE file_code = ?",
        (file_code,),
    )
    res = cursor.fetchone()
    if res:
      f_id, f_type = res[0], res[1]
      if f_type == "document":
        bot.send_document(message.chat.id, f_id, caption="📁 Hosted File")
      elif f_type == "photo":
        bot.send_photo(message.chat.id, f_id, caption="🖼️ Hosted Photo")
      elif f_type == "video":
        bot.send_video(message.chat.id, f_id, caption="🎬 Hosted Video")
      return
    else:
      bot.reply_to(message, "❌ Yeh file nahi mili ya link invalid hai.")
      return

  markup = InlineKeyboardMarkup()
  markup.add(
      InlineKeyboardButton(
          "➕ Bot ko Group me Jodein",
          url=f"https://t.me/{bot.get_me().username}?startgroup=true",
      )
  )

  text = (
      f"👋 **Namaste {user.first_name}!**\n\n"
      "Main ek advanced bot hoon jo yeh sab kaam kar sakta hai:\n"
      "• ⚙️ **Custom Welcome Message** (Group me set karein)\n"
      "• 🚫 **Link Blocker** (Spam links delete karega)\n"
      "• 📁 **File Hosting** (Mujhe file bhejein, link banayein)\n"
      "• 📢 **Admin & Broadcast Controls**\n\n"
      "Bot ko group me add karke `/setwelcome` use karein!"
  )
  bot.send_message(
      message.chat.id, text, reply_markup=markup, parse_mode="Markdown"
  )


# ================= 1. CUSTOM WELCOME SYSTEM =================
@bot.message_handler(commands=["setwelcome"])
def set_welcome_message(message):
  if message.chat.type == "private":
    bot.reply_to(
        message, "⚠️ Yeh command sirf Groups ya Channels ke andar kaam karegi!"
    )
    return

  if not is_admin(message.chat.id, message.from_user.id):
    bot.reply_to(
        message, "❌ Yeh command sirf Group Admin use kar sakte hain!"
    )
    return

  text = message.text.replace("/setwelcome", "").strip()
  if not text:
    bot.reply_to(
        message,
        "⚠️ Sahi format use karein:\n`/setwelcome Hello {name}, welcome to"
        " {group}!`",
        parse_mode="Markdown",
    )
    return

  cursor.execute(
      "INSERT OR REPLACE INTO group_welcomes (chat_id, welcome_text) VALUES"
      " (?, ?)",
      (message.chat.id, text),
  )
  conn.commit()
  bot.reply_to(
      message, "✅ Custom welcome message safalta-poorvak set ho gaya hai!"
  )


@bot.message_handler(content_types=["new_chat_members"])
def welcome_new_member(message):
  for member in message.new_chat_members:
    if member.id == bot.get_me().id:
      bot.send_message(
          message.chat.id,
          "🎉 Shukriya mujhe group me add karne ke liye! Ab yahan welcome aur"
          " security active hai.",
      )
    else:
      cursor.execute(
          "SELECT welcome_text FROM group_welcomes WHERE chat_id = ?",
          (message.chat.id,),
      )
      res = cursor.fetchone()

      if res and res[0]:
        welcome_msg = res[0].format(
            name=member.first_name, group=message.chat.title
        )
      else:
        welcome_msg = (
            f"✨ **Welcome {member.first_name}** to **{message.chat.title}**!\n\n"
            "Kripya group ke rules ka palan karein."
        )

      bot.send_message(message.chat.id, welcome_msg, parse_mode="Markdown")


# ================= 2. LINK BLOCKER SYSTEM =================
@bot.message_handler(commands=["togglelinks"])
def toggle_link_blocker(message):
  if message.chat.type == "private":
    return
  if not is_admin(message.chat.id, message.from_user.id):
    bot.reply_to(message, "❌ Sirf admin yeh on/off kar sakte hain!")
    return

  cursor.execute(
      "SELECT status FROM link_blocker WHERE chat_id = ?", (message.chat.id,)
  )
  res = cursor.fetchone()

  if res and res[0] == "ON":
    cursor.execute(
        "UPDATE link_blocker SET status = 'OFF' WHERE chat_id = ?",
        (message.chat.id,),
    )
    status_text = "OFF ❌"
  else:
    cursor.execute(
        "INSERT OR REPLACE INTO link_blocker (chat_id, status) VALUES (?,"
        " 'ON')",
        (message.chat.id,),
    )
    status_text = "ON ✅"

  conn.commit()
  bot.reply_to(
      message, f"🛡️ Link Blocker status is now: **{status_text}**", parse_mode="Markdown"
  )


@bot.message_handler(
    func=lambda m: m.chat.type in ["group", "supergroup"]
    and (m.text or m.caption)
)
def check_links(message):
  cursor.execute(
      "SELECT status FROM link_blocker WHERE chat_id = ?", (message.chat.id,)
  )
  res = cursor.fetchone()

  if res and res[0] == "ON":
    if not is_admin(message.chat.id, message.from_user.id):
      text = message.text or message.caption
      if "http://" in text or "https://" in text or "t.me/" in text:
        try:
          bot.delete_message(message.chat.id, message.message_id)
          bot.send_message(
              message.chat.id,
              f"⚠️ <b>{message.from_user.first_name}</b>, is group me links"
              " bhejna mana hai!",
              parse_mode="HTML",
          )
        except:
          pass


# ================= 3. FILE HOSTING SYSTEM =================
@bot.message_handler(
    content_types=["document", "photo", "video"],
    func=lambda m: m.chat.type == "private",
)
def handle_file_hosting(message):
  file_id, f_type, f_name = "", "", "File"

  if message.document:
    file_id = message.document.file_id
    f_type = "document"
    f_name = message.document.file_name or "Document"
  elif message.photo:
    file_id = message.photo[-1].file_id
    f_type = "photo"
    f_name = "Photo"
  elif message.video:
    file_id = message.video.file_id
    f_type = "video"
    f_name = "Video"

  cursor.execute(
      "INSERT INTO hosted_files (file_id, file_type, file_name) VALUES (?, ?,"
      " ?)",
      (file_id, f_type, f_name),
  )
  conn.commit()
  file_code = cursor.lastrowid

  bot_username = bot.get_me().username
  share_link = f"https://t.me/{bot_username}?start={file_code}"

  bot.reply_to(
      message,
      f"📂 **File Successfully Hosted!**\n\nAapka shareable link tayar hai:\n`{share_link}`",
      parse_mode="Markdown",
  )


# ================= 4. ADMIN BROADCAST & STATS =================
@bot.message_handler(commands=["stats"])
def admin_stats(message):
  if message.from_user.id != ADMIN_ID:
    return
  cursor.execute("SELECT COUNT(*) FROM bot_users")
  users_count = cursor.fetchone()[0]
  cursor.execute("SELECT COUNT(*) FROM hosted_files")
  files_count = cursor.fetchone()[0]

  bot.reply_to(
      message,
      f"📊 **Bot Statistics:**\n\nTotal Users: `{users_count}`\nHosted"
      f" Files: `{files_count}`",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["broadcast"])
def admin_broadcast(message):
  if message.from_user.id != ADMIN_ID:
    return
  text = message.text.replace("/broadcast", "").strip()
  if not text:
    bot.reply_to(message, "⚠️ Broadcast message likhein.")
    return

  cursor.execute("SELECT user_id FROM bot_users")
  users = cursor.fetchall()
  success = 0

  for u in users:
    try:
      bot.send_message(
          u[0], f"📢 **Announcement:**\n\n{text}", parse_mode="Markdown"
      )
      success += 1
    except:
      pass

  bot.reply_to(
      message, f"✅ Broadcast sent successfully to `{success}` users."
  )


# ================= RUN BOT =================
if __name__ == "__main__":
  print("🚀 Advanced Hosting & Welcome Bot successfully start ho gaya hai...")
  bot.infinity_polling(none_stop=True)
