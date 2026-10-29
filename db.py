import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("DB_PATH", "bot.db")
_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
    welcome TEXT, goodbye TEXT,
    welcome_on INTEGER DEFAULT 1, goodbye_on INTEGER DEFAULT 0,
    rules TEXT, antilink INTEGER DEFAULT 0, warn_limit INTEGER DEFAULT 3,
    active INTEGER DEFAULT 1, added_at INTEGER);
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
    started_at INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS warns(
    chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS polls(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, title TEXT,
    active INTEGER DEFAULT 1, message_id INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS candidates(
    poll_id INTEGER, user_id INTEGER, name TEXT, bonus INTEGER DEFAULT 0,
    PRIMARY KEY(poll_id, user_id));
CREATE TABLE IF NOT EXISTS votes(
    poll_id INTEGER, voter_id INTEGER, candidate_id INTEGER,
    PRIMARY KEY(poll_id, voter_id));
CREATE TABLE IF NOT EXISTS relay(
    owner_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
"""
with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def run(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


def one(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchone()


def all_(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


# ---------- chats ----------
def upsert_chat(chat_id, title, ctype):
    run("""INSERT INTO chats(chat_id,title,type,added_at) VALUES(?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type, active=1""",
        (chat_id, title, ctype, int(time.time())))


def get_chat(chat_id):
    return one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))


def set_chat(chat_id, field, value):
    assert field in {"welcome", "goodbye", "welcome_on", "goodbye_on", "rules",
                     "antilink", "warn_limit", "active"}
    run(f"UPDATE chats SET {field}=? WHERE chat_id=?", (value, chat_id))


# ---------- users ----------
def upsert_user(u):
    run("""INSERT INTO users(user_id,first_name,username,started_at) VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username""",
        (u.id, u.first_name, u.username, int(time.time())))


def is_bot_banned(uid):
    r = one("SELECT banned FROM users WHERE user_id=?", (uid,))
    return bool(r and r["banned"])


# ---------- warns ----------
def add_warn(chat_id, uid):
    run("""INSERT INTO warns(chat_id,user_id,count) VALUES(?,?,1)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1""", (chat_id, uid))
    return one("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))["count"]


def get_warns(chat_id, uid):
    r = one("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return r["count"] if r else 0


def reset_warns(chat_id, uid):
    run("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))


# ---------- voting ----------
def active_poll(chat_id):
    return one("SELECT * FROM polls WHERE chat_id=? AND active=1 ORDER BY id DESC LIMIT 1", (chat_id,))


def standings(poll_id):
    return all_("""SELECT c.user_id, c.name, c.bonus,
        (SELECT COUNT(*) FROM votes v WHERE v.poll_id=c.poll_id AND v.candidate_id=c.user_id) AS real_votes,
        (SELECT COUNT(*) FROM votes v WHERE v.poll_id=c.poll_id AND v.candidate_id=c.user_id) + c.bonus AS total
        FROM candidates c WHERE c.poll_id=? ORDER BY total DESC, c.name""", (poll_id,))
