#!/usr/bin/env python3
"""
Telegram Movie Watchlist Bot
With TMDB integration, inline buttons, PostgreSQL storage.
"""

import os
import random
import logging
import json
from datetime import datetime
from io import BytesIO

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# TMDB Config
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/w500"


# ============== DATABASE ==============

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Movies table with TMDB data
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
            tmdb_id INT,
            year INT,
            rating REAL,
            poster_path VARCHAR(255),
            genres TEXT
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
    
    # Vote basket
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
    
    # Add new columns if they don't exist (migration)
    for col, col_type in [("tmdb_id", "INT"), ("year", "INT"), ("rating", "REAL"), 
                          ("poster_path", "VARCHAR(255)"), ("genres", "TEXT")]:
        try:
            cur.execute(f"ALTER TABLE movies ADD COLUMN {col} {col_type}")
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()
    
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database initialized")


# ============== TMDB API ==============

async def tmdb_search(query: str) -> list[dict]:
    """Search TMDB for movies."""
    if not TMDB_API_KEY:
        return []
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/search/movie",
            params={"api_key": TMDB_API_KEY, "query": query, "language": "ru-RU"}
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("results", [])[:5]
    return []


async def tmdb_get_movie(tmdb_id: int) -> dict | None:
    """Get movie details from TMDB."""
    if not TMDB_API_KEY:
        return None
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"}
        )
        if resp.status_code == 200:
            return resp.json()
    return None


async def tmdb_get_recommendations(tmdb_id: int) -> list[dict]:
    """Get movie recommendations from TMDB."""
    if not TMDB_API_KEY:
        return []
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{tmdb_id}/recommendations",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"}
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("results", [])[:10]
    return []


async def tmdb_discover_by_genres(genre_ids: list[int], exclude_ids: list[int] = None) -> list[dict]:
    """Discover movies by genres."""
    if not TMDB_API_KEY:
        return []
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/discover/movie",
            params={
                "api_key": TMDB_API_KEY,
                "language": "ru-RU",
                "with_genres": ",".join(map(str, genre_ids)),
                "sort_by": "vote_average.desc",
                "vote_count.gte": 100
            }
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if exclude_ids:
                results = [m for m in results if m["id"] not in exclude_ids]
            return results[:10]
    return []


# ============== DB FUNCTIONS ==============

def add_movie_db(chat_id: int, title: str, added_by: str, 
                 tmdb_id: int = None, year: int = None, rating: float = None,
                 poster_path: str = None, genres: str = None) -> tuple[bool, str]:
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(
            """INSERT INTO movies (chat_id, title, added_by, tmdb_id, year, rating, poster_path, genres) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (chat_id, title, added_by, tmdb_id, year, rating, poster_path, genres)
        )
        conn.commit()
        return True, "added"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.execute(
            "SELECT status FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)",
            (chat_id, title)
        )
        row = cur.fetchone()
        return False, row["status"] if row else "exists"
    finally:
        cur.close()
        conn.close()


def get_movie_by_id(chat_id: int, movie_id: int) -> dict | None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def mark_watched_by_id(chat_id: int, movie_id: int, watched_by: str) -> tuple[bool, str | None]:
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT id, title, status FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
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
        "UPDATE movies SET status = 'watched', watched_by = %s, watched_at = %s WHERE id = %s",
        (watched_by, datetime.now(), row["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def unwatch_movie_by_id(chat_id: int, movie_id: int) -> tuple[bool, str | None]:
    """Move a watched movie back to to_watch list."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT id, title, status FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return False, None
    
    if row["status"] != "watched":
        cur.close()
        conn.close()
        return False, row["title"]
    
    cur.execute(
        "UPDATE movies SET status = 'to_watch', watched_by = NULL, watched_at = NULL WHERE id = %s",
        (row["id"],)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def remove_movie_by_id(chat_id: int, movie_id: int) -> str | None:
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT title FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()
    
    if not row:
        cur.close()
        conn.close()
        return None
    
    cur.execute("DELETE FROM movies WHERE id = %s", (movie_id,))
    conn.commit()
    cur.close()
    conn.close()
    return row["title"]


def mark_watched_db(chat_id: int, search: str, watched_by: str) -> tuple[bool, str | None]:
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "SELECT id, title, status FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)",
        (chat_id, search)
    )
    row = cur.fetchone()
    
    if not row:
        cur.execute(
            "SELECT id, title, status FROM movies WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s) AND status = 'to_watch' LIMIT 1",
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
        "UPDATE movies SET status = 'watched', watched_by = %s, watched_at = %s WHERE id = %s",
        (watched_by, datetime.now(), row["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def remove_movie_db(chat_id: int, search: str) -> str | None:
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT id, title FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)", (chat_id, search))
    row = cur.fetchone()
    
    if not row:
        cur.execute("SELECT id, title FROM movies WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s) LIMIT 1", (chat_id, f"%{search}%"))
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
    conn = get_db_connection()
    cur = conn.cursor()
    
    if status:
        cur.execute("SELECT * FROM movies WHERE chat_id = %s AND status = %s ORDER BY added_at", (chat_id, status))
    else:
        cur.execute("SELECT * FROM movies WHERE chat_id = %s ORDER BY status DESC, added_at", (chat_id,))
    
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_counts_db(chat_id: int) -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT status, COUNT(*) as count FROM movies WHERE chat_id = %s GROUP BY status", (chat_id,))
    
    counts = {"to_watch": 0, "watched": 0}
    for row in cur.fetchall():
        counts[row["status"]] = row["count"]
    
    cur.close()
    conn.close()
    return counts


def get_watched_genres(chat_id: int) -> list[int]:
    """Get most common genres from watched movies."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT genres FROM movies WHERE chat_id = %s AND status = 'watched' AND genres IS NOT NULL", (chat_id,))
    rows = cur.fetchall()
    
    cur.close()
    conn.close()
    
    genre_count = {}
    for row in rows:
        if row["genres"]:
            for g in row["genres"].split(","):
                g = g.strip()
                if g.isdigit():
                    gid = int(g)
                    genre_count[gid] = genre_count.get(gid, 0) + 1
    
    # Return top 3 genres
    sorted_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)
    return [g[0] for g in sorted_genres[:3]]


def get_watched_tmdb_ids(chat_id: int) -> list[int]:
    """Get TMDB IDs of watched movies."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT tmdb_id FROM movies WHERE chat_id = %s AND tmdb_id IS NOT NULL", (chat_id,))
    rows = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return [row["tmdb_id"] for row in rows]


# ============== VOTE BASKET ==============

def add_to_basket(chat_id: int, user_id: int, user_name: str, movie_nums: list[int]) -> tuple[list[int], list[int]]:
    conn = get_db_connection()
    cur = conn.cursor()
    
    added = []
    exists = []
    
    for num in movie_nums:
        try:
            cur.execute(
                "INSERT INTO vote_basket (chat_id, user_id, user_name, movie_num) VALUES (%s, %s, %s, %s)",
                (chat_id, user_id, user_name, num)
            )
            conn.commit()
            added.append(num)
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            exists.append(num)
    
    cur.close()
    conn.close()
    return added, exists


def remove_from_basket(chat_id: int, user_id: int, movie_nums: list[int] | None = None) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    
    if movie_nums is None:
        cur.execute("DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    else:
        cur.execute("DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s AND movie_num = ANY(%s)", (chat_id, user_id, movie_nums))
    
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def clear_basket(chat_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM vote_basket WHERE chat_id = %s", (chat_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def get_user_basket(chat_id: int, user_id: int) -> list[int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT movie_num FROM vote_basket WHERE chat_id = %s AND user_id = %s ORDER BY movie_num", (chat_id, user_id))
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums


def get_full_basket(chat_id: int) -> list[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, user_name, movie_num FROM vote_basket WHERE chat_id = %s ORDER BY user_name, movie_num", (chat_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_unique_basket_movies(chat_id: int) -> list[int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT movie_num FROM vote_basket WHERE chat_id = %s ORDER BY movie_num", (chat_id,))
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums


# ============== HELPERS ==============

def format_movie(movie: dict, idx: int = None) -> str:
    """Format movie for display."""
    parts = []
    if idx:
        parts.append(f"{idx}.")
    
    parts.append(movie["title"])
    
    if movie.get("year"):
        parts.append(f"({movie['year']})")
    
    if movie.get("rating"):
        parts.append(f"⭐{movie['rating']:.1f}")
    
    return " ".join(parts)


# ============== BOT COMMANDS ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_text = """
🎬 *Movie Watchlist Bot*

*Основные:*
`/add название` — добавить фильм
`/batch` — добавить несколько
`/list` — список фильмов
`/pages` — список с кнопками
`/wlist` — просмотренные с кнопками
`/info 5` — инфо о фильме
`/watched 5` — отметить просмотренным
`/remove 5` — удалить

*Рандом и голосование:*
`/random` — случайный фильм
`/poll N` — poll из N случайных
`/vote 1,5,12` — poll за выбранные
`/rpoll 1,5,12` — рандом из выбранных

*Корзина:*
`/v+ 1,5,12` — в корзину
`/v-` — очистить свою
`/vmy` — моя корзина
`/vlist` — общая корзина
`/go` — запустить poll
`/vrand` — рандом из корзины
`/vc` — очистить всю

*Дополнительно:*
`/suggest` — рекомендации
`/export` — экспорт списка
"""
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add movie with TMDB search."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/add Inception`", parse_mode="Markdown")
        return
    
    query = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    
    # Search TMDB
    if TMDB_API_KEY:
        results = await tmdb_search(query)
        
        if results:
            # Show search results with buttons
            keyboard = []
            context.user_data["tmdb_results"] = {}
            
            for i, movie in enumerate(results[:5]):
                year = movie.get("release_date", "")[:4]
                rating = movie.get("vote_average", 0)
                title = movie.get("title", "Unknown")
                
                btn_text = f"{title}"
                if year:
                    btn_text += f" ({year})"
                if rating:
                    btn_text += f" ⭐{rating:.1f}"
                
                callback_data = f"tmdb_add_{movie['id']}"
                context.user_data["tmdb_results"][str(movie['id'])] = movie
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
            
            # Add manual option
            keyboard.append([InlineKeyboardButton(f"➕ Добавить как \"{query}\"", callback_data=f"add_manual_{query[:50]}")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("🔍 Найдено в TMDB:", reply_markup=reply_markup)
            return
    
    # No TMDB or no results - add directly
    added_by = update.effective_user.first_name
    success, status = add_movie_db(chat_id, query, added_by)
    
    if success:
        counts = get_counts_db(chat_id)
        await update.message.reply_text(f"✅ *{query}* добавлен\n📋 К просмотру: {counts['to_watch']}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ *{query}* уже в списке!", parse_mode="Markdown")


async def tmdb_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle TMDB movie selection."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    added_by = query.from_user.first_name
    
    if data.startswith("tmdb_add_"):
        tmdb_id = data.replace("tmdb_add_", "")
        movie = context.user_data.get("tmdb_results", {}).get(tmdb_id)
        
        if movie:
            title = movie.get("title", "Unknown")
            year = int(movie.get("release_date", "0000")[:4]) if movie.get("release_date") else None
            rating = movie.get("vote_average")
            poster_path = movie.get("poster_path")
            genres = ",".join(map(str, movie.get("genre_ids", [])))
            
            success, status = add_movie_db(
                chat_id, title, added_by,
                tmdb_id=int(tmdb_id), year=year, rating=rating,
                poster_path=poster_path, genres=genres
            )
            
            if success:
                counts = get_counts_db(chat_id)
                text = f"✅ *{title}*"
                if year:
                    text += f" ({year})"
                if rating:
                    text += f" ⭐{rating:.1f}"
                text += f"\n📋 К просмотру: {counts['to_watch']}"
                await query.edit_message_text(text, parse_mode="Markdown")
            else:
                await query.edit_message_text(f"⚠️ *{title}* уже в списке!", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Ошибка. Попробуй ещё раз.")
    
    elif data.startswith("add_manual_"):
        title = data.replace("add_manual_", "")
        success, status = add_movie_db(chat_id, title, added_by)
        
        if success:
            counts = get_counts_db(chat_id)
            await query.edit_message_text(f"✅ *{title}* добавлен\n📋 К просмотру: {counts['to_watch']}", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"⚠️ *{title}* уже в списке!", parse_mode="Markdown")


async def batch_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add multiple movies."""
    text = update.message.text
    if text.startswith("/batch"):
        text = text[6:].strip()
    
    if not text:
        await update.message.reply_text(
            "📝 Отправь список фильмов:\n\n`/batch\nInception\nThe Matrix\nInterstellar`",
            parse_mode="Markdown"
        )
        return
    
    movies = [m.strip() for m in text.split("\n") if m.strip()]
    if not movies:
        await update.message.reply_text("❌ Не найдено фильмов")
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
    
    parts = []
    if added:
        parts.append(f"✅ Добавлено ({len(added)}):")
        for m in added[:10]:
            parts.append(f"  • {m}")
        if len(added) > 10:
            parts.append(f"  ...и ещё {len(added) - 10}")
    
    if skipped:
        parts.append(f"\n⚠️ Уже в списке ({len(skipped)})")
    
    counts = get_counts_db(chat_id)
    parts.append(f"\n📋 Всего к просмотру: {counts['to_watch']}")
    
    await update.message.reply_text("\n".join(parts))


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple list without buttons, 25 per page."""
    chat_id = update.effective_chat.id
    movies = get_movies_db(chat_id)
    
    to_watch = [m for m in movies if m["status"] == "to_watch"]
    watched = [m for m in movies if m["status"] == "watched"]
    
    if not movies:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return
    
    # Build simple list
    parts = [f"📋 *К просмотру ({len(to_watch)}):*\n"]
    
    for i, movie in enumerate(to_watch[:25], 1):
        parts.append(format_movie(movie, i))
    
    if len(to_watch) > 25:
        parts.append(f"_...и ещё {len(to_watch) - 25}_")
    
    if not to_watch:
        parts.append("_пусто_")
    
    parts.append(f"\n✅ *Просмотрено ({len(watched)}):*")
    if watched:
        for i, movie in enumerate(watched[:10], 1):
            parts.append(format_movie(movie, i))
        if len(watched) > 10:
            parts.append(f"_...и ещё {len(watched) - 10}_")
    else:
        parts.append("_пусто_")
    
    parts.append(f"\n💡 Используй /pages для навигации с кнопками")
    
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def pages_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Paginated list with buttons."""
    chat_id = update.effective_chat.id
    page = 0
    
    # Check if page number provided
    if context.args:
        try:
            page = max(0, int(context.args[0]) - 1)
        except ValueError:
            pass
    
    await show_page(update.message, chat_id, page)


async def show_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show a page of movies with buttons."""
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        text = "📭 Список пуст! Добавь фильмы через /add"
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return
    
    per_page = 10
    total_pages = (len(to_watch) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(to_watch))
    page_movies = to_watch[start_idx:end_idx]
    
    # Build text
    parts = [f"📋 *К просмотру* (стр. {page + 1}/{total_pages}):\n"]
    
    for i, movie in enumerate(page_movies, start_idx + 1):
        parts.append(format_movie(movie, i))
    
    # Build keyboard - 2 rows of 5 number buttons
    keyboard = []
    
    # First row of numbers (1-5 or 11-15 etc)
    row1 = []
    row2 = []
    for i, movie in enumerate(page_movies):
        num = start_idx + i + 1
        btn = InlineKeyboardButton(str(num), callback_data=f"movie_{movie['id']}")
        if i < 5:
            row1.append(btn)
        else:
            row2.append(btn)
    
    if row1:
        keyboard.append(row1)
    if row2:
        keyboard.append(row2)
    
    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
    
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))
    
    keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        await message.edit_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle page navigation."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    
    if data == "noop":
        return
    
    if data.startswith("page_"):
        page = int(data.replace("page_", ""))
        await show_page(query.message, chat_id, page, edit=True)
    
    elif data.startswith("movie_"):
        movie_id = int(data.replace("movie_", ""))
        movie = get_movie_by_id(chat_id, movie_id)
        
        if movie:
            await show_movie_detail(query, movie, chat_id)
        else:
            await query.answer("Фильм не найден", show_alert=True)


async def show_movie_detail(query, movie: dict, chat_id: int) -> None:
    """Show movie detail with action buttons."""
    parts = [f"🎬 *{movie['title']}*\n"]
    
    if movie.get("year"):
        parts.append(f"📅 Год: {movie['year']}")
    if movie.get("rating"):
        parts.append(f"⭐ Рейтинг: {movie['rating']:.1f}")
    if movie.get("added_by"):
        parts.append(f"👤 Добавил: {movie['added_by']}")
    if movie.get("added_at"):
        parts.append(f"📆 Когда: {movie['added_at'].strftime('%d.%m.%Y')}")
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Просмотрено", callback_data=f"w_{movie['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"d_{movie['id']}")
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_pages")]
    ]
    
    await query.edit_message_text("\n".join(parts), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def movie_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle watched/delete actions from movie detail."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    user_name = query.from_user.first_name
    
    if data.startswith("w_"):
        movie_id = int(data.replace("w_", ""))
        success, title = mark_watched_by_id(chat_id, movie_id, user_name)
        
        if success:
            await query.answer(f"✅ {title} просмотрен!", show_alert=True)
            await show_page(query.message, chat_id, 0, edit=True)
        else:
            await query.answer("Ошибка", show_alert=True)
    
    elif data.startswith("d_"):
        movie_id = int(data.replace("d_", ""))
        title = remove_movie_by_id(chat_id, movie_id)
        
        if title:
            await query.answer(f"🗑 {title} удалён!", show_alert=True)
            await show_page(query.message, chat_id, 0, edit=True)
        else:
            await query.answer("Ошибка", show_alert=True)
    
    elif data == "back_pages":
        await show_page(query.message, chat_id, 0, edit=True)


async def watched_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle watched movies pagination and actions."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    
    if data == "noop":
        return
    
    if data.startswith("wpage_"):
        page = int(data.replace("wpage_", ""))
        await show_watched_page(query.message, chat_id, page - 1, edit=True)
    
    elif data.startswith("wmovie_"):
        movie_id = int(data.replace("wmovie_", ""))
        movie = get_movie_by_id(chat_id, movie_id)
        
        if movie and movie["status"] == "watched":
            await show_watched_movie_detail(query, movie, chat_id)
        else:
            await query.answer("Фильм не найден", show_alert=True)


async def show_watched_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show a page of watched movies with buttons."""
    watched = get_movies_db(chat_id, "watched")
    
    if not watched:
        text = "📭 Просмотренных фильмов пока нет"
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return
    
    per_page = 10
    total_pages = (len(watched) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(watched))
    page_movies = watched[start_idx:end_idx]
    
    # Build text
    parts = [f"✅ *Просмотренные фильмы* (стр. {page + 1}/{total_pages}):\n"]
    
    for i, movie in enumerate(page_movies, start_idx + 1):
        line = f"{i}. {movie['title']}"
        if movie.get("year"):
            line += f" ({movie['year']})"
        if movie.get("rating"):
            line += f" ⭐{movie['rating']:.1f}"
        parts.append(line)
    
    # Build keyboard
    keyboard = []
    
    # Number buttons
    row1 = []
    row2 = []
    for i, movie in enumerate(page_movies):
        num = start_idx + i + 1
        btn = InlineKeyboardButton(str(num), callback_data=f"wmovie_{movie['id']}")
        if i < 5:
            row1.append(btn)
        else:
            row2.append(btn)
    
    if row1:
        keyboard.append(row1)
    if row2:
        keyboard.append(row2)
    
    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"wpage_{page + 1}"))
    
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"wpage_{page + 2}"))
    
    keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        await message.edit_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def show_watched_movie_detail(query, movie: dict, chat_id: int) -> None:
    """Show watched movie detail with action buttons."""
    parts = [f"🎬 *{movie['title']}*\n"]
    
    if movie.get("year"):
        parts.append(f"📅 Год: {movie['year']}")
    if movie.get("rating"):
        parts.append(f"⭐ Рейтинг: {movie['rating']:.1f}")
    if movie.get("watched_by"):
        parts.append(f"✅ Смотрел: {movie['watched_by']}")
    if movie.get("watched_at"):
        parts.append(f"📆 Когда: {movie['watched_at'].strftime('%d.%m.%Y')}")
    
    keyboard = [
        [
            InlineKeyboardButton("↩️ Вернуть в список", callback_data=f"unw_{movie['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"wd_{movie['id']}")
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_wlist")]
    ]
    
    await query.edit_message_text("\n".join(parts), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def watched_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unwatch/delete actions from watched movie detail."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    
    if data.startswith("unw_"):
        movie_id = int(data.replace("unw_", ""))
        success, title = unwatch_movie_by_id(chat_id, movie_id)
        
        if success:
            await query.answer(f"↩️ {title} возвращён в список!", show_alert=True)
            await show_watched_page(query.message, chat_id, 0, edit=True)
        else:
            await query.answer("Ошибка", show_alert=True)
    
    elif data.startswith("wd_"):
        movie_id = int(data.replace("wd_", ""))
        title = remove_movie_by_id(chat_id, movie_id)
        
        if title:
            await query.answer(f"🗑 {title} удалён!", show_alert=True)
            await show_watched_page(query.message, chat_id, 0, edit=True)
        else:
            await query.answer("Ошибка", show_alert=True)
    
    elif data == "back_wlist":
        await show_watched_page(query.message, chat_id, 0, edit=True)


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show movie info by number with action buttons."""
    if not context.args:
        await update.message.reply_text("❌ Укажи номер: `/info 5`", parse_mode="Markdown")
        return
    
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный номер")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if num < 1 or num > len(to_watch):
        await update.message.reply_text(f"❌ Номер должен быть 1-{len(to_watch)}")
        return
    
    movie = to_watch[num - 1]
    
    parts = [f"🎬 *{movie['title']}*\n"]
    
    if movie.get("year"):
        parts.append(f"📅 Год: {movie['year']}")
    if movie.get("rating"):
        parts.append(f"⭐ Рейтинг: {movie['rating']:.1f}")
    if movie.get("added_by"):
        parts.append(f"👤 Добавил: {movie['added_by']}")
    if movie.get("added_at"):
        parts.append(f"📆 Когда: {movie['added_at'].strftime('%d.%m.%Y')}")
    
    # Action buttons
    keyboard = [[
        InlineKeyboardButton("✅ Просмотрено", callback_data=f"w_{movie['id']}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"d_{movie['id']}")
    ]]
    
    # Show poster if available
    if movie.get("poster_path") and TMDB_API_KEY:
        poster_url = f"{TMDB_IMAGE_URL}{movie['poster_path']}"
        await update.message.reply_photo(
            poster_url, 
            caption="\n".join(parts), 
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "\n".join(parts), 
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def mark_watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark movie as watched by number."""
    if not context.args:
        await update.message.reply_text("❌ Укажи номер: `/watched 5`", parse_mode="Markdown")
        return
    
    chat_id = update.effective_chat.id
    watched_by = update.effective_user.first_name
    to_watch = get_movies_db(chat_id, "to_watch")
    
    # Try as number first
    try:
        num = int(context.args[0])
        if 1 <= num <= len(to_watch):
            movie = to_watch[num - 1]
            success, title = mark_watched_by_id(chat_id, movie['id'], watched_by)
            
            if success:
                counts = get_counts_db(chat_id)
                await update.message.reply_text(
                    f"✅ *{title}* просмотрен!\n📋 Осталось: {counts['to_watch']} | ✅ Просмотрено: {counts['watched']}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"ℹ️ *{title}* уже просмотрен", parse_mode="Markdown")
            return
    except ValueError:
        pass
    
    # Fallback to search by name
    search = " ".join(context.args).strip()
    success, title = mark_watched_db(chat_id, search, watched_by)
    
    if success:
        counts = get_counts_db(chat_id)
        await update.message.reply_text(
            f"✅ *{title}* просмотрен!\n📋 Осталось: {counts['to_watch']} | ✅ Просмотрено: {counts['watched']}",
            parse_mode="Markdown"
        )
    elif title:
        await update.message.reply_text(f"ℹ️ *{title}* уже просмотрен", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")


async def remove_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove movie by number."""
    if not context.args:
        await update.message.reply_text("❌ Укажи номер: `/remove 5`", parse_mode="Markdown")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    # Try as number first
    try:
        num = int(context.args[0])
        if 1 <= num <= len(to_watch):
            movie = to_watch[num - 1]
            title = remove_movie_by_id(chat_id, movie['id'])
            
            if title:
                await update.message.reply_text(f"🗑️ *{title}* удалён", parse_mode="Markdown")
            return
    except ValueError:
        pass
    
    # Fallback to search by name
    search = " ".join(context.args).strip()
    title = remove_movie_db(chat_id, search)
    
    if title:
        await update.message.reply_text(f"🗑️ *{title}* удалён", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")


async def random_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    chosen = random.choice(to_watch)
    text = f"🎲 *{chosen['title']}*"
    if chosen.get("year"):
        text += f" ({chosen['year']})"
    if chosen.get("rating"):
        text += f" ⭐{chosen['rating']:.1f}"
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    num = 3
    if context.args:
        try:
            num = max(1, min(10, int(context.args[0])))
        except ValueError:
            pass
    
    if len(to_watch) < num:
        num = len(to_watch)
    
    if num < 2:
        chosen = random.choice(to_watch)
        await update.message.reply_text(f"🎬 Только один вариант:\n*{chosen['title']}*", parse_mode="Markdown")
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
    if not context.args:
        await update.message.reply_text("❌ Укажи номера:\n`/vote 1,5,12`", parse_mode="Markdown")
        return
    
    input_text = " ".join(context.args).replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат")
        return
    
    if len(numbers) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 фильма")
        return
    
    if len(numbers) > 10:
        await update.message.reply_text("❌ Максимум 10 фильмов")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    selected = []
    invalid = []
    
    for num in numbers:
        if 1 <= num <= len(to_watch):
            selected.append(to_watch[num - 1])
        else:
            invalid.append(num)
    
    if invalid:
        await update.message.reply_text(f"❌ Неверные номера: {', '.join(map(str, invalid))}")
        return
    
    options = [movie["title"][:100] for movie in selected]
    
    await update.effective_chat.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def random_from_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ Укажи номера:\n`/rpoll 1,5,12`", parse_mode="Markdown")
        return
    
    input_text = " ".join(context.args).replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат")
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    selected = []
    for num in numbers:
        if 1 <= num <= len(to_watch):
            selected.append(to_watch[num - 1])
    
    if not selected:
        await update.message.reply_text("❌ Нет валидных фильмов")
        return
    
    chosen = random.choice(selected)
    await update.message.reply_text(f"🎲 *{chosen['title']}*", parse_mode="Markdown")


async def suggest_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suggest movies based on watched genres."""
    chat_id = update.effective_chat.id
    
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB не настроен")
        return
    
    # Get watched genres
    genres = get_watched_genres(chat_id)
    
    if not genres:
        await update.message.reply_text("❌ Нужно сначала посмотреть фильмы с TMDB данными для рекомендаций")
        return
    
    # Get already known movies to exclude
    exclude_ids = get_watched_tmdb_ids(chat_id)
    
    # Discover movies by genres
    recommendations = await tmdb_discover_by_genres(genres, exclude_ids)
    
    if not recommendations:
        await update.message.reply_text("❌ Не удалось найти рекомендации")
        return
    
    parts = ["🎯 *Рекомендации по твоим вкусам:*\n"]
    
    keyboard = []
    context.user_data["tmdb_results"] = {}
    
    for movie in recommendations[:5]:
        year = movie.get("release_date", "")[:4]
        rating = movie.get("vote_average", 0)
        title = movie.get("title", "Unknown")
        
        line = f"• *{title}*"
        if year:
            line += f" ({year})"
        if rating:
            line += f" ⭐{rating:.1f}"
        parts.append(line)
        
        # Add button to add movie
        context.user_data["tmdb_results"][str(movie['id'])] = movie
        keyboard.append([InlineKeyboardButton(f"➕ {title}", callback_data=f"tmdb_add_{movie['id']}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def wlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show watched movies with pagination."""
    chat_id = update.effective_chat.id
    args = context.args or []
    
    # Parse args
    show_all = "-a" in args
    search_query = None
    page_num = 1
    
    if "-s" in args:
        try:
            idx = args.index("-s")
            if idx + 1 < len(args):
                search_query = " ".join(args[idx + 1:])
        except ValueError:
            pass
    
    if "-p" in args:
        try:
            idx = args.index("-p")
            if idx + 1 < len(args):
                page_num = int(args[idx + 1])
        except (ValueError, IndexError):
            pass
    
    watched = get_movies_db(chat_id, "watched")
    
    if not watched:
        await update.message.reply_text("📭 Просмотренных фильмов пока нет")
        return
    
    # Apply search
    if search_query:
        watched = [m for m in watched if search_query.lower() in m["title"].lower()]
        if not watched:
            await update.message.reply_text(f"🔍 Не найдено: '{search_query}'")
            return
    
    # Paginate
    per_page = 50 if show_all else 10
    total_pages = (len(watched) + per_page - 1) // per_page
    page_num = max(1, min(page_num, total_pages))
    
    start = (page_num - 1) * per_page
    end = start + per_page
    page_movies = watched[start:end]
    
    # Build message
    header = "✅ *Просмотренные фильмы*\n"
    if search_query:
        header += f"🔍 Поиск: _{search_query}_\n"
    header += "\n"
    
    lines = []
    for i, movie in enumerate(page_movies, start + 1):
        line = f"{i}. {movie['title']}"
        if movie.get("year"):
            line += f" ({movie['year']})"
        if movie.get("rating"):
            line += f" ⭐{movie['rating']:.1f}"
        lines.append(line)
    
    message = header + "\n".join(lines)
    
    # Build keyboard
    keyboard = []
    
    # Number buttons (max 5 per row)
    if len(page_movies) <= 10:
        num_buttons = [
            InlineKeyboardButton(str(i), callback_data=f"wmovie_{watched[start + idx]['id']}")
            for idx, i in enumerate(range(start + 1, start + len(page_movies) + 1))
        ]
        for i in range(0, len(num_buttons), 5):
            keyboard.append(num_buttons[i:i + 5])
    
    # Pagination row
    nav_row = []
    if page_num > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"wpage_{page_num - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
    if page_num < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"wpage_{page_num + 1}"))
    keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


async def export_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export movie list to text file."""
    chat_id = update.effective_chat.id
    movies = get_movies_db(chat_id)
    
    if not movies:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    to_watch = [m for m in movies if m["status"] == "to_watch"]
    watched = [m for m in movies if m["status"] == "watched"]
    
    lines = ["MOVIE WATCHLIST", "=" * 40, "", "TO WATCH:", "-" * 20]
    
    for i, movie in enumerate(to_watch, 1):
        line = f"{i}. {movie['title']}"
        if movie.get("year"):
            line += f" ({movie['year']})"
        if movie.get("rating"):
            line += f" ⭐{movie['rating']:.1f}"
        lines.append(line)
    
    lines.extend(["", "WATCHED:", "-" * 20])
    
    for i, movie in enumerate(watched, 1):
        line = f"{i}. {movie['title']}"
        if movie.get("year"):
            line += f" ({movie['year']})"
        lines.append(line)
    
    lines.extend(["", "=" * 40, f"Total: {len(to_watch)} to watch, {len(watched)} watched"])
    
    content = "\n".join(lines)
    
    # Send as file
    file = BytesIO(content.encode("utf-8"))
    file.name = "watchlist.txt"
    
    await update.message.reply_document(file, caption="📄 Твой список фильмов")


# ============== VOTE BASKET COMMANDS ==============

async def basket_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    input_text = text[3:].strip() if text.startswith("/v+") else ""
    
    if not input_text:
        await update.message.reply_text("❌ Укажи номера:\n`/v+ 1,5,12`", parse_mode="Markdown")
        return
    
    input_text = input_text.replace(",", " ")
    
    try:
        numbers = [int(n.strip()) for n in input_text.split() if n.strip()]
    except ValueError:
        await update.message.reply_text("❌ Неверный формат")
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    to_watch = get_movies_db(chat_id, "to_watch")
    valid = [n for n in numbers if 1 <= n <= len(to_watch)]
    invalid = [n for n in numbers if n not in valid]
    
    if invalid:
        await update.message.reply_text(f"❌ Неверные номера: {', '.join(map(str, invalid))}")
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
    text = update.message.text
    input_text = text[3:].strip() if text.startswith("/v-") else ""
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not input_text:
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
    
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def basket_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    basket = get_full_basket(chat_id)
    
    if not basket:
        await update.message.reply_text("📭 Корзина пуста")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    by_user = {}
    for item in basket:
        name = item["user_name"]
        if name not in by_user:
            by_user[name] = []
        by_user[name].append(item["movie_num"])
    
    parts = ["🛒 *Общая корзина:*\n"]
    for user_name, nums in by_user.items():
        movies = [f"{num}. {to_watch[num-1]['title']}" for num in nums if 1 <= num <= len(to_watch)]
        if movies:
            parts.append(f"*{user_name}:*")
            parts.extend(movies)
            parts.append("")
    
    unique = get_unique_basket_movies(chat_id)
    parts.append(f"📊 Уникальных: {len(unique)}")
    
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def basket_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    unique_nums = get_unique_basket_movies(chat_id)
    
    if not unique_nums:
        await update.message.reply_text("📭 Корзина пуста!")
        return
    
    if len(unique_nums) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 фильма")
        return
    
    if len(unique_nums) > 10:
        await update.message.reply_text(f"❌ Максимум 10. Сейчас: {len(unique_nums)}")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    options = [to_watch[num-1]["title"][:100] for num in unique_nums if 1 <= num <= len(to_watch)]
    
    if len(options) < 2:
        await update.message.reply_text("❌ Недостаточно фильмов")
        return
    
    await update.effective_chat.send_poll(
        question="🎬 Что смотрим?",
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


async def basket_random(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    unique_nums = get_unique_basket_movies(chat_id)
    
    if not unique_nums:
        await update.message.reply_text("📭 Корзина пуста!")
        return
    
    to_watch = get_movies_db(chat_id, "to_watch")
    valid = [num for num in unique_nums if 1 <= num <= len(to_watch)]
    
    if not valid:
        await update.message.reply_text("❌ Нет валидных фильмов")
        return
    
    chosen = to_watch[random.choice(valid) - 1]
    await update.message.reply_text(f"🎲 *{chosen['title']}*", parse_mode="Markdown")


async def basket_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    count = clear_basket(chat_id)
    await update.message.reply_text(f"🗑️ Корзина очищена ({count})")


# ============== MAIN ==============

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    
    init_db()
    
    application = Application.builder().token(token).build()
    
    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_movie))
    application.add_handler(CommandHandler("batch", batch_add))
    application.add_handler(CommandHandler("watched", mark_watched))
    application.add_handler(CommandHandler("remove", remove_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("pages", pages_command))
    application.add_handler(CommandHandler("wlist", wlist_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("random", random_movie))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CommandHandler("vote", vote_poll))
    application.add_handler(CommandHandler("rpoll", random_from_selection))
    application.add_handler(CommandHandler("suggest", suggest_movies))
    application.add_handler(CommandHandler("export", export_list))
    
    # Vote basket
    application.add_handler(MessageHandler(filters.Regex(r'^/v\+'), basket_add_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^/v-'), basket_remove_handler))
    application.add_handler(CommandHandler("vmy", basket_my))
    application.add_handler(CommandHandler("vlist", basket_list))
    application.add_handler(CommandHandler("go", basket_go))
    application.add_handler(CommandHandler("vrand", basket_random))
    application.add_handler(CommandHandler("vc", basket_clear))
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(tmdb_add_callback, pattern=r"^(tmdb_add_|add_manual_)"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^(page_|movie_|noop)"))
    application.add_handler(CallbackQueryHandler(movie_action_callback, pattern=r"^(w_|d_|back_pages)"))
    application.add_handler(CallbackQueryHandler(watched_callback, pattern=r"^(wpage_|wmovie_)"))
    application.add_handler(CallbackQueryHandler(watched_action_callback, pattern=r"^(unw_|wd_|back_wlist)"))
    
    print("🎬 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
