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
    MessageHandler,
    ContextTypes,
    filters,
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
            watched_at TIMESTAMP
        )
    """)
    
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_chat_title 
        ON movies(chat_id, LOWER(title))
    """)
    
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_movies_chat_status 
        ON movies(chat_id, status)
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vote_basket (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            user_name VARCHAR(100),
            movie_num INT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, user_id, movie_num)
        )
    """)
    
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_vote_basket_chat 
        ON vote_basket(chat_id)
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


# === Vote Basket Functions ===

def add_to_basket(chat_id: int, user_id: int, user_name: str, movie_nums: list[int]) -> tuple[list[int], list[int]]:
    """Add movies to user's basket. Returns (added, already_exists)."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    added = []
    exists = []
    
    for num in movie_nums:
        try:
            cur.execute(
                """INSERT INTO vote_basket (chat_id, user_id, user_name, movie_num)
                   VALUES (%s, %s, %s, %s)""",
                (chat_id, user_id, user_name, num)
            )
            added.append(num)
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            exists.append(num)
    
    conn.commit()
    cur.close()
    conn.close()
    return added, exists


def remove_from_basket(chat_id: int, user_id: int, movie_nums: list[int] | None = None) -> int:
    """Remove movies from user's basket. If movie_nums is None, clear all. Returns count removed."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    if movie_nums is None:
        cur.execute(
            "DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s",
            (chat_id, user_id)
        )
    else:
        cur.execute(
            "DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s AND movie_num = ANY(%s)",
            (chat_id, user_id, movie_nums)
        )
    
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def clear_basket(chat_id: int) -> int:
    """Clear entire basket for chat. Returns count removed."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("DELETE FROM vote_basket WHERE chat_id = %s", (chat_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def get_user_basket(chat_id: int, user_id: int) -> list[int]:
    """Get user's basket movie numbers."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "SELECT movie_num FROM vote_basket WHERE chat_id = %s AND user_id = %s ORDER BY movie_num",
        (chat_id, user_id)
    )
    
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums


def get_full_basket(chat_id: int) -> list[dict]:
    """Get full basket with user info."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        """SELECT user_id, user_name, movie_num 
           FROM vote_basket WHERE chat_id = %s ORDER BY user_name, movie_num""",
        (chat_id,)
    )
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_unique_basket_movies(chat_id: int) -> list[int]:
    """Get unique movie numbers from basket."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "SELECT DISTINCT movie_num FROM vote_basket WHERE chat_id = %s ORDER BY movie_num",
        (chat_id,)
    )
    
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums


# === Bot Commands ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message."""
    welcome_text = """
🎬 *Movie Watchlist Bot*

*Основные:*
`/add название` — добавить фильм
`/batch` — добавить несколько
`/watched название` — просмотрен
`/remove название` — удалить
`/list` — все фильмы

*Рандом и голосование:*
`/random` — случайный фильм
`/poll N` — poll из N случайных
`/vote 1,5,12` — poll за выбранные
`/rpoll 1,5,12` — рандом из выбранных

*Корзина голосования:*
`/v+ 1,5,12` — добавить в корзину
`/v-` — очистить свою корзину
`/v- 5` — убрать фильм из корзины
`/vmy` — моя корзина
`/vlist` — общая корзина
`/go` — запустить poll
`/vrand` — случайный из корзины
`/vc` — очистить всю корзину
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


async def batch_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add multiple movies at once."""
    text = update.message.text
    
    # Remove /batch command from text
    if text.startswith("/batch"):
        text = text[6:].strip()
    
    if not text:
        await update.message.reply_text(
            "📝 Отправь список фильмов, каждый с новой строки:\n\n"
            "`/batch\n"
            "Inception\n"
            "The Matrix\n"
            "Interstellar`",
            parse_mode="Markdown"
        )
        return
    
    # Split by newlines
    movies = [m.strip() for m in text.split("\n") if m.strip()]
    
    if not movies:
        await update.message.reply_text("❌ Не найдено фильмов для добавления")
        return
    
    chat_id = update.effective_chat.id
    added_by = update.effective_user.first_name
    
    added = []
    skipped = []
    
    for title in movies:
        success, _ = add_movie_db(chat_id, title, added_by)
        if success:
            added.append(title)
        else:
            skipped.append(title)
    
    # Build response
    parts = []
    if added:
        parts.append(f"✅ Добавлено ({len(added)}):")
        for m in added:
            parts.append(f"  • {m}")
    
    if skipped:
        parts.append(f"\n⚠️ Уже в списке ({len(skipped)}):")
        for m in skipped:
            parts.append(f"  • {m}")
    
    counts = get_counts_db(chat_id)
    parts.append(f"\n📋 Всего к просмотру: {counts['to_watch']}")
    
    await update.message.reply_text("\n".join(parts))


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

    await update.effective_chat.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def vote_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a poll with specific movies by their numbers."""
    if not context.args:
        await update.message.reply_text(
            "❌ Укажи номера фильмов:\n`/vote 1,5,12`\n\nНомера см. в /list",
            parse_mode="Markdown"
        )
        return
    
    # Parse numbers from input like "1,5,12" or "1, 5, 12" or "1 5 12"
    input_text = " ".join(context.args)
    input_text = input_text.replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Пример: `/vote 1,5,12`", parse_mode="Markdown")
        return
    
    if len(numbers) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 фильма для голосования")
        return
    
    if len(numbers) > 10:
        await update.message.reply_text("❌ Максимум 10 фильмов в опросе")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    # Get movies by numbers (1-indexed)
    selected = []
    invalid = []
    
    for num in numbers:
        if 1 <= num <= len(to_watch):
            selected.append(to_watch[num - 1])
        else:
            invalid.append(num)
    
    if invalid:
        await update.message.reply_text(
            f"❌ Неверные номера: {', '.join(map(str, invalid))}\n"
            f"Доступно: 1-{len(to_watch)}"
        )
        return
    
    if len(selected) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 фильма для голосования")
        return
    
    options = [movie["title"][:100] for movie in selected]
    
    await update.effective_chat.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def random_from_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a random movie from specific numbers."""
    if not context.args:
        await update.message.reply_text(
            "❌ Укажи номера фильмов:\n`/rpoll 1,5,12`\n\nНомера см. в /list",
            parse_mode="Markdown"
        )
        return
    
    # Parse numbers
    input_text = " ".join(context.args)
    input_text = input_text.replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Пример: `/rpoll 1,5,12`", parse_mode="Markdown")
        return
    
    if not numbers:
        await update.message.reply_text("❌ Укажи номера фильмов")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    # Get movies by numbers (1-indexed)
    selected = []
    invalid = []
    
    for num in numbers:
        if 1 <= num <= len(to_watch):
            selected.append(to_watch[num - 1])
        else:
            invalid.append(num)
    
    if invalid:
        await update.message.reply_text(
            f"❌ Неверные номера: {', '.join(map(str, invalid))}\n"
            f"Доступно: 1-{len(to_watch)}"
        )
        return
    
    if not selected:
        await update.message.reply_text("❌ Не найдено фильмов")
        return
    
    chosen = random.choice(selected)
    await update.message.reply_text(f"🎲 *{chosen['title']}*", parse_mode="Markdown")


# === Vote Basket Commands ===

async def basket_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add movies to user's basket. Handles /v+ command."""
    text = update.message.text
    # Remove /v+ prefix
    input_text = text[3:].strip() if text.startswith("/v+") else ""
    
    if not input_text:
        await update.message.reply_text(
            "❌ Укажи номера:\n`/v+ 1,5,12`",
            parse_mode="Markdown"
        )
        return
    
    input_text = input_text.replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат")
        return
    
    if not numbers:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    # Validate numbers against movie list
    to_watch = get_movies_db(chat_id, "to_watch")
    valid = []
    invalid = []
    
    for num in numbers:
        if 1 <= num <= len(to_watch):
            valid.append(num)
        else:
            invalid.append(num)
    
    if invalid:
        await update.message.reply_text(
            f"❌ Неверные номера: {', '.join(map(str, invalid))}\n"
            f"Доступно: 1-{len(to_watch)}"
        )
        return
    
    added, exists = add_to_basket(chat_id, user_id, user_name, valid)
    
    parts = []
    if added:
        movie_titles = [to_watch[n-1]["title"] for n in added]
        parts.append(f"✅ Добавлено: {', '.join(movie_titles)}")
    if exists:
        parts.append(f"⚠️ Уже в корзине: {', '.join(map(str, exists))}")
    
    await update.message.reply_text("\n".join(parts))


async def basket_remove_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove movies from user's basket. Handles /v- command."""
    text = update.message.text
    # Remove /v- prefix
    input_text = text[3:].strip() if text.startswith("/v-") else ""
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not input_text:
        # Clear all
        count = remove_from_basket(chat_id, user_id)
        await update.message.reply_text(f"🗑️ Корзина очищена ({count})")
        return
    
    input_text = input_text.replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат")
        return
    
    count = remove_from_basket(chat_id, user_id, numbers)
    await update.message.reply_text(f"🗑️ Удалено: {count}")


async def basket_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's basket."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    nums = get_user_basket(chat_id, user_id)
    
    if not nums:
        await update.message.reply_text("📭 Твоя корзина пуста")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    parts = ["🛒 *Твоя корзина:*\n"]
    for num in nums:
        if 1 <= num <= len(to_watch):
            parts.append(f"{num}. {to_watch[num-1]['title']}")
        else:
            parts.append(f"{num}. _(фильм удалён)_")
    
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def basket_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show full basket for chat."""
    chat_id = update.effective_chat.id
    
    basket = get_full_basket(chat_id)
    
    if not basket:
        await update.message.reply_text("📭 Корзина пуста")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    # Group by user
    by_user = {}
    for item in basket:
        name = item["user_name"]
        if name not in by_user:
            by_user[name] = []
        by_user[name].append(item["movie_num"])
    
    parts = ["🛒 *Общая корзина:*\n"]
    for user_name, nums in by_user.items():
        movies = []
        for num in nums:
            if 1 <= num <= len(to_watch):
                movies.append(f"{num}. {to_watch[num-1]['title']}")
        if movies:
            parts.append(f"*{user_name}:*")
            parts.extend(movies)
            parts.append("")
    
    # Show unique count
    unique = get_unique_basket_movies(chat_id)
    parts.append(f"📊 Уникальных фильмов: {len(unique)}")
    
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def basket_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start poll from basket."""
    chat_id = update.effective_chat.id
    
    unique_nums = get_unique_basket_movies(chat_id)
    
    if not unique_nums:
        await update.message.reply_text("📭 Корзина пуста! Добавь фильмы через /v+")
        return
    
    if len(unique_nums) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 фильма для голосования")
        return
    
    if len(unique_nums) > 10:
        await update.message.reply_text(f"❌ Максимум 10 фильмов. Сейчас: {len(unique_nums)}")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    # Get movie titles
    options = []
    for num in unique_nums:
        if 1 <= num <= len(to_watch):
            options.append(to_watch[num-1]["title"][:100])
    
    if len(options) < 2:
        await update.message.reply_text("❌ Недостаточно валидных фильмов")
        return
    
    await update.effective_chat.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def basket_random(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick random movie from basket."""
    chat_id = update.effective_chat.id
    
    unique_nums = get_unique_basket_movies(chat_id)
    
    if not unique_nums:
        await update.message.reply_text("📭 Корзина пуста!")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    # Filter valid movies
    valid = [num for num in unique_nums if 1 <= num <= len(to_watch)]
    
    if not valid:
        await update.message.reply_text("❌ Нет валидных фильмов в корзине")
        return
    
    chosen_num = random.choice(valid)
    chosen = to_watch[chosen_num - 1]
    
    await update.message.reply_text(f"🎲 *{chosen['title']}*", parse_mode="Markdown")


async def basket_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear entire basket."""
    chat_id = update.effective_chat.id
    count = clear_basket(chat_id)
    await update.message.reply_text(f"🗑️ Корзина очищена ({count})")


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
    application.add_handler(CommandHandler("batch", batch_add))
    application.add_handler(CommandHandler("watched", mark_watched))
    application.add_handler(CommandHandler("remove", remove_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("random", random_movie))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CommandHandler("vote", vote_poll))
    application.add_handler(CommandHandler("rpoll", random_from_selection))
    
    # Vote basket commands
    application.add_handler(MessageHandler(filters.Regex(r'^/v\+'), basket_add_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^/v-'), basket_remove_handler))
    application.add_handler(CommandHandler("vmy", basket_my))
    application.add_handler(CommandHandler("vlist", basket_list))
    application.add_handler(CommandHandler("go", basket_go))
    application.add_handler(CommandHandler("vrand", basket_random))
    application.add_handler(CommandHandler("vc", basket_clear))

    print("🎬 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
