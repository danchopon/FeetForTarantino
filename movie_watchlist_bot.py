#!/usr/bin/env python3
"""
Telegram Movie Watchlist Bot
With TMDB integration, inline buttons, PostgreSQL storage.
MCP server integration for smart recommendations.
"""

import os
import sys
import random
import logging
import json
import asyncio
from io import BytesIO

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from bot.db import (
    get_db_connection, init_db,
    add_movie_db, get_movie_by_id, mark_watched_by_id, unwatch_movie_by_id,
    update_movie_tmdb_data, rename_movie_by_id, remove_movie_by_id,
    mark_watched_db, remove_movie_db, get_movies_db, get_counts_db,
    get_watched_genres, get_watched_tmdb_ids,
    add_to_basket, remove_from_basket, clear_basket,
    get_user_basket, get_full_basket, get_unique_basket_movies,
)
from bot.tmdb_api import (
    parse_movie_query, tmdb_search, tmdb_get_movie,
    tmdb_get_recommendations, tmdb_discover_by_genres,
    TMDB_API_KEY, TMDB_IMAGE_URL,
)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


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
`/add название` — один фильм + TMDB
`/add` + список — несколько без TMDB
`/list` — пагинация + кнопки
`/list -a` — только пагинация
`/list -s Matrix` — поиск
`/list -ps 25` — размер страницы
`/pages` — список с кнопками
`/wlist` — просмотренные
`/info 5` — инфо о фильме
`/watched 5` — отметить просмотренным
`/remove 5` — удалить
`/rename 5 Новое название` — переименовать

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
`/suggest` — рекомендации (по истории)
`/suggest мрачное` — по настроению
`/similar Inception` — похожие на фильм
`/sync` — синхронизация с TMDB
`/sync 5` — синхронизировать фильм #5
`/sync -a` — синхронизировать все
`/sync -w` — синхронизировать просмотренные
`/export` — экспорт .txt
`/export -csv` — экспорт .csv
"""
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def add_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add movie(s) - single with TMDB search or multiple directly."""
    text = update.message.text
    
    if text.startswith("/add"):
        text = text[4:].strip()
    
    if not text:
        await update.message.reply_text(
            "❌ Укажи название:\n`/add Inception` - с поиском TMDB\n\n"
            "Или несколько фильмов:\n`/add\nInception\nThe Matrix\nInterstellar`",
            parse_mode="Markdown"
        )
        return
    
    chat_id = update.effective_chat.id
    added_by = update.effective_user.first_name
    
    # Check if multi-line (batch mode)
    if "\n" in text:
        movies = [m.strip() for m in text.split("\n") if m.strip()]
        
        if not movies:
            await update.message.reply_text("❌ Не найдено фильмов")
            return
        
        # Batch add without TMDB
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
        return
    
    # Single movie - search TMDB
    query = text.strip()
    
    if TMDB_API_KEY:
        # Parse title and year from query
        title, year = parse_movie_query(query)
        
        search_data = await tmdb_search(title, page=1, year=year)
        results = search_data.get("results", [])
        
        if results:
            # Store search data for pagination
            context.user_data["tmdb_search_query"] = title
            context.user_data["tmdb_search_year"] = year
            context.user_data["tmdb_search_mode"] = "add"
            context.user_data["tmdb_search_chat_id"] = chat_id
            
            await show_tmdb_results(update.message, context, search_data, query, mode="add")
            return
    
    # No TMDB or no results - add directly
    success, status = add_movie_db(chat_id, query, added_by)
    
    if success:
        counts = get_counts_db(chat_id)
        await update.message.reply_text(f"✅ *{query}* добавлен\n📋 К просмотру: {counts['to_watch']}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ *{query}* уже в списке!", parse_mode="Markdown")


async def show_tmdb_results(message, context: ContextTypes.DEFAULT_TYPE, search_data: dict, query: str, mode: str = "add") -> None:
    """Show TMDB search results with pagination."""
    results = search_data.get("results", [])[:5]  # 5 per page
    page = search_data.get("page", 1)
    total_pages = search_data.get("total_pages", 1)
    
    # Build keyboard
    keyboard = []
    context.user_data["tmdb_results"] = {}
    
    for i, movie in enumerate(results):
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
    
    # Pagination row
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️ Пред", callback_data=f"tmdb_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("След ▶️", callback_data=f"tmdb_page_{page + 1}"))
        keyboard.append(nav_row)
    
    # Add manual option
    keyboard.append([InlineKeyboardButton(f"➕ Добавить как \"{query}\"", callback_data=f"add_manual_{query[:50]}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    header = "🔍 Найдено в TMDB"
    if total_pages > 1:
        header += f" (стр. {page}/{total_pages})"
    header += ":"
    
    await message.reply_text(header, reply_markup=reply_markup)


async def tmdb_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle TMDB movie selection and pagination."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = query.message.chat_id
    added_by = query.from_user.first_name
    
    if data.startswith("tmdb_page_"):
        # Handle pagination
        page = int(data.replace("tmdb_page_", ""))
        search_query = context.user_data.get("tmdb_search_query")
        search_year = context.user_data.get("tmdb_search_year")
        mode = context.user_data.get("tmdb_search_mode", "add")
        
        if search_query:
            search_data = await tmdb_search(search_query, page=page, year=search_year)
            
            # Update message with new page
            results = search_data.get("results", [])[:5]
            page_num = search_data.get("page", 1)
            total_pages = search_data.get("total_pages", 1)
            
            # Build keyboard
            keyboard = []
            context.user_data["tmdb_results"] = {}
            
            for movie in results:
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
            
            # Pagination row
            if total_pages > 1:
                nav_row = []
                if page_num > 1:
                    nav_row.append(InlineKeyboardButton("◀️ Пред", callback_data=f"tmdb_page_{page_num - 1}"))
                nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
                if page_num < total_pages:
                    nav_row.append(InlineKeyboardButton("След ▶️", callback_data=f"tmdb_page_{page_num + 1}"))
                keyboard.append(nav_row)
            
            # Add manual option - use original query with year if it was provided
            original_query = context.user_data.get("tmdb_search_query")
            if search_year:
                original_query = f"{original_query} ({search_year})"
            keyboard.append([InlineKeyboardButton(f"➕ Добавить как \"{original_query}\"", callback_data=f"add_manual_{original_query[:50]}")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            header = "🔍 Найдено в TMDB"
            if total_pages > 1:
                header += f" (стр. {page_num}/{total_pages})"
            header += ":"
            
            await query.edit_message_text(header, reply_markup=reply_markup)
        return
    
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
    """Paginated list with search and customizable page size."""
    chat_id = update.effective_chat.id
    args = context.args or []
    
    # Parse arguments
    show_all_pages = "-a" in args  # Only pagination buttons, no number buttons
    search_query = None
    page_num = 1
    page_size = 10
    
    # Search term
    if "-s" in args:
        try:
            idx = args.index("-s")
            if idx + 1 < len(args):
                # Get all args after -s until next flag
                search_terms = []
                for i in range(idx + 1, len(args)):
                    if args[i].startswith("-"):
                        break
                    search_terms.append(args[i])
                search_query = " ".join(search_terms)
        except (ValueError, IndexError):
            pass
    
    # Page number
    if "-p" in args:
        try:
            idx = args.index("-p")
            if idx + 1 < len(args):
                page_num = int(args[idx + 1])
        except (ValueError, IndexError):
            pass
    
    # Page size (max 50)
    if "-ps" in args:
        try:
            idx = args.index("-ps")
            if idx + 1 < len(args):
                page_size = max(1, min(50, int(args[idx + 1])))
        except (ValueError, IndexError):
            pass
    
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        await update.message.reply_text("📭 Список пуст! Добавь фильмы через /add")
        return
    
    # Apply search filter
    if search_query:
        to_watch = [m for m in to_watch if search_query.lower() in m["title"].lower()]
        if not to_watch:
            await update.message.reply_text(f"🔍 Не найдено: '{search_query}'")
            return
    
    # Paginate
    total_pages = (len(to_watch) + page_size - 1) // page_size
    page_num = max(1, min(page_num, total_pages))
    
    start = (page_num - 1) * page_size
    end = start + page_size
    page_movies = to_watch[start:end]
    
    # Build message
    header = f"📋 *К просмотру* (стр. {page_num}/{total_pages})\n"
    if search_query:
        header += f"🔍 Поиск: _{search_query}_\n"
    header += "\n"
    
    lines = []
    for i, movie in enumerate(page_movies, start + 1):
        lines.append(format_movie(movie, i))
    
    message = header + "\n".join(lines)
    
    # Build keyboard
    keyboard = []
    
    # Number buttons (only if not -a flag and page size <= 10)
    if not show_all_pages and len(page_movies) <= 10:
        row1 = []
        row2 = []
        for i, movie in enumerate(page_movies):
            num = start + i + 1
            btn = InlineKeyboardButton(str(num), callback_data=f"movie_{movie['id']}")
            if i < 5:
                row1.append(btn)
            else:
                row2.append(btn)
        
        if row1:
            keyboard.append(row1)
        if row2:
            keyboard.append(row2)
    
    # Pagination row
    nav_row = []
    if page_num > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"list_{page_num - 1}_{page_size}_{search_query or ''}_{show_all_pages}"))
    nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
    if page_num < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"list_{page_num + 1}_{page_size}_{search_query or ''}_{show_all_pages}"))
    keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


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
    
    elif data.startswith("list_"):
        # Format: list_page_size_search_showall
        parts = data.replace("list_", "").split("_", 3)
        page = int(parts[0])
        page_size = int(parts[1])
        search_query = parts[2] if parts[2] else None
        show_all = parts[3] == "True" if len(parts) > 3 else False
        
        to_watch = get_movies_db(chat_id, "to_watch")
        
        # Apply search
        if search_query:
            to_watch = [m for m in to_watch if search_query.lower() in m["title"].lower()]
        
        if not to_watch:
            await query.answer("Список пуст", show_alert=True)
            return
        
        # Paginate
        total_pages = (len(to_watch) + page_size - 1) // page_size
        page = max(1, min(page, total_pages))
        
        start = (page - 1) * page_size
        end = start + page_size
        page_movies = to_watch[start:end]
        
        # Build message
        header = f"📋 *К просмотру* (стр. {page}/{total_pages})\n"
        if search_query:
            header += f"🔍 Поиск: _{search_query}_\n"
        header += "\n"
        
        lines = [format_movie(m, start + i + 1) for i, m in enumerate(page_movies)]
        message = header + "\n".join(lines)
        
        # Build keyboard
        keyboard = []
        
        # Number buttons (only if not show_all and <= 10)
        if not show_all and len(page_movies) <= 10:
            row1 = []
            row2 = []
            for i, movie in enumerate(page_movies):
                num = start + i + 1
                btn = InlineKeyboardButton(str(num), callback_data=f"movie_{movie['id']}")
                if i < 5:
                    row1.append(btn)
                else:
                    row2.append(btn)
            
            if row1:
                keyboard.append(row1)
            if row2:
                keyboard.append(row2)
        
        # Pagination row
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"list_{page - 1}_{page_size}_{search_query or ''}_{show_all}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"list_{page + 1}_{page_size}_{search_query or ''}_{show_all}"))
        keyboard.append(nav_row)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, parse_mode="Markdown", reply_markup=reply_markup)
    
    elif data.startswith("lpage_"):
        page = int(data.replace("lpage_", ""))
        await show_page(query.message, chat_id, page - 1, edit=True)
    
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
        [InlineKeyboardButton("✏️ Переименовать", callback_data=f"r_{movie['id']}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_pages")]
    ]
    
    await query.edit_message_text("\n".join(parts), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def movie_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle watched/delete/rename actions from movie detail."""
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
    
    elif data.startswith("r_"):
        movie_id = int(data.replace("r_", ""))
        movie = get_movie_by_id(chat_id, movie_id)
        
        if movie:
            # Store movie_id for rename
            context.user_data["rename_movie_id"] = movie_id
            
            # Show old title in monospace for easy copying
            keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel_rename")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✏️ Переименовать:\n\n"
                f"Старое название:\n`{movie['title']}`\n\n"
                f"Отправь новое название:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await query.answer("Ошибка", show_alert=True)
    
    elif data == "cancel_rename":
        context.user_data.pop("rename_movie_id", None)
        await query.edit_message_text("❌ Переименование отменено")
    
    elif data == "back_to_list":
        # Return to first page of list
        await show_list_page(query.message, chat_id, 1, edit=True)
    
    elif data == "back_pages":
        await show_page(query.message, chat_id, 0, edit=True)


async def show_list_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show list page for back_to_list button."""
    to_watch = get_movies_db(chat_id, "to_watch")
    
    if not to_watch:
        text = "📭 Список пуст!"
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return
    
    per_page = 10
    total_pages = (len(to_watch) + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    
    start = (page - 1) * per_page
    end = start + per_page
    page_movies = to_watch[start:end]
    
    # Build message
    header = f"📋 *К просмотру* (стр. {page}/{total_pages}):\n"
    lines = [format_movie(m, start + i + 1) for i, m in enumerate(page_movies)]
    text = header + "\n".join(lines)
    
    # Build keyboard
    keyboard = []
    
    # Number buttons
    row1 = []
    row2 = []
    for i, movie in enumerate(page_movies):
        num = start + i + 1
        btn = InlineKeyboardButton(str(num), callback_data=f"movie_{movie['id']}")
        if i < 5:
            row1.append(btn)
        else:
            row2.append(btn)
    
    if row1:
        keyboard.append(row1)
    if row2:
        keyboard.append(row2)
    
    # Pagination row
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"list_{page - 1}_10__False"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"list_{page + 1}_10__False"))
    keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


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


async def rename_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rename movie by number. Usage: /rename 5 New Title"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Укажи номер и новое название:\n`/rename 5 Новое название`",
            parse_mode="Markdown"
        )
        return
    
    chat_id = update.effective_chat.id
    to_watch = get_movies_db(chat_id, "to_watch")
    
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Первый аргумент должен быть номером фильма")
        return
    
    if num < 1 or num > len(to_watch):
        await update.message.reply_text(f"❌ Номер должен быть 1-{len(to_watch)}")
        return
    
    new_title = " ".join(context.args[1:]).strip()
    
    if not new_title:
        await update.message.reply_text("❌ Укажи новое название")
        return
    
    movie = to_watch[num - 1]
    success, old_title = rename_movie_by_id(chat_id, movie['id'], new_title)
    
    if success:
        await update.message.reply_text(
            f"✏️ Переименовано:\n*{old_title}* → *{new_title}*",
            parse_mode="Markdown"
        )
    else:
        if old_title:
            await update.message.reply_text(
                f"❌ Фильм *{new_title}* уже есть в списке",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Фильм не найден")


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


async def call_mcp_tool(tool_name: str, arguments: dict) -> dict | None:
    """Call a tool on the TMDB MCP server via stdio."""
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmdb_mcp_server.py")
    python_exe = sys.executable

    server_params = StdioServerParameters(
        command=python_exe,
        args=[server_script],
        env={**os.environ},
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if result.content:
                    return json.loads(result.content[0].text)
    except Exception as e:
        logger.error(f"MCP call failed ({tool_name}): {e}")
    return None


async def suggest_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suggest movies via MCP — supports mood description.

    Usage:
      /suggest                    — by watch history
      /suggest мрачное но короткое — by mood
    """
    chat_id = update.effective_chat.id
    mood = " ".join(context.args) if context.args else ""

    await update.message.reply_text("🔍 Ищу рекомендации...")

    result = await call_mcp_tool("suggest_movies", {"chat_id": chat_id, "mood": mood})

    if not result or "error" in result:
        err = result.get("error", "неизвестная ошибка") if result else "MCP сервер недоступен"
        await update.message.reply_text(f"❌ {err}")
        return

    suggestions = result.get("suggestions", [])
    if not suggestions:
        await update.message.reply_text("❌ Не удалось найти рекомендации")
        return

    mood_text = result.get("mood", "")
    genres_text = ", ".join(result.get("matched_genres", []))

    parts = ["🎯 *Рекомендации"]
    if mood_text and mood_text != "общие рекомендации":
        parts[0] += f" по запросу «{mood_text}»"
    parts[0] += ":*"
    if genres_text:
        parts.append(f"_Жанры: {genres_text}_\n")

    keyboard = []
    context.user_data["tmdb_results"] = {}

    for movie in suggestions:
        tmdb_id = movie["tmdb_id"]
        title = movie["title"]
        year = movie.get("year", "")
        rating = movie.get("rating", 0)
        reasoning = movie.get("reasoning", "")
        overview = movie.get("overview", "")

        line = f"• *{title}*"
        if year:
            line += f" ({year})"
        if rating:
            line += f" ⭐{rating:.1f}"
        if reasoning:
            line += f"\n  _{reasoning}_"
        if overview:
            short_overview = overview[:120] + "..." if len(overview) > 120 else overview
            line += f"\n  {short_overview}"
        parts.append(line)

        # Store minimal TMDB data for add button
        context.user_data["tmdb_results"][str(tmdb_id)] = {
            "id": tmdb_id,
            "title": title,
            "release_date": f"{year}-01-01" if year else "",
            "vote_average": rating,
        }
        keyboard.append([InlineKeyboardButton(f"➕ {title}", callback_data=f"tmdb_add_{tmdb_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def similar_movies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Find movies similar to a given title, excluding current watchlist.

    Usage: /similar Inception
    """
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            "❌ Укажи название фильма:\n`/similar Inception`\n`/similar Начало`",
            parse_mode="Markdown",
        )
        return

    movie_title = " ".join(context.args)
    await update.message.reply_text(f"🔍 Ищу похожее на *{movie_title}*...", parse_mode="Markdown")

    result = await call_mcp_tool("find_similar", {"chat_id": chat_id, "movie_title": movie_title})

    if not result or "error" in result:
        err = result.get("error", "неизвестная ошибка") if result else "MCP сервер недоступен"
        await update.message.reply_text(f"❌ {err}")
        return

    source = result.get("source", {})
    similar = result.get("similar", [])
    filtered_out = result.get("filtered_out", 0)

    if not similar:
        await update.message.reply_text(
            f"❌ Не нашёл похожих фильмов на *{source.get('title', movie_title)}*",
            parse_mode="Markdown",
        )
        return

    source_title = source.get("title", movie_title)
    source_year = source.get("year", "")
    header = f"🎬 *Похожее на {source_title}"
    if source_year:
        header += f" ({source_year})"
    header += ":*"
    if filtered_out:
        header += f"\n_Исключено из вашего списка: {filtered_out} фильмов_"

    parts = [header, ""]

    keyboard = []
    context.user_data["tmdb_results"] = {}

    for movie in similar:
        tmdb_id = movie["tmdb_id"]
        title = movie["title"]
        year = movie.get("year", "")
        rating = movie.get("rating", 0)
        genres = movie.get("genres", "")
        overview = movie.get("overview", "")
        note = movie.get("note", "")

        line = f"• *{title}*"
        if year:
            line += f" ({year})"
        if rating:
            line += f" ⭐{rating:.1f}"
        if genres:
            line += f"\n  _{genres}_"
        if overview:
            short = overview[:120] + "..." if len(overview) > 120 else overview
            line += f"\n  {short}"
        if note:
            line += f"\n  ⚡ {note}"
        parts.append(line)

        context.user_data["tmdb_results"][str(tmdb_id)] = {
            "id": tmdb_id,
            "title": title,
            "release_date": f"{year}-01-01" if year else "",
            "vote_average": rating,
        }
        keyboard.append([InlineKeyboardButton(f"➕ {title}", callback_data=f"tmdb_add_{tmdb_id}")])

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


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync movies with TMDB."""
    chat_id = update.effective_chat.id
    args = context.args or []
    
    # Parse arguments
    sync_all = "-a" in args  # Sync even already linked movies
    sync_watched = "-w" in args  # Sync watched movies
    movie_num = None
    
    # Check if specific movie number provided
    for arg in args:
        if arg.isdigit():
            movie_num = int(arg)
            break
    
    # Get movies based on status
    if sync_watched:
        movies = get_movies_db(chat_id, "watched")
        status_name = "просмотренных"
    else:
        movies = get_movies_db(chat_id, "to_watch")
        status_name = "к просмотру"
    
    if not movies:
        await update.message.reply_text(f"📭 Список {status_name} пуст!")
        return
    
    # Get movies to sync
    if movie_num:
        # Sync specific movie
        if movie_num < 1 or movie_num > len(movies):
            await update.message.reply_text(f"❌ Номер должен быть 1-{len(movies)}")
            return
        movies_to_sync = [movies[movie_num - 1]]
        start_index = movie_num - 1
    else:
        # Sync all unlinked (or all with -a)
        if sync_all:
            movies_to_sync = movies
        else:
            movies_to_sync = [m for m in movies if not m.get("tmdb_id")]
        start_index = 0
    
    if not movies_to_sync:
        await update.message.reply_text("✅ Все фильмы уже связаны с TMDB!")
        return
    
    # Store sync state
    context.user_data["sync_movies"] = movies_to_sync
    context.user_data["sync_index"] = start_index
    context.user_data["sync_chat_id"] = chat_id
    context.user_data["sync_status"] = "watched" if sync_watched else "to_watch"
    
    # Start syncing first movie
    await show_sync_movie(update.message, context, start_index)


async def show_sync_movie(message, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    """Show TMDB search results for movie to sync."""
    movies_to_sync = context.user_data.get("sync_movies", [])
    
    if index >= len(movies_to_sync):
        await message.reply_text("✅ Синхронизация завершена!")
        context.user_data.pop("sync_movies", None)
        context.user_data.pop("sync_index", None)
        context.user_data.pop("sync_chat_id", None)
        return
    
    movie = movies_to_sync[index]
    progress = f"{index + 1}/{len(movies_to_sync)}"
    
    # Search TMDB
    if not TMDB_API_KEY:
        await message.reply_text("❌ TMDB API не настроен")
        return
    
    # Parse title and year from movie title
    title, year = parse_movie_query(movie["title"])
    
    search_data = await tmdb_search(title, page=1, year=year)
    results = search_data.get("results", [])
    
    if not results:
        # No results - show skip/stop
        keyboard = [
            [InlineKeyboardButton("⏭ Пропустить", callback_data=f"sync_skip_{index}")],
            [InlineKeyboardButton("❌ Стоп", callback_data="sync_stop")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            f"🔍 Синхронизация ({progress})\n\n"
            f"*{movie['title']}*\n\n"
            f"❌ Не найдено в TMDB",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return
    
    # Store sync search data
    context.user_data["tmdb_search_query"] = title
    context.user_data["tmdb_search_year"] = year
    context.user_data["tmdb_search_mode"] = "sync"
    context.user_data["sync_current_movie_id"] = movie["id"]
    
    # Show results with pagination
    await show_sync_tmdb_results(message, context, search_data, index, progress)


async def show_sync_tmdb_results(message, context: ContextTypes.DEFAULT_TYPE, search_data: dict, sync_index: int, progress: str) -> None:
    """Show TMDB search results for sync with pagination."""
    results = search_data.get("results", [])[:5]
    page = search_data.get("page", 1)
    total_pages = search_data.get("total_pages", 1)
    
    # Show search results
    keyboard = []
    context.user_data["sync_tmdb_results"] = {}
    
    for tmdb_movie in results:
        year = tmdb_movie.get("release_date", "")[:4]
        rating = tmdb_movie.get("vote_average", 0)
        title = tmdb_movie.get("title", "Unknown")
        
        btn_text = f"{title}"
        if year:
            btn_text += f" ({year})"
        if rating:
            btn_text += f" ⭐{rating:.1f}"
        
        callback_data = f"sync_select_{sync_index}_{tmdb_movie['id']}"
        context.user_data["sync_tmdb_results"][str(tmdb_movie['id'])] = tmdb_movie
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
    
    # Pagination row for sync
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️ Пред", callback_data=f"sync_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("След ▶️", callback_data=f"sync_page_{page + 1}"))
        keyboard.append(nav_row)
    
    # Action buttons
    keyboard.append([
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"sync_skip_{sync_index}"),
        InlineKeyboardButton("❌ Стоп", callback_data="sync_stop")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    movies_to_sync = context.user_data.get("sync_movies", [])
    movie = movies_to_sync[sync_index]
    
    header = f"🔍 Синхронизация ({progress})\n\n*{movie['title']}*\n\nНайдено в TMDB"
    if total_pages > 1:
        header += f" (стр. {page}/{total_pages})"
    header += ":"
    
    await message.reply_text(
        header,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def sync_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle sync actions."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = context.user_data.get("sync_chat_id")
    
    if data.startswith("sync_page_"):
        # Handle pagination in sync
        page = int(data.replace("sync_page_", ""))
        search_query = context.user_data.get("tmdb_search_query")
        search_year = context.user_data.get("tmdb_search_year")
        sync_index = context.user_data.get("sync_index", 0)
        
        if search_query:
            search_data = await tmdb_search(search_query, page=page, year=search_year)
            movies_to_sync = context.user_data.get("sync_movies", [])
            progress = f"{sync_index + 1}/{len(movies_to_sync)}"
            
            # Update results
            results = search_data.get("results", [])[:5]
            page_num = search_data.get("page", 1)
            total_pages = search_data.get("total_pages", 1)
            
            keyboard = []
            context.user_data["sync_tmdb_results"] = {}
            
            for tmdb_movie in results:
                year = tmdb_movie.get("release_date", "")[:4]
                rating = tmdb_movie.get("vote_average", 0)
                title = tmdb_movie.get("title", "Unknown")
                
                btn_text = f"{title}"
                if year:
                    btn_text += f" ({year})"
                if rating:
                    btn_text += f" ⭐{rating:.1f}"
                
                callback_data = f"sync_select_{sync_index}_{tmdb_movie['id']}"
                context.user_data["sync_tmdb_results"][str(tmdb_movie['id'])] = tmdb_movie
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
            
            # Pagination row
            if total_pages > 1:
                nav_row = []
                if page_num > 1:
                    nav_row.append(InlineKeyboardButton("◀️ Пред", callback_data=f"sync_page_{page_num - 1}"))
                nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
                if page_num < total_pages:
                    nav_row.append(InlineKeyboardButton("След ▶️", callback_data=f"sync_page_{page_num + 1}"))
                keyboard.append(nav_row)
            
            # Action buttons
            keyboard.append([
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"sync_skip_{sync_index}"),
                InlineKeyboardButton("❌ Стоп", callback_data="sync_stop")
            ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            movie = movies_to_sync[sync_index]
            header = f"🔍 Синхронизация ({progress})\n\n*{movie['title']}*\n\nНайдено в TMDB"
            if total_pages > 1:
                header += f" (стр. {page_num}/{total_pages})"
            header += ":"
            
            await query.edit_message_text(header, reply_markup=reply_markup, parse_mode="Markdown")
        return
    
    if data.startswith("sync_select_"):
        # Format: sync_select_index_tmdbid
        parts = data.replace("sync_select_", "").split("_")
        index = int(parts[0])
        tmdb_id = parts[1]
        
        movies_to_sync = context.user_data.get("sync_movies", [])
        movie = movies_to_sync[index]
        tmdb_movie = context.user_data.get("sync_tmdb_results", {}).get(tmdb_id)
        
        if tmdb_movie:
            # Update movie with TMDB data
            year = int(tmdb_movie.get("release_date", "0000")[:4]) if tmdb_movie.get("release_date") else None
            rating = tmdb_movie.get("vote_average")
            poster_path = tmdb_movie.get("poster_path")
            genres = ",".join(map(str, tmdb_movie.get("genre_ids", [])))
            
            success = update_movie_tmdb_data(
                chat_id, movie["id"], int(tmdb_id),
                year=year, rating=rating, poster_path=poster_path, genres=genres
            )
            
            if success:
                await query.answer(f"✅ {movie['title']} обновлен!", show_alert=True)
                
                # Show next movie
                context.user_data["sync_index"] = index + 1
                await query.edit_message_text(
                    f"✅ *{movie['title']}* синхронизирован с TMDB!",
                    parse_mode="Markdown"
                )
                await show_sync_movie(query.message, context, index + 1)
            else:
                await query.answer("❌ Ошибка обновления", show_alert=True)
        else:
            await query.answer("❌ Фильм не найден", show_alert=True)
    
    elif data.startswith("sync_skip_"):
        index = int(data.replace("sync_skip_", ""))
        movies_to_sync = context.user_data.get("sync_movies", [])
        movie = movies_to_sync[index]
        
        await query.edit_message_text(f"⏭ *{movie['title']}* пропущен", parse_mode="Markdown")
        await show_sync_movie(query.message, context, index + 1)
    
    elif data == "sync_stop":
        context.user_data.pop("sync_movies", None)
        context.user_data.pop("sync_index", None)
        context.user_data.pop("sync_chat_id", None)
        context.user_data.pop("sync_tmdb_results", None)
        await query.edit_message_text("❌ Синхронизация остановлена")


async def export_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export movie list to text or CSV file."""
    chat_id = update.effective_chat.id
    args = context.args or []
    
    # Check if CSV format requested
    export_csv = "-csv" in args or "csv" in args
    
    movies = get_movies_db(chat_id)
    
    if not movies:
        await update.message.reply_text("📭 Список пуст!")
        return
    
    to_watch = [m for m in movies if m["status"] == "to_watch"]
    watched = [m for m in movies if m["status"] == "watched"]
    
    if export_csv:
        # CSV format
        import csv
        from io import StringIO
        
        output = StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow(["Status", "Title", "Year", "Rating", "TMDB ID", "Genres", "Added By", "Added At", "Watched By", "Watched At"])
        
        # To watch movies
        for movie in to_watch:
            writer.writerow([
                "To Watch",
                movie["title"],
                movie.get("year") or "",
                movie.get("rating") or "",
                movie.get("tmdb_id") or "",
                movie.get("genres") or "",
                movie.get("added_by") or "",
                movie.get("added_at").strftime("%Y-%m-%d %H:%M:%S") if movie.get("added_at") else "",
                "",
                ""
            ])
        
        # Watched movies
        for movie in watched:
            writer.writerow([
                "Watched",
                movie["title"],
                movie.get("year") or "",
                movie.get("rating") or "",
                movie.get("tmdb_id") or "",
                movie.get("genres") or "",
                movie.get("added_by") or "",
                movie.get("added_at").strftime("%Y-%m-%d %H:%M:%S") if movie.get("added_at") else "",
                movie.get("watched_by") or "",
                movie.get("watched_at").strftime("%Y-%m-%d %H:%M:%S") if movie.get("watched_at") else ""
            ])
        
        content = output.getvalue()
        file = BytesIO(content.encode("utf-8"))
        file.name = "watchlist.csv"
        
        await update.message.reply_document(file, caption=f"📊 Экспорт: {len(to_watch)} к просмотру, {len(watched)} просмотрено")
    
    else:
        # Text format
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


async def handle_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text message for renaming movie."""
    movie_id = context.user_data.get("rename_movie_id")
    
    if not movie_id:
        return  # Not in rename mode
    
    chat_id = update.effective_chat.id
    new_title = update.message.text.strip()
    
    if not new_title:
        await update.message.reply_text("❌ Название не может быть пустым")
        return
    
    success, old_title = rename_movie_by_id(chat_id, movie_id, new_title)
    
    if success:
        # Add "Back to list" button
        keyboard = [[InlineKeyboardButton("◀️ К списку", callback_data="back_to_list")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✏️ Переименовано:\n*{old_title}* → *{new_title}*",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data.pop("rename_movie_id", None)
    else:
        if old_title:
            await update.message.reply_text(
                f"❌ Фильм *{new_title}* уже есть в списке",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❌ Фильм не найден")
            context.user_data.pop("rename_movie_id", None)


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
    application.add_handler(CommandHandler("watched", mark_watched))
    application.add_handler(CommandHandler("remove", remove_movie))
    application.add_handler(CommandHandler("rename", rename_movie))
    application.add_handler(CommandHandler("list", list_movies))
    application.add_handler(CommandHandler("pages", pages_command))
    application.add_handler(CommandHandler("wlist", wlist_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("random", random_movie))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CommandHandler("vote", vote_poll))
    application.add_handler(CommandHandler("rpoll", random_from_selection))
    application.add_handler(CommandHandler("suggest", suggest_movies))
    application.add_handler(CommandHandler("similar", similar_movies))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("export", export_list))
    
    # Vote basket
    application.add_handler(MessageHandler(filters.Regex(r'^/v\+'), basket_add_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^/v-'), basket_remove_handler))
    application.add_handler(CommandHandler("vmy", basket_my))
    application.add_handler(CommandHandler("vlist", basket_list))
    application.add_handler(CommandHandler("go", basket_go))
    application.add_handler(CommandHandler("vrand", basket_random))
    application.add_handler(CommandHandler("vc", basket_clear))
    
    # Text handler for rename (must be after commands)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rename_text))
    
    # Callbacks
    application.add_handler(CallbackQueryHandler(tmdb_add_callback, pattern=r"^(tmdb_add_|tmdb_page_|add_manual_)"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^(page_|list_|lpage_|movie_|noop)"))
    application.add_handler(CallbackQueryHandler(movie_action_callback, pattern=r"^(w_|d_|r_|cancel_rename|back_to_list|back_pages)"))
    application.add_handler(CallbackQueryHandler(watched_callback, pattern=r"^(wpage_|wmovie_)"))
    application.add_handler(CallbackQueryHandler(watched_action_callback, pattern=r"^(unw_|wd_|back_wlist)"))
    application.add_handler(CallbackQueryHandler(sync_callback, pattern=r"^(sync_select_|sync_skip_|sync_page_|sync_stop)"))
    
    print("🎬 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
