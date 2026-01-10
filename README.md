# Movie Watchlist Bot — Railway Deploy

## Файлы

```
railway-deploy/
├── movie_watchlist_bot.py  # бот
├── requirements.txt        # зависимости
├── Procfile               # команда запуска
└── runtime.txt            # версия Python
```

## Деплой на Railway

### 1. Создай репозиторий на GitHub

Залей все файлы из этой папки в новый репозиторий.

### 2. Railway

1. Зайди на [railway.app](https://railway.app)
2. Войди через GitHub
3. **New Project** → **Deploy from GitHub repo**
4. Выбери свой репозиторий

### 3. Добавь токен бота

1. В Railway открой свой проект
2. Перейди во вкладку **Variables**
3. Добавь переменную:
   - Name: `TELEGRAM_BOT_TOKEN`
   - Value: `твой_токен_от_BotFather`

### 4. Готово!

Бот автоматически запустится. Логи можно смотреть во вкладке **Deployments**.

---

## Команды бота

- `/add название` — добавить фильм
- `/watched название` — отметить просмотренным  
- `/remove название` — удалить
- `/list` — список всех фильмов
- `/random` — случайный фильм
- `/poll N` — голосование (N фильмов)

---

## Важно

⚠️ Railway Free tier даёт $5/месяц кредитов — этого хватит для бота.

⚠️ Данные хранятся в `movie_data.json`. При редеплое файл сбросится. Для постоянного хранения нужна база данных (могу добавить).
