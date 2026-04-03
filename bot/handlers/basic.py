"""Basic bot commands: /start, /help, /add, /list, /pages, /wlist, /info, /random + their callbacks."""

import random
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db import (
    add_movie_db, get_movie_by_id, get_movies_db, get_counts_db,
)
from bot.tmdb_api import (
    parse_movie_query, tmdb_search,
    TMDB_API_KEY, TMDB_IMAGE_URL,
)
from bot.utils import format_movie
from bot.ui_helpers import (
    show_tmdb_results, show_page, show_movie_detail,
    show_watched_page, show_watched_movie_detail,
)


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
`/unwatched 5` — вернуть в список
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
`/rec` — AI-рекомендации по истории группы
`/rec мрачный триллер` — по настроению
`/rec как Inception` — похожее на фильм
`/sync` — синхронизация с TMDB
`/sync 5` — синхронизировать фильм #5
`/sync -a` — синхронизировать все
`/sync -w` — синхронизировать просмотренные
`/export` — экспорт .txt
`/export -csv` — экспорт .csv
`/app` — открыть список в iOS приложении
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

    # External links
    if movie.get("tmdb_id"):
        tmdb_id = movie["tmdb_id"]
        link_row = [InlineKeyboardButton("TMDB", url=f"https://www.themoviedb.org/movie/{tmdb_id}")]

        # Fetch IMDB ID from TMDB
        try:
            async with __import__("httpx").AsyncClient() as client:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids",
                    params={"api_key": TMDB_API_KEY},
                )
                if resp.status_code == 200:
                    imdb_id = resp.json().get("imdb_id")
                    if imdb_id:
                        link_row.append(InlineKeyboardButton("IMDB", url=f"https://www.imdb.com/title/{imdb_id}/"))
        except Exception:
            pass

        kp_query = movie["title"].replace(" ", "+")
        link_row.append(InlineKeyboardButton("Кинопоиск", url=f"https://www.kinopoisk.ru/index.php?kp_query={kp_query}"))
        keyboard.append(link_row)

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


async def app_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a deep link to open the watchlist in the iOS app."""
    chat = update.effective_chat
    chat_id = chat.id

    if chat.title:
        chat_name = chat.title
    else:
        chat_name = update.effective_user.first_name

    user = update.effective_user
    encoded_name = quote(chat_name)
    encoded_user_name = quote(user.first_name)
    deep_link = (
        f"https://danchopon.github.io/feetfortarantino/chat"
        f"?id={chat_id}&name={encoded_name}"
        f"&user_id={user.id}&user_name={encoded_user_name}"
    )

    keyboard = [[InlineKeyboardButton("📱 Открыть в приложении", url=deep_link)]]
    await update.message.reply_text(
        "Нажми кнопку чтобы открыть список в приложении:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Callbacks ─────────────────────────────────────────────────────────────────


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
