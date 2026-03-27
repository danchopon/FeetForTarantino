# FeetForTarantino — Movie Watchlist Telegram Bot

Telegram бот для группового управления вотчлистом фильмов с голосованием и умными рекомендациями.

## Стек

- **Python 3.14** + `python-telegram-bot>=20`
- **PostgreSQL** (локально на MacBook, база `movie_bot`)
- **TMDB API** — поиск, обогащение данных, рекомендации
- **MCP сервер** (`tmdb_mcp_server.py`) — умные рекомендации через Model Context Protocol

## Запуск

```bash
./run.sh          # активирует venv и запускает бота
```

Или вручную:
```bash
source venv/bin/activate
python movie_watchlist_bot.py
```

## Переменные окружения (`.env`, не в git)

```
TELEGRAM_BOT_TOKEN=...
TMDB_API_KEY=...
DATABASE_URL=postgresql://daniiar.erkinov@localhost:5432/movie_bot
```

## Архитектура

```
movie_watchlist_bot.py   — основной бот (Telegram handlers, DB, TMDB)
tmdb_mcp_server.py       — MCP сервер с инструментами recommend/similar
run.sh                   — скрипт запуска
venv/                    — виртуальное окружение Python
.env                     — секреты (в gitignore)
```

### MCP сервер

Запускается ботом как subprocess (stdio транспорт) через `call_mcp_tool()`.
Инструменты:
- `suggest_movies(chat_id, mood)` — рекомендации по настроению + история просмотров группы
- `find_similar(chat_id, movie_title)` — похожие фильмы, без уже добавленных

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
| `/suggest [настроение]` | умные рекомендации через MCP |
| `/similar Название` | похожие фильмы через MCP |
| `/sync` | синхронизировать данные с TMDB |
| `/export` | экспорт в .txt или .csv |
| `/v+ 1,5` `/v-` `/go` | корзина голосования |

## Известные нюансы

- `init_db()` использует `SAVEPOINT` для миграций (не `rollback`) — иначе `rollback` откатывал бы и `CREATE TABLE`
- MCP сервер читает `.env` через `python-dotenv` — нужно запускать из папки проекта
- При поиске TMDB: сначала ru-RU, если < 5 результатов — добавляет en-US, дедупликация по tmdb_id
- История: был на Railway PostgreSQL, переехали на локальный Mac. В будущем — свой сервер
