"""AI-powered recommendations: /rec command."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.groq_ai import get_rec_suggestions

logger = logging.getLogger(__name__)


async def rec_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """AI-powered recommendations — auto-detects intent via Groq (Llama 3.3 70B).

    Usage:
      /rec                    — by watch history
      /rec мрачный триллер    — by mood/vibe
      /rec как Inception      — similar to a movie
    """
    chat_id = update.effective_chat.id
    query = " ".join(context.args) if context.args else ""

    await update.message.reply_text("🤖 Подбираю рекомендации...")

    try:
        result = await get_rec_suggestions(chat_id, query)
    except RuntimeError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except Exception as e:
        logger.error(f"rec_command error: {e}")
        await update.message.reply_text("❌ Ошибка при обращении к AI. Попробуй позже.")
        return

    suggestions = result.get("suggestions", [])
    if not suggestions:
        await update.message.reply_text("❌ AI не смог подобрать рекомендации. Попробуй другой запрос.")
        return

    intent = result.get("intent", "history")
    source_movie = result.get("source_movie")

    if intent == "similar" and source_movie:
        header = f"🎬 *Похожее на {source_movie}:*"
    elif intent == "mood" and query:
        header = f"🎯 *Рекомендации по запросу «{query}»:*"
    else:
        header = "🤖 *Рекомендации на основе истории группы:*"

    parts = [header, ""]
    keyboard = []
    context.user_data["tmdb_results"] = {}

    for movie in suggestions:
        tmdb_id = movie.get("tmdb_id")
        title = movie["title"]
        year = movie.get("year", "")
        rating = movie.get("rating", 0)
        reason = movie.get("reason", "")
        overview = movie.get("overview", "")

        line = f"• *{title}*"
        if year:
            line += f" ({year})"
        if rating:
            line += f" ⭐{rating:.1f}"
        if reason:
            line += f"\n  _{reason}_"
        if overview:
            short = overview[:120] + "..." if len(overview) > 120 else overview
            line += f"\n  {short}"
        parts.append(line)

        if tmdb_id:
            context.user_data["tmdb_results"][str(tmdb_id)] = {
                "id": tmdb_id,
                "title": title,
                "release_date": f"{year}-01-01" if year else "",
                "vote_average": rating,
            }
            keyboard.append([InlineKeyboardButton(f"➕ {title}", callback_data=f"tmdb_add_{tmdb_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=reply_markup)
