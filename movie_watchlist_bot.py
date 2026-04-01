#!/usr/bin/env python3
"""
Telegram Movie Watchlist Bot
With TMDB integration, inline buttons, PostgreSQL storage.
AI recommendations via Groq (Llama 3.3 70B).
"""

import os
import logging

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from bot.db import init_db
from bot.handlers.basic import (
    start, help_command, add_movie, batch_add,
    list_movies, pages_command, wlist_command,
    info_command, random_movie,
    tmdb_add_callback, page_callback, watched_callback,
)
from bot.handlers.movie_actions import (
    mark_watched, remove_movie, rename_movie, export_list,
    movie_action_callback, watched_action_callback,
    handle_rename_text,
)
from bot.handlers.polling import create_poll, vote_poll, random_from_selection
from bot.handlers.basket import (
    basket_add_handler, basket_remove_handler,
    basket_my, basket_list, basket_go, basket_random, basket_clear,
)
from bot.handlers.ai_features import rec_command
from bot.handlers.sync import sync_command, sync_callback

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


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
    application.add_handler(CommandHandler("rec", rec_command))
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
