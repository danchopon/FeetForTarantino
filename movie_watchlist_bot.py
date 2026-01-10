#!/usr/bin/env python3
"""
Telegram Movie Watchlist Bot
With PostgreSQL storage for persistence.
"""

import os
import random
import logging
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_db_connection():
    """Get database connection from DATABASE_URL."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def init_db():
    """Initialize database tables."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            title VARCHAR(255) NOT NULL,
            status VARCHAR(20) DEFAULT 'to_watch',
            added_by VARCHAR(100),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            watched_by VARCHAR(100),
            watched_at TIMESTAMP,
            UNIQUE(chat_id, LOWER(title))
        )
    """)
    
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_movies_chat_status 
        ON movies(chat_id, status)
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database initialized")


def add_movie_db(chat_id: int, title: str, added_by: str) -> tuple[bool, str]:
    """Add movie to database. Returns (success, message)."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(
            """INSERT INTO movies (chat_id, title, added_by) 
               VALUES (%s, %s, %s)""",
            (chat_id, title, added_by)
        )
        conn.commit()
        return True, "added"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        # Check where the movie is
        cur.execute(
            "SELECT status FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)",
            (chat_id, title)
        )
        row = cur.fetchone()
        return False, row["status"] if row else "exists"
    finally:
        cur.close()
        conn.close()


def mark_watched_db(chat_id: int, search: str, watched_by: str) -> tuple[bool, str | None]:
    """Mark movie as watched. Returns (success, movie_title)."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Find movie (exact match first, then partial)
    cur.execute(
        """SELECT id, title, status FROM movies 
           WHERE chat_id = %s AND LOWER(title) = LOWER(%s)""",
        (chat_id, search)
    )
    row = cur.fetchone()
    
    if not row:
        cur.execute(
            """SELECT id, title, status FROM movies 
               WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s) AND status = 'to_watch'
               LIMIT 1""",
            (chat_id, f"%{search}%")
        )
        row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return False, None
    
    if row["status"] == "watched":
        cur.close()
        conn.close()
        return False, row["title"]
    
    cur.execute(
        """UPDATE movies 
           SET status = 'watched', watched_by = %s, watched_at = %s
           WHERE id = %s""",
        (watched_by, datetime.now(), row["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def remove_movie_db(chat_id: int, search: str) -> str | None:
    """Remove movie. Returns title if found."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Find movie
    cur.execute(
        """SELECT id, title FROM movies 
           WHERE chat_id = %s AND LOWER(title) = LOWER(%s)""",
        (chat_id, search)
    )
    row = cur.fetchone()
    
    if not row:
        cur.execute(
            """SELECT id, title FROM movies 
               WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s)
               LIMIT 1""",
            (chat_id, f"%{search}%")
        )
        row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return None
    
    cur.execute("DELETE FROM movies WHERE id = %s", (row["id"],))
    conn.commit()
    cur.close()
    conn.close()
    return row["title"]


def get_movies_db(chat_id: int, status: str | None = None) -> list[dict]:
    """Get movies for chat, optionally filtered by status."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    if status:
        cur.execute(
            "SELECT * FROM movies WHERE chat_id = %s AND status = %s ORDER BY added_at",
            (chat_id, status)
        )
    else:
        cur.execute(
            "SELECT * FROM movies WHERE chat_id = %s ORDER BY status DESC, added_at",
            (chat_id,)
        )
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_counts_db(chat_id: int) -> dict:
    """Get movie counts by status."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        """SELECT status, COUNT(*) as count 
           FROM movies WHERE chat_id = %s GROUP BY status""",
        (chat_id,)
    )
    
    counts = {"to_watch": 0, "watched": 0}
    for row in cur.fetchall():
        counts[row["status"]] = row["count"]
    
    cur.close()
    conn.close()
    return counts


# === Bot Commands ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message."""
    welcome_text = """
🎬 *Movie Watchlist Bot*

*Команды:*
`/add название` — добавить фильм в список
`/watched название` — отметить как просмотренный
`/remove название` — удалить фильм
`/list` — показать все фильмы
`/random` — случайный фильм
`/poll N` — голосование (N = 1-10 фильмов)

*Примеры:*
`/add Inception`
`/watched Inception`
`/poll 3`
"""
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a movie to to_watch list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/add Inception`", parse_mode="Markdown")
        return

    movie_title = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    added_by = update.effective_user.first_name

    success, status = add_movie_db(chat_id, movie_title, added_by)
    
    if success:
        counts = get_counts_db(chat_id)
        await update.message.reply_text(
            f"✅ *{movie_title}* добавлен в список\n📋 Всего к просмотру: {counts['to_watch']}",
            parse_mode="Markdown"
        )
    else:
        status_text = "к просмотру" if status == "to_watch" else "просмотренных"
        await update.message.reply_text(
            f"⚠️ *{movie_title}* уже в списке ({status_text})!",
            parse_mode="Markdown"
        )


async def mark_watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move a movie to watched list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/watched Inception`", parse_mode="Markdown")
        return

    search = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    watched_by = update.effective_user.first_name

    success, title = mark_watched_db(chat_id, search, watched_by)
    
    if success:
        counts = get_counts_db(chat_id)
        await update.message.reply_text(
            f"✅ *{title}* просмотрен!\n"
            f"📋 Осталось: {counts['to_watch']} | ✅ Просмотрено: {counts['watched']}",
            parse_mode="Markdown"
        )
    elif title:
        await update.message.reply_text(f"ℹ️ *{title}* уже просмотрен", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")


async def remove_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a movie from any list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/remove Inception`", parse_mode="Markdown")
        return

    search = " ".join(context.args).strip()
    chat_id = update.effective_chat.id

    title = remove_movie_db(chat_id, search)
    
    if title:
        await update.message.reply_text(f"🗑️ *{title}* удалён", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all movies."""
    chat_id = update.effective_chat.id
    movies = get_movies_db(chat_id)

    parts = ["🎬 *Список фильмов*\n"]

    to_watch = [m for m in movies if m["status"] == "to_watch"]
    watched = [m for m in movies if m["status"] == "watched"]

    parts.append(f"📋 *К просмотру ({len(to_watch)}):*")
    if to_watch:
        for i, movie in enumerate(to_watch, 1):
            parts.append(f"{i}. {movie['title']}")
    else:
        parts.append("_пусто_")

    parts.append("")

    parts.append(f"✅ *Просмотрено ({len(watched)}):*")
    if watched:
        for i, movie in enumerate(watched, 1):
            parts.append(f"{i}. {movie['title']}")
    else:
        parts.append("_пусто_")

    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def random_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a random movie from to_watch."""
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")

    if not to_watch:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return

    chosen = random.choice(to_watch)
    await update.message.reply_text(f"🎲 *{chosen['title']}*", parse_mode="Markdown")


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a poll with N random movies."""
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")

    if not to_watch:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return

    num = 3
    if context.args:
        try:
            num = int(context.args[0])
            num = max(1, min(10, num))
        except ValueError:
            pass

    if len(to_watch) < num:
        num = len(to_watch)

    if num < 2:
        chosen = random.choice(to_watch)
        await update.message.reply_text(
            f"🎬 Только один вариант:\n*{chosen['title']}*",
            parse_mode="Markdown"
        )
        return

    chosen = random.sample(to_watch, num)
    options = [movie["title"][:100] for movie in chosen]

    await update.message.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


def main() -> None:
    """Run the bot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return

    # Initialize database
    init_db()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_movie))
    application.add_handler(CommandHandler("watched", mark_watched))
    application.add_handler(CommandHandler("remove", remove_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("random", random_movie))
    application.add_handler(CommandHandler("poll", create_poll))

    print("🎬 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
