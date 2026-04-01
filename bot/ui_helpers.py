"""UI display helpers for Telegram bot — pagination, movie details, search results."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db import get_movies_db, get_movie_by_id
from bot.tmdb_api import tmdb_search, parse_movie_query, TMDB_API_KEY
from bot.utils import format_movie


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
            btn_text += f" \u2b50{rating:.1f}"

        callback_data = f"tmdb_add_{movie['id']}"
        context.user_data["tmdb_results"][str(movie['id'])] = movie
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    # Pagination row
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("\u25c0\ufe0f \u041f\u0440\u0435\u0434", callback_data=f"tmdb_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("\u0421\u043b\u0435\u0434 \u25b6\ufe0f", callback_data=f"tmdb_page_{page + 1}"))
        keyboard.append(nav_row)

    # Add manual option
    keyboard.append([InlineKeyboardButton(f"\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043a\u0430\u043a \"{query}\"", callback_data=f"add_manual_{query[:50]}")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    header = "\U0001f50d \u041d\u0430\u0439\u0434\u0435\u043d\u043e \u0432 TMDB"
    if total_pages > 1:
        header += f" (\u0441\u0442\u0440. {page}/{total_pages})"
    header += ":"

    await message.reply_text(header, reply_markup=reply_markup)


async def show_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show a page of movies with buttons."""
    to_watch = get_movies_db(chat_id, "to_watch")

    if not to_watch:
        text = "\U0001f4ed \u0421\u043f\u0438\u0441\u043e\u043a \u043f\u0443\u0441\u0442! \u0414\u043e\u0431\u0430\u0432\u044c \u0444\u0438\u043b\u044c\u043c\u044b \u0447\u0435\u0440\u0435\u0437 /add"
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
    parts = [f"\U0001f4cb *\u041a \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0443* (\u0441\u0442\u0440. {page + 1}/{total_pages}):\n"]

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
        nav_row.append(InlineKeyboardButton("\u25c0\ufe0f", callback_data=f"page_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("\u25b6\ufe0f", callback_data=f"page_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(" ", callback_data="noop"))

    keyboard.append(nav_row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def show_movie_detail(query, movie: dict, chat_id: int) -> None:
    """Show movie detail with action buttons."""
    parts = [f"\U0001f3ac *{movie['title']}*\n"]

    if movie.get("year"):
        parts.append(f"\U0001f4c5 \u0413\u043e\u0434: {movie['year']}")
    if movie.get("rating"):
        parts.append(f"\u2b50 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: {movie['rating']:.1f}")
    if movie.get("added_by"):
        parts.append(f"\U0001f464 \u0414\u043e\u0431\u0430\u0432\u0438\u043b: {movie['added_by']}")
    if movie.get("added_at"):
        parts.append(f"\U0001f4c6 \u041a\u043e\u0433\u0434\u0430: {movie['added_at'].strftime('%d.%m.%Y')}")

    keyboard = [
        [
            InlineKeyboardButton("\u2705 \u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u043d\u043e", callback_data=f"w_{movie['id']}"),
            InlineKeyboardButton("\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"d_{movie['id']}")
        ],
        [InlineKeyboardButton("\u270f\ufe0f \u041f\u0435\u0440\u0435\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u0442\u044c", callback_data=f"r_{movie['id']}")],
        [InlineKeyboardButton("\u25c0\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="back_pages")]
    ]

    await query.edit_message_text("\n".join(parts), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_list_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show list page for back_to_list button."""
    to_watch = get_movies_db(chat_id, "to_watch")

    if not to_watch:
        text = "\U0001f4ed \u0421\u043f\u0438\u0441\u043e\u043a \u043f\u0443\u0441\u0442!"
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
    header = f"\U0001f4cb *\u041a \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0443* (\u0441\u0442\u0440. {page}/{total_pages}):\n"
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
        nav_row.append(InlineKeyboardButton("\u25c0\ufe0f", callback_data=f"list_{page - 1}_10__False"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("\u25b6\ufe0f", callback_data=f"list_{page + 1}_10__False"))
    keyboard.append(nav_row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def show_watched_page(message, chat_id: int, page: int, edit: bool = False) -> None:
    """Show a page of watched movies with buttons."""
    watched = get_movies_db(chat_id, "watched")

    if not watched:
        text = "\U0001f4ed \u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u043d\u043d\u044b\u0445 \u0444\u0438\u043b\u044c\u043c\u043e\u0432 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442"
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
    parts = [f"\u2705 *\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u043d\u043d\u044b\u0435 \u0444\u0438\u043b\u044c\u043c\u044b* (\u0441\u0442\u0440. {page + 1}/{total_pages}):\n"]

    for i, movie in enumerate(page_movies, start_idx + 1):
        line = f"{i}. {movie['title']}"
        if movie.get("year"):
            line += f" ({movie['year']})"
        if movie.get("rating"):
            line += f" \u2b50{movie['rating']:.1f}"
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
        nav_row.append(InlineKeyboardButton("\u25c0\ufe0f", callback_data=f"wpage_{page + 1}"))

    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("\u25b6\ufe0f", callback_data=f"wpage_{page + 2}"))

    keyboard.append(nav_row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)


async def show_watched_movie_detail(query, movie: dict, chat_id: int) -> None:
    """Show watched movie detail with action buttons."""
    parts = [f"\U0001f3ac *{movie['title']}*\n"]

    if movie.get("year"):
        parts.append(f"\U0001f4c5 \u0413\u043e\u0434: {movie['year']}")
    if movie.get("rating"):
        parts.append(f"\u2b50 \u0420\u0435\u0439\u0442\u0438\u043d\u0433: {movie['rating']:.1f}")
    if movie.get("watched_by"):
        parts.append(f"\u2705 \u0421\u043c\u043e\u0442\u0440\u0435\u043b: {movie['watched_by']}")
    if movie.get("watched_at"):
        parts.append(f"\U0001f4c6 \u041a\u043e\u0433\u0434\u0430: {movie['watched_at'].strftime('%d.%m.%Y')}")

    keyboard = [
        [
            InlineKeyboardButton("\u21a9\ufe0f \u0412\u0435\u0440\u043d\u0443\u0442\u044c \u0432 \u0441\u043f\u0438\u0441\u043e\u043a", callback_data=f"unw_{movie['id']}"),
            InlineKeyboardButton("\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"wd_{movie['id']}")
        ],
        [InlineKeyboardButton("\u25c0\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="back_wlist")]
    ]

    await query.edit_message_text("\n".join(parts), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_sync_movie(message, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    """Show TMDB search results for movie to sync."""
    movies_to_sync = context.user_data.get("sync_movies", [])

    if index >= len(movies_to_sync):
        await message.reply_text("\u2705 \u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430!")
        context.user_data.pop("sync_movies", None)
        context.user_data.pop("sync_index", None)
        context.user_data.pop("sync_chat_id", None)
        return

    movie = movies_to_sync[index]
    progress = f"{index + 1}/{len(movies_to_sync)}"

    # Search TMDB
    if not TMDB_API_KEY:
        await message.reply_text("\u274c TMDB API \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d")
        return

    # Parse title and year from movie title
    title, year = parse_movie_query(movie["title"])

    search_data = await tmdb_search(title, page=1, year=year)
    results = search_data.get("results", [])

    if not results:
        # No results - show skip/stop
        keyboard = [
            [InlineKeyboardButton("\u23ed \u041f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u0442\u044c", callback_data=f"sync_skip_{index}")],
            [InlineKeyboardButton("\u274c \u0421\u0442\u043e\u043f", callback_data="sync_stop")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            f"\U0001f50d \u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f ({progress})\n\n"
            f"*{movie['title']}*\n\n"
            f"\u274c \u041d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e \u0432 TMDB",
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
            btn_text += f" \u2b50{rating:.1f}"

        callback_data = f"sync_select_{sync_index}_{tmdb_movie['id']}"
        context.user_data["sync_tmdb_results"][str(tmdb_movie['id'])] = tmdb_movie
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

    # Pagination row for sync
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("\u25c0\ufe0f \u041f\u0440\u0435\u0434", callback_data=f"sync_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("\u0421\u043b\u0435\u0434 \u25b6\ufe0f", callback_data=f"sync_page_{page + 1}"))
        keyboard.append(nav_row)

    # Action buttons
    keyboard.append([
        InlineKeyboardButton("\u23ed \u041f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u0442\u044c", callback_data=f"sync_skip_{sync_index}"),
        InlineKeyboardButton("\u274c \u0421\u0442\u043e\u043f", callback_data="sync_stop")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    movies_to_sync = context.user_data.get("sync_movies", [])
    movie = movies_to_sync[sync_index]

    header = f"\U0001f50d \u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f ({progress})\n\n*{movie['title']}*\n\n\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u0432 TMDB"
    if total_pages > 1:
        header += f" (\u0441\u0442\u0440. {page}/{total_pages})"
    header += ":"

    await message.reply_text(
        header,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
