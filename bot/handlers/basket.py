"""Vote basket commands: /v+, /v-, /vmy, /vlist, /go, /vrand, /vc."""

import random

from telegram import Update
from telegram.ext import ContextTypes

from bot.db import (
    get_movies_db,
    add_to_basket, remove_from_basket, clear_basket,
    get_user_basket, get_full_basket, get_unique_basket_movies,
)


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
