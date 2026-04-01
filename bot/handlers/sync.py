"""TMDB sync: /sync command and its callbacks."""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db import get_movies_db, update_movie_tmdb_data
from bot.tmdb_api import tmdb_search, parse_movie_query, TMDB_API_KEY
from bot.ui_helpers import show_sync_movie, show_sync_tmdb_results


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
