# FeetForTarantino — Movie Watchlist Telegram Bot

Telegram бот для группового управления вотчлистом фильмов с голосованием и умными рекомендациями.

## Стек

- **Python 3.14** + `python-telegram-bot>=20`
- **PostgreSQL** (локально на MacBook, база `movie_bot`)
- **TMDB API** — поиск, обогащение данных, рекомендации
- **Groq API** (`bot/groq_ai.py`) — AI-рекомендации через Llama 3.3 70B (команда `/rec`)
- **FastAPI** (`api.py`) — REST API поверх той же БД, для iOS приложения

## Запуск

```bash
./run.sh          # активирует venv и запускает бота
```

Или вручную:
```bash
source venv/bin/activate
python movie_watchlist_bot.py
```

### FastAPI сервер (для iOS)

```bash
source venv/bin/activate
uvicorn api:app --reload --port 8000
```

Документация доступна по адресу: **http://localhost:8000/docs**

Бот и API можно запускать одновременно — оба работают с одной БД.

## Переменные окружения (`.env`, не в git)

```
TELEGRAM_BOT_TOKEN=...
TMDB_API_KEY=...
DATABASE_URL=postgresql://daniiar.erkinov@localhost:5432/movie_bot
GROQ_API_KEY=...
```

## Архитектура

### Структура модулей

```
bot/
├── __init__.py
├── db.py              — слой PostgreSQL (22 функции: movies CRUD + vote basket)
├── tmdb_api.py        — TMDB HTTP запросы (5 функций + константы TMDB_API_KEY, TMDB_BASE_URL)
├── groq_ai.py         — Groq AI: get_rec_suggestions() — авто-определяет intent (similar/mood/history)
├── utils.py           — format_movie()
├── ui_helpers.py      — UI-хелперы: show_page, show_movie_detail, show_tmdb_results и др. (8 функций)
└── handlers/
    ├── __init__.py
    ├── basic.py       — /start, /help, /add, /list, /pages, /wlist, /info, /random + колбэки навигации
    ├── movie_actions.py — /watched, /remove, /rename, /export + колбэки действий над фильмами
    ├── polling.py     — /poll, /vote, /rpoll
    ├── basket.py      — /v+, /v-, /vmy, /vlist, /go, /vrand, /vc
    ├── ai_features.py — /rec (Groq)
    └── sync.py        — /sync и его колбэки

movie_watchlist_bot.py  — только main() + регистрация хэндлеров
api.py                  — FastAPI REST API для iOS приложения
run.sh                  — скрипт запуска бота
venv/                   — виртуальное окружение Python
.env                    — секреты (в gitignore)
```

### Groq AI модуль (`bot/groq_ai.py`)

Используется командой `/rec`. Автоматически определяет intent по запросу:
- `similar` — запрос содержит название фильма ("как Inception", "похожее на Начало")
- `mood` — описание настроения/жанра ("мрачный триллер", "что-то смешное")
- `history` — пустой запрос → рекомендации на основе истории группы

Модель: `llama-3.3-70b-versatile` (Groq).
Получает историю просмотров и вотчлист из БД → LLM возвращает JSON → TMDB обогащает данными.
Переменная окружения: `GROQ_API_KEY`

### База данных

Две таблицы:

**movies**
| поле | тип | описание |
|------|-----|----------|
| id | SERIAL | PK |
| chat_id | BIGINT | ID Telegram чата |
| title | VARCHAR | название |
| status | VARCHAR | `to_watch` / `watched` |
| added_by / watched_by | VARCHAR | имя пользователя |
| tmdb_id, year, rating, poster_path, genres | — | данные из TMDB |

**vote_basket** — корзина голосования (chat_id, user_id, movie_num)

## Команды бота

| команда | описание |
|---------|----------|
| `/add название` | добавить фильм с поиском TMDB |
| `/add\nФильм1\nФильм2` | пакетное добавление |
| `/list` | список к просмотру (пагинация + кнопки) |
| `/wlist` | просмотренные |
| `/watched 5` | отметить #5 просмотренным |
| `/remove 5` | удалить |
| `/rename 5 Название` | переименовать |
| `/info 5` | инфо о фильме |
| `/random` | случайный фильм |
| `/poll N` | Telegram poll из N случайных |
| `/rec [запрос]` | AI-рекомендации (Groq): по истории, настроению или похожее на фильм |
| `/sync` | синхронизировать данные с TMDB |
| `/export` | экспорт в .txt или .csv |
| `/v+ 1,5` `/v-` `/go` | корзина голосования |

## FastAPI (`api.py`)

REST API поверх `bot/db.py` для iOS приложения. Запускается отдельно от бота.

### Endpoints

| метод | путь | описание |
|-------|------|----------|
| `GET` | `/movies?chat_id=X` | все фильмы чата |
| `GET` | `/movies?chat_id=X&status=to_watch` | только вотчлист |
| `GET` | `/movies?chat_id=X&status=watched` | только просмотренные |
| `GET` | `/movies/{id}?chat_id=X` | один фильм |
| `POST` | `/movies` | добавить фильм |
| `PATCH` | `/movies/{id}/watched` | отметить просмотренным |
| `PATCH` | `/movies/{id}/unwatch` | вернуть в вотчлист |
| `PATCH` | `/movies/{id}/rename` | переименовать |
| `DELETE` | `/movies/{id}?chat_id=X` | удалить |
| `GET` | `/stats?chat_id=X` | счётчики to_watch / watched |
| `GET` | `/search?q=Inception` | поиск через TMDB |
| `GET` | `/recommendations?chat_id=X&q=` | AI рекомендации (Groq) |

### Архитектура

`api.py` импортирует функции напрямую из `bot/db.py` и `bot/tmdb_api.py` — никакого дублирования кода.
CORS открыт для всех origins (локальная разработка). При деплое ограничить до iOS app domain.

## Известные нюансы

- `init_db()` использует `SAVEPOINT` для миграций (не `rollback`) — иначе `rollback` откатывал бы и `CREATE TABLE`
- При поиске TMDB: сначала ru-RU, если < 5 результатов — добавляет en-US, дедупликация по tmdb_id
- История: был на Railway PostgreSQL, переехали на локальный Mac. В будущем — свой сервер
- FastAPI вызывает `init_db()` при старте через `lifespan` — таблицы создаются автоматически
