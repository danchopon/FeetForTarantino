"""Polling commands: /poll, /vote, /rpoll."""

import random

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import get_movies_db


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
