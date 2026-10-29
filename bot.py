import sqlite3
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# Bot Token aur Admin ID yahan dalein
TOKEN = "9113864833:AAFIVUXUCS4Rv-3Yitl6KRd_aTCApbY3BC0"
ADMIN_ID = 8680144387 # Apna Telegram Numeric User ID yahan dalein

bot = telebot.TeleBot(TOKEN)

# Database Setup
conn = sqlite3.connect("bot_database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS votes (
    poll_id TEXT,
    user_id INTEGER,
    username TEXT,
    vote_count INTEGER,
    PRIMARY KEY (poll_id, user_id)
)
"""
)
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS bot_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT
)
"""
)
conn.commit()


@bot.message_handler(commands=["start"])
def send_welcome(message):
  if message.chat.type == "private":
    cursor.execute(
        "INSERT OR IGNORE INTO bot_users (user_id, username) VALUES (?, ?)",
        (message.from_user.id, message.from_user.username),
    )
    conn.commit()

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "➕ Bot ko Group me Jodein",
            url=f"https://t.me/{bot.get_me().username}?startgroup=true",
        )
    )

    bot.send_message(
        message.chat.id,
        f"👋 **Namaste {message.from_user.first_name}!**\n\nMain ek active bot hoon. Mujhe apne group me add karein ya koi message bhejein jo seedha admin ke paas chala jayega.",
        reply_markup=markup,
        parse_mode="Markdown",
    )


# Welcome Message for Groups
@bot.message_handler(content_types=["new_chat_members"])
def welcome_new_member(message):
  for member in message.new_chat_members:
    if member.id != bot.get_me().id:
      bot.send_message(
          message.chat.id,
          f"🎉 **Welcome {member.first_name}!** Hamare group me aapka swagat hai.",
          parse_mode="Markdown",
      )


# Admin Forwarding & Chat
@bot.message_handler(
    func=lambda message: message.chat.type == "private"
    and message.from_user.id != ADMIN_ID
)
def user_to_admin(message):
  user = message.from_user
  try:
    bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    bot.send_message(
        ADMIN_ID,
        f"👤 **New Message User Se:**\nNaam: {user.first_name}\nID: `{user.id}`",
        parse_mode="Markdown",
    )
    bot.reply_to(message, "📩 Aapka message admin ke paas bhej diya gaya hai!")
  except Exception as e:
    print(e)


@bot.message_handler(
    func=lambda message: message.chat.type == "private"
    and message.from_user.id == ADMIN_ID
)
def admin_reply(message):
  if message.reply_to_message and message.reply_to_message.forward_from:
    user_id = message.reply_to_message.forward_from.id
    try:
      bot.send_message(user_id, f"💬 **Admin ka Jawab:**\n\n{message.text}")
      bot.reply_to(message, "✅ Jawab bhej diya gaya hai!")
    except Exception as e:
      bot.reply_to(message, f"❌ Error: {e}")


# Vote System & Admin Vote Control
@bot.message_handler(commands=["vote"])
def vote_cmd(message):
  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(message, "⚠️ Use karein: `/vote <poll_id>`", parse_mode="Markdown")
    return

  poll_id = args[1]
  user = message.from_user
  cursor.execute(
      "SELECT vote_count FROM votes WHERE poll_id = ? AND user_id = ?",
      (poll_id, user.id),
  )
  res = cursor.fetchone()

  if res:
    cursor.execute(
        "UPDATE votes SET vote_count = vote_count + 1 WHERE poll_id = ? AND"
        " user_id = ?",
        (poll_id, user.id),
    )
  else:
    cursor.execute(
        "INSERT INTO votes (poll_id, user_id, username, vote_count) VALUES"
        " (?, ?, ?, 1)",
        (poll_id, user.id, user.first_name),
    )
  conn.commit()
  bot.reply_to(message, f"✅ Aapka vote darj ho gaya hai (Poll: {poll_id})!")


@bot.message_handler(commands=["addvote"])
def admin_add_vote(message):
  if message.from_user.id != ADMIN_ID:
    return
  args = message.text.split()
  if len(args) < 4:
    bot.reply_to(
        message,
        "⚠️ Use karein: `/addvote <poll_id> <user_id> <count>`",
        parse_mode="Markdown",
    )
    return

  poll_id, target_user, count = args[1], int(args[2]), int(args[3])
  cursor.execute(
      "SELECT vote_count FROM votes WHERE poll_id = ? AND user_id = ?",
      (poll_id, target_user),
  )
  res = cursor.fetchone()

  if res:
    cursor.execute(
        "UPDATE votes SET vote_count = vote_count + ? WHERE poll_id = ? AND"
        " user_id = ?",
        (count, poll_id, target_user),
    )
  else:
    cursor.execute(
        "INSERT INTO votes (poll_id, user_id, username, vote_count) VALUES"
        " (?, ?, 'Manual', ?)",
        (poll_id, target_user, count),
    )
  conn.commit()
  bot.reply_to(
      message,
      f"✅ User `{target_user}` ke votes me `{count}` add kar diye gaye hain!",
      parse_mode="Markdown",
  )


@bot.message_handler(commands=["leaderboard"])
def leaderboard_cmd(message):
  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(
        message, "⚠️ Use karein: `/leaderboard <poll_id>`", parse_mode="Markdown"
    )
    return

  poll_id = args[1]
  cursor.execute(
      "SELECT username, vote_count FROM votes WHERE poll_id = ? ORDER BY"
      " vote_count DESC LIMIT 10",
      (poll_id,),
  )
  rows = cursor.fetchall()
  if not rows:
    bot.reply_to(message, "📭 Abhi koi votes nahi hain.")
    return

  text = f"🏆 **Leaderboard ({poll_id})**:\n\n"
  for i, r in enumerate(rows, 1):
    text += f"{i}. {r[0]} — ⭐ {r[1]} Votes\n"
  bot.send_message(message.chat.id, text, parse_mode="Markdown")


if __name__ == "__main__":
  print("Bot is running...")
  bot.infinity_polling()
  
