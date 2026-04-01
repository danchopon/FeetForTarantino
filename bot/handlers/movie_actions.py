"""Movie action commands: /watched, /remove, /rename, /export + action callbacks."""

from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db import (
    get_movie_by_id, get_movies_db, get_counts_db,
    mark_watched_by_id, mark_watched_db, remove_movie_by_id, remove_movie_db,
    unwatch_movie_by_id, rename_movie_by_id,
)
from bot.ui_helpers import show_page, show_watched_page, show_list_page


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


# ── Callbacks ─────────────────────────────────────────────────────────────────


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
