# Movie Watchlist Bot — Railway Deploy

## Файлы

```
railway-deploy/
├── movie_watchlist_bot.py  # бот
├── requirements.txt        # зависимости
├── Procfile               # команда запуска
├── nixpacks.toml          # конфиг сборки
└── runtime.txt            # версия Python
```

## Деплой на Railway

### 1. Создай репозиторий на GitHub

Залей все файлы из этой папки в новый репозиторий.

### 2. Railway — создай проект

1. Зайди на [railway.app](https://railway.app)
2. Войди через GitHub
3. **New Project** → **Deploy from GitHub repo**
4. Выбери свой репозиторий

### 3. Добавь PostgreSQL

1. В проекте нажми **+ New** → **Database** → **PostgreSQL**
2. Railway автоматически добавит `DATABASE_URL`

### 4. Добавь токен бота

1. Кликни на свой сервис (не на PostgreSQL)
2. Перейди во вкладку **Variables**
3. Добавь переменную:
   - Name: `TELEGRAM_BOT_TOKEN`
   - Value: `твой_токен_от_BotFather`

### 5. Свяжи сервисы

1. В Variables твоего сервиса нажми **+ Add Variable**
2. Выбери **Add Reference** → `DATABASE_URL` из PostgreSQL

### 6. Готово!

Бот автоматически запустится. Данные теперь хранятся в PostgreSQL и не пропадут при редеплое.

---

## Команды бота

- `/add название` — добавить фильм
- `/watched название` — отметить просмотренным  
- `/remove название` — удалить
- `/list` — список всех фильмов
- `/random` — случайный фильм
- `/poll N` — голосование (N фильмов)
