#!/usr/bin/env python3
"""
Telegram Movie Watchlist Bot
Simplified version with to_watch and watched lists.
"""

import json
import random
import logging
from pathlib import Path
from datetime import datetime
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

# Data file path
DATA_FILE = Path("movie_data.json")


def load_data() -> dict:
    """Load movie data from JSON file."""
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    """Save movie data to JSON file."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_chat_data(chat_id: int) -> dict:
    """Get or create data for a specific chat."""
    data = load_data()
    chat_key = str(chat_id)
    if chat_key not in data:
        data[chat_key] = {
            "to_watch": [],
            "watched": [],
        }
        save_data(data)
    return data[chat_key]


def update_chat_data(chat_id: int, chat_data: dict) -> None:
    """Update data for a specific chat."""
    data = load_data()
    data[str(chat_id)] = chat_data
    save_data(data)


def find_movie(movies: list, search: str) -> tuple[int, dict] | tuple[None, None]:
    """Find movie by title (case-insensitive partial match)."""
    search_lower = search.lower()
    for i, movie in enumerate(movies):
        if movie["title"].lower() == search_lower:
            return i, movie
    # Partial match
    for i, movie in enumerate(movies):
        if search_lower in movie["title"].lower():
            return i, movie
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message with available commands."""
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
    """Show help message."""
    await start(update, context)


async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a movie to to_watch list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/add Inception`", parse_mode="Markdown")
        return

    movie_title = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)

    # Check if already exists
    for movie in chat_data["to_watch"] + chat_data["watched"]:
        if movie["title"].lower() == movie_title.lower():
            await update.message.reply_text(f"⚠️ *{movie['title']}* уже в списке!", parse_mode="Markdown")
            return

    # Add movie
    movie_entry = {
        "title": movie_title,
        "added_by": update.effective_user.first_name,
        "added_at": datetime.now().isoformat(),
    }
    chat_data["to_watch"].append(movie_entry)
    update_chat_data(chat_id, chat_data)

    count = len(chat_data["to_watch"])
    await update.message.reply_text(
        f"✅ *{movie_title}* добавлен в список\n📋 Всего к просмотру: {count}",
        parse_mode="Markdown"
    )


async def mark_watched(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move a movie to watched list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/watched Inception`", parse_mode="Markdown")
        return

    search = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)

    # Find in to_watch
    idx, movie = find_movie(chat_data["to_watch"], search)

    if movie is None:
        # Check if already in watched
        _, in_watched = find_movie(chat_data["watched"], search)
        if in_watched:
            await update.message.reply_text(f"ℹ️ *{in_watched['title']}* уже просмотрен", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")
        return

    # Move to watched
    movie_entry = chat_data["to_watch"].pop(idx)
    movie_entry["watched_at"] = datetime.now().isoformat()
    movie_entry["watched_by"] = update.effective_user.first_name
    chat_data["watched"].append(movie_entry)
    update_chat_data(chat_id, chat_data)

    await update.message.reply_text(
        f"✅ *{movie_entry['title']}* просмотрен!\n"
        f"📋 Осталось: {len(chat_data['to_watch'])} | ✅ Просмотрено: {len(chat_data['watched'])}",
        parse_mode="Markdown"
    )


async def remove_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a movie from any list."""
    if not context.args:
        await update.message.reply_text("❌ Укажи название:\n`/remove Inception`", parse_mode="Markdown")
        return

    search = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)

    # Try to find in to_watch first
    idx, movie = find_movie(chat_data["to_watch"], search)
    if movie:
        removed = chat_data["to_watch"].pop(idx)
        update_chat_data(chat_id, chat_data)
        await update.message.reply_text(f"🗑️ *{removed['title']}* удалён", parse_mode="Markdown")
        return

    # Try watched
    idx, movie = find_movie(chat_data["watched"], search)
    if movie:
        removed = chat_data["watched"].pop(idx)
        update_chat_data(chat_id, chat_data)
        await update.message.reply_text(f"🗑️ *{removed['title']}* удалён", parse_mode="Markdown")
        return

    await update.message.reply_text(f"❌ Фильм *{search}* не найден", parse_mode="Markdown")


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all movies."""
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)

    parts = ["🎬 *Список фильмов*\n"]

    # To watch
    to_watch = chat_data["to_watch"]
    parts.append(f"📋 *К просмотру ({len(to_watch)}):*")
    if to_watch:
        for i, movie in enumerate(to_watch, 1):
            parts.append(f"{i}. {movie['title']}")
    else:
        parts.append("_пусто_")

    parts.append("")

    # Watched
    watched = chat_data["watched"]
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
    chat_data = get_chat_data(chat_id)

    to_watch = chat_data["to_watch"]

    if not to_watch:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return

    chosen = random.choice(to_watch)

    await update.message.reply_text(
        f"🎲 *{chosen['title']}*",
        parse_mode="Markdown"
    )


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a poll with N random movies."""
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)

    to_watch = chat_data["to_watch"]

    if not to_watch:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return

    # Get number of options (default 3)
    num = 3
    if context.args:
        try:
            num = int(context.args[0])
            num = max(1, min(10, num))  # Limit 1-10
        except ValueError:
            pass

    if len(to_watch) < num:
        num = len(to_watch)

    if num < 2:
        # Just show the movie if only 1
        chosen = random.choice(to_watch)
        await update.message.reply_text(
            f"🎬 Только один вариант:\n*{chosen['title']}*",
            parse_mode="Markdown"
        )
        return

    # Pick random movies
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
    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        print("=" * 50)
        print("TELEGRAM MOVIE WATCHLIST BOT")
        print("=" * 50)
        print("\n1. Напиши @BotFather в Telegram")
        print("2. Отправь /newbot и следуй инструкциям")
        print("3. Скопируй токен и запусти:")
        print("\n   export TELEGRAM_BOT_TOKEN='твой_токен'")
        print("   python movie_watchlist_bot.py")
        print("\n" + "=" * 50)
        return

    application = Application.builder().token(token).build()

    # Commands
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
