"""
FastAPI server for the movie watchlist bot.

Run locally:
    uvicorn api:app --reload --port 8000

Docs available at:
    http://localhost:8000/docs
"""

import asyncio
import json
import os
import random as _random
import secrets
import time
from contextlib import asynccontextmanager
from typing import Optional, List

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from telegram import Bot

load_dotenv()

_redis_client: aioredis.Redis | None = None


async def _redis_listener() -> None:
    """Subscribe to all chat_events:* channels and forward to WebSocket clients."""
    while True:
        pubsub = _redis_client.pubsub()
        try:
            await pubsub.psubscribe("chat_events:*")
            async for message in pubsub.listen():
                if message["type"] == "pmessage":
                    try:
                        event = json.loads(message["data"])
                        await manager.broadcast(event["chat_id"], event)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            await pubsub.aclose()
            return
        except Exception:
            await pubsub.aclose()
            await asyncio.sleep(2)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, chat_id: int, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(chat_id, []).append(ws)

    def disconnect(self, chat_id: int, ws: WebSocket):
        conns = self._connections.get(chat_id, [])
        if ws in conns:
            conns.remove(ws)

    async def broadcast(self, chat_id: int, event: dict):
        for ws in list(self._connections.get(chat_id, [])):
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(chat_id, ws)


manager = ConnectionManager()

from bot.db import (
    init_db,
    get_movies_db,
    get_movie_by_id,
    get_counts_db,
    add_movie_db,
    mark_watched_by_id,
    unwatch_movie_by_id,
    remove_movie_by_id,
    rename_movie_by_id,
    add_to_basket,
    remove_from_basket,
    clear_basket,
    get_user_basket,
    get_full_basket,
    get_unique_basket_movies,
)
from bot.tmdb_api import tmdb_search, parse_movie_query, tmdb_get_movie, tmdb_get_credits
from bot.groq_ai import get_rec_suggestions


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_client
    init_db()

    listener_task = None
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        _redis_client = aioredis.from_url(redis_url, decode_responses=True)
        listener_task = asyncio.create_task(_redis_listener())

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        bot = Bot(token=token)
        await bot.initialize()
        app.state.bot = bot
        app.state.bot_token = token
    else:
        app.state.bot = None
        app.state.bot_token = None

    yield

    if listener_task:
        listener_task.cancel()
    if _redis_client:
        await _redis_client.aclose()
    if app.state.bot:
        await app.state.bot.shutdown()


app = FastAPI(
    title="Movie Watchlist API",
    description=(
        "REST API for the **FeetForTarantino** Telegram movie watchlist bot.\n\n"
        "Every request is scoped to a Telegram `chat_id` — the group chat the watchlist belongs to.\n\n"
        "**Poster images:** `https://image.tmdb.org/t/p/w500` + `poster_path`"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── auth ──────────────────────────────────────────────────────────────────────

_SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
_OPEN_PATHS = {"/docs", "/openapi.json", "/redoc", "/auth/exchange"}


@app.middleware("http")
async def verify_session(request: Request, call_next):
    if request.url.path in _OPEN_PATHS or request.url.path.startswith("/ws/"):
        return await call_next(request)
    if not _redis_client:
        return await call_next(request)  # no Redis — skip auth (local dev)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    session_data = await _redis_client.get(f"session:{auth[7:]}")
    if not session_data:
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})
    session = json.loads(session_data)
    request.state.user_id = session["user_id"]
    request.state.user_name = session["user_name"]
    request.state.chat_id = session["chat_id"]
    request.state.chat_name = session["chat_name"]
    # Enforce chat_id / user_id query params match session
    qp = request.query_params
    if "chat_id" in qp:
        try:
            if int(qp["chat_id"]) != session["chat_id"]:
                return JSONResponse(status_code=403, content={"detail": "Access denied to this chat"})
        except ValueError:
            pass
    if "user_id" in qp:
        try:
            if int(qp["user_id"]) != session["user_id"]:
                return JSONResponse(status_code=403, content={"detail": "Cannot act as another user"})
        except ValueError:
            pass
    return await call_next(request)


class ExchangeRequest(BaseModel):
    token: str


class SessionResponse(BaseModel):
    session_token: str
    expires_in: int = _SESSION_TTL
    user_id: int
    user_name: str
    chat_id: int
    chat_name: str


@app.post(
    "/auth/exchange",
    summary="Exchange OTP for a session token",
    response_model=SessionResponse,
    responses={
        400: {"description": "Token missing"},
        404: {"description": "Token not found or expired"},
        503: {"description": "Redis not configured"},
    },
    tags=["Auth"],
)
async def auth_exchange(body: ExchangeRequest):
    """
    Called once when the iOS app opens via the Telegram deep link.

    Consumes the single-use OTP, creates a 30-day session, and returns
    the session token. Store it in the iOS Keychain and send it as
    `Authorization: Bearer <session_token>` on every subsequent request.
    """
    if not body.token:
        raise HTTPException(status_code=400, detail="Token required")
    if not _redis_client:
        raise HTTPException(status_code=503, detail="Auth service unavailable")
    payload_str = await _redis_client.getdel(f"otp:{body.token}")
    if not payload_str:
        raise HTTPException(status_code=404, detail="Token not found or expired")
    payload = json.loads(payload_str)
    session_token = secrets.token_urlsafe(32)
    await _redis_client.setex(f"session:{session_token}", _SESSION_TTL, json.dumps(payload))
    return SessionResponse(
        session_token=session_token,
        user_id=payload["user_id"],
        user_name=payload["user_name"],
        chat_id=payload["chat_id"],
        chat_name=payload["chat_name"],
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _serialize(row) -> dict:
    """Convert psycopg2 RealDictRow to a plain dict with serializable values."""
    if row is None:
        return None
    result = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):  # datetime
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# ── request / response schemas ────────────────────────────────────────────────

class AddMovieRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram group chat ID", examples=[-1001234567890])
    title: str = Field(..., description="Movie title", json_schema_extra={"example": "Inception"})
    added_by: str = Field("iOS", description="Name of the person adding the movie")
    tmdb_id: Optional[int] = Field(None, description="TMDB movie ID", json_schema_extra={"example": 27205})
    year: Optional[int] = Field(None, description="Release year", json_schema_extra={"example": 2010})
    rating: Optional[float] = Field(None, description="TMDB vote average (0–10)", json_schema_extra={"example": 8.4})
    poster_path: Optional[str] = Field(None, description="TMDB poster path, e.g. /abc123.jpg", json_schema_extra={"example": "/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg"})
    genres: Optional[str] = Field(None, description="Comma-separated TMDB genre IDs", json_schema_extra={"example": "28,12,878"})
    overview: Optional[str] = Field(None, description="Movie synopsis")
    runtime: Optional[int] = Field(None, description="Duration in minutes", json_schema_extra={"example": 148})
    director: Optional[str] = Field(None, description="Director name", json_schema_extra={"example": "Christopher Nolan"})


class RenameMovieRequest(BaseModel):
    new_title: str = Field(..., description="New movie title", json_schema_extra={"example": "Inception (2010)"})


class WatchedRequest(BaseModel):
    watched_by: str = Field("iOS", description="Name of the person marking as watched", json_schema_extra={"example": "Daniiar"})


class MovieResponse(BaseModel):
    id: int
    chat_id: int
    title: str
    status: str = Field(..., description="'to_watch' or 'watched'")
    added_by: Optional[str] = None
    added_at: Optional[str] = Field(None, description="ISO 8601 datetime")
    watched_by: Optional[str] = None
    watched_at: Optional[str] = Field(None, description="ISO 8601 datetime, null if not watched")
    tmdb_id: Optional[int] = None
    year: Optional[int] = None
    rating: Optional[float] = None
    poster_path: Optional[str] = Field(None, description="Append to https://image.tmdb.org/t/p/w500")
    genres: Optional[str] = Field(None, description="Comma-separated TMDB genre IDs, e.g. '28,12,878'")
    overview: Optional[str] = None
    runtime: Optional[int] = Field(None, description="Duration in minutes")
    director: Optional[str] = Field(None, description="Director name")


class StatsResponse(BaseModel):
    to_watch: int = Field(..., description="Number of movies in watchlist")
    watched: int = Field(..., description="Number of watched movies")


class SearchResult(BaseModel):
    tmdb_id: Optional[int] = None
    title: Optional[str] = None
    original_title: Optional[str] = None
    year: Optional[str] = Field(None, description="4-digit year string, e.g. '2010'")
    rating: Optional[float] = Field(None, description="TMDB vote average")
    overview: str = ""
    poster_path: Optional[str] = Field(None, description="Append to https://image.tmdb.org/t/p/w500")


class SearchResponse(BaseModel):
    results: List[SearchResult]
    total_pages: int
    page: int


class Suggestion(BaseModel):
    title: str
    year: Optional[str] = None
    rating: Optional[float] = None
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    tmdb_id: Optional[int] = None
    reason: str = Field(..., description="Explanation in Russian")


class RecommendationResponse(BaseModel):
    intent: str = Field(..., description="'similar', 'mood', or 'history'")
    source_movie: Optional[str] = Field(None, description="Source movie title (only for 'similar' intent)")
    suggestions: List[Suggestion]


class StatusResponse(BaseModel):
    status: str
    title: str


class RenameResponse(BaseModel):
    old_title: str
    new_title: str


class BasketAddRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram group chat ID", examples=[-1001234567890])
    user_id: int = Field(..., description="Telegram user ID", examples=[123456789])
    user_name: str = Field(..., description="Display name", examples=["Daniiar"])
    movie_nums: List[int] = Field(..., description="1-based positions in the to_watch list", examples=[[1, 5, 12]])


class BasketRemoveRequest(BaseModel):
    movie_nums: Optional[List[int]] = Field(
        None,
        description="Positions to remove. Omit (or pass null) to clear the user's entire basket.",
        examples=[[1, 5]],
    )


class BasketMovieEntry(BaseModel):
    movie_num: int = Field(..., description="1-based position in to_watch list")
    movie: Optional[MovieResponse] = Field(None, description="Resolved movie, null if num out of range")


class UserBasketEntry(BaseModel):
    user_id: int
    user_name: str
    movies: List[BasketMovieEntry]


class BasketResponse(BaseModel):
    by_user: List[UserBasketEntry]
    unique_count: int = Field(..., description="Number of distinct movie positions across all users")


class BasketAddResponse(BaseModel):
    added: List[int] = Field(..., description="Positions successfully added")
    already_in_basket: List[int] = Field(..., description="Positions already present")


class ChatMemberResponse(BaseModel):
    user_id: int = Field(..., description="Telegram user ID")
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = Field(None, description="Without the @ prefix")
    is_bot: bool
    photo_url: Optional[str] = Field(
        None,
        description="Proxied photo URL: GET /users/{user_id}/photo?chat_id=X. Null if user has no photo.",
    )


def _check_body_access(request: Request, chat_id: int, user_id: int | None = None):
    """Raise 403 if body chat_id/user_id doesn't match the authenticated session."""
    if hasattr(request.state, "chat_id") and chat_id != request.state.chat_id:
        raise HTTPException(status_code=403, detail="Access denied to this chat")
    if user_id is not None and hasattr(request.state, "user_id") and user_id != request.state.user_id:
        raise HTTPException(status_code=403, detail="Cannot act as another user")


# ── internal helpers ─────────────────────────────────────────────────────────

async def _has_profile_photo(bot: Bot, user_id: int) -> bool:
    """Return True if the Telegram user has at least one profile photo."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        return photos.total_count > 0
    except Exception:
        return False


async def _fetch_tmdb_extras(tmdb_id: int) -> tuple[dict | None, str | None]:
    """Fetch movie details and director from TMDB in parallel."""
    details, director = await asyncio.gather(
        tmdb_get_movie(tmdb_id),
        tmdb_get_credits(tmdb_id),
    )
    return details, director


# ── movies ────────────────────────────────────────────────────────────────────

@app.get(
    "/movies",
    summary="List movies",
    response_model=List[MovieResponse],
    tags=["Movies"],
)
async def get_movies(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    status: Optional[str] = Query(None, description="Filter by status: `to_watch` or `watched`. Omit for all movies."),
):
    """
    Returns movies for a chat, ordered by date added (newest first).

    - **No status** → all movies
    - **status=to_watch** → watchlist only
    - **status=watched** → watch history only
    """
    rows = get_movies_db(chat_id, status=status)
    return [_serialize(r) for r in rows]


@app.get(
    "/movies/{movie_id}",
    summary="Get a single movie",
    response_model=MovieResponse,
    responses={404: {"description": "Movie not found"}},
    tags=["Movies"],
)
async def get_movie(
    movie_id: int,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Returns a single movie by its database ID."""
    row = get_movie_by_id(chat_id, movie_id)
    if not row:
        raise HTTPException(status_code=404, detail="Movie not found")
    return _serialize(row)


@app.post(
    "/movies",
    status_code=201,
    summary="Add a movie",
    response_model=StatusResponse,
    responses={409: {"description": "Movie already exists in watchlist or watched"}},
    tags=["Movies"],
)
async def add_movie(body: AddMovieRequest, request: Request):
    """
    Add a movie to the watchlist.

    Only `chat_id` and `title` are required. Provide TMDB fields for richer data
    (use `GET /search` to find them).

    When `tmdb_id` is supplied, `overview`, `runtime`, and `director` are fetched
    automatically from TMDB if not already provided in the request body.

    Returns **409** if the movie title already exists for this chat.
    """
    _check_body_access(request, body.chat_id)
    overview = body.overview
    runtime = body.runtime
    director = body.director

    if body.tmdb_id and (overview is None or runtime is None or director is None):
        details, credits_director = await _fetch_tmdb_extras(body.tmdb_id)
        if details:
            if overview is None:
                overview = details.get("overview") or None
            if runtime is None:
                runtime = details.get("runtime") or None
        if director is None:
            director = credits_director

    success, status = add_movie_db(
        chat_id=body.chat_id,
        title=body.title,
        added_by=body.added_by,
        tmdb_id=body.tmdb_id,
        year=body.year,
        rating=body.rating,
        poster_path=body.poster_path,
        genres=body.genres,
        overview=overview,
        runtime=runtime,
        director=director,
    )
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Movie already exists with status '{status}'",
        )
    return {"status": "added", "title": body.title}


@app.patch(
    "/movies/{movie_id}/watched",
    summary="Mark movie as watched",
    response_model=StatusResponse,
    responses={400: {"description": "Movie not found or already watched"}},
    tags=["Movies"],
)
async def mark_watched(
    movie_id: int,
    body: WatchedRequest,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Moves a movie from the watchlist to watch history."""
    success, title = mark_watched_by_id(chat_id, movie_id, body.watched_by)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or already watched")
    return {"status": "watched", "title": title}


@app.patch(
    "/movies/{movie_id}/unwatch",
    summary="Move back to watchlist",
    response_model=StatusResponse,
    responses={400: {"description": "Movie not found or not in watched list"}},
    tags=["Movies"],
)
async def unwatch(
    movie_id: int,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Moves a movie from watch history back to the watchlist."""
    success, title = unwatch_movie_by_id(chat_id, movie_id)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or not in watched list")
    return {"status": "to_watch", "title": title}


@app.patch(
    "/movies/{movie_id}/rename",
    summary="Rename a movie",
    response_model=RenameResponse,
    responses={400: {"description": "Movie not found or new title already exists"}},
    tags=["Movies"],
)
async def rename_movie(
    movie_id: int,
    body: RenameMovieRequest,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Rename a movie. Returns 400 if the new title already exists for this chat."""
    success, old_title = rename_movie_by_id(chat_id, movie_id, body.new_title)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or title already exists")
    return {"old_title": old_title, "new_title": body.new_title}


@app.delete(
    "/movies/{movie_id}",
    summary="Delete a movie",
    response_model=StatusResponse,
    responses={404: {"description": "Movie not found"}},
    tags=["Movies"],
)
async def remove_movie(
    movie_id: int,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Permanently removes a movie from the watchlist or history."""
    title = remove_movie_by_id(chat_id, movie_id)
    if not title:
        raise HTTPException(status_code=404, detail="Movie not found")
    return {"status": "removed", "title": title}


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get(
    "/stats",
    summary="Get watchlist stats",
    response_model=StatsResponse,
    tags=["Stats"],
)
async def get_stats(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Returns the count of movies in each status bucket for a chat."""
    return get_counts_db(chat_id)


# ── tmdb search ───────────────────────────────────────────────────────────────

@app.get(
    "/search",
    summary="Search movies via TMDB",
    response_model=SearchResponse,
    tags=["Search"],
)
async def search_movies(
    q: str = Query(..., description="Movie title, optionally with year: `Inception 2010` or `Начало (2010)`", examples=["Inception 2010"]),
    page: int = Query(1, ge=1, le=10, description="Page number (1–10)"),
):
    """
    Search TMDB for movies. Returns up to 20 results sorted by popularity score.

    **Typical flow:** search → user picks a result → `POST /movies` with the `tmdb_id` and metadata.

    Supports Russian and English queries. Year is parsed automatically from the query string.
    """
    title, year = parse_movie_query(q)
    data = await tmdb_search(title, page=page, year=year)
    results = []
    for r in data.get("results", []):
        results.append({
            "tmdb_id": r.get("id"),
            "title": r.get("title"),
            "original_title": r.get("original_title"),
            "year": r.get("release_date", "")[:4] or None,
            "rating": r.get("vote_average"),
            "overview": r.get("overview", ""),
            "poster_path": r.get("poster_path", ""),
        })
    return {"results": results, "total_pages": data.get("total_pages", 1), "page": page}


# ── ai recommendations ────────────────────────────────────────────────────────

@app.get(
    "/recommendations",
    summary="AI movie recommendations",
    response_model=RecommendationResponse,
    responses={503: {"description": "GROQ_API_KEY not configured"}},
    tags=["Recommendations"],
)
async def get_recommendations(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    q: str = Query(
        "",
        description=(
            "Optional query. Intent is auto-detected:\n"
            "- **Empty** → recommendations based on group's watch history\n"
            "- **Movie title** (e.g. `like Inception`, `как Начало`) → similar movies\n"
            "- **Mood/genre** (e.g. `мрачный триллер`, `something funny`) → mood-based picks"
        ),
        examples=["like Inception"],
    ),
):
    """
    Returns 3 AI-powered movie recommendations via Groq (Llama 3.3 70B).

    The intent is auto-detected from the query:
    - `history` — empty query, uses group's watch history as context
    - `similar` — query contains a movie title
    - `mood` — query describes a vibe or genre

    Each suggestion includes a `reason` field in Russian explaining why it was recommended.
    Requires `GROQ_API_KEY` in server `.env`.
    """
    try:
        result = await get_rec_suggestions(chat_id, q)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return result


# ── random ────────────────────────────────────────────────────────────────────

@app.get(
    "/random",
    summary="Random movie from watchlist",
    response_model=MovieResponse,
    responses={404: {"description": "Watchlist is empty"}},
    tags=["Random"],
)
async def random_movie(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Returns a single randomly chosen movie from the to_watch list."""
    movies = get_movies_db(chat_id, "to_watch")
    if not movies:
        raise HTTPException(status_code=404, detail="Watchlist is empty")
    return _serialize(_random.choice(movies))


# ── vote basket ───────────────────────────────────────────────────────────────

def _resolve_basket(chat_id: int, basket_rows: list) -> tuple[list, list]:
    """Return (to_watch list, resolved BasketMovieEntry dicts) for given basket rows."""
    to_watch = get_movies_db(chat_id, "to_watch")

    def _entry(num: int) -> dict:
        movie = _serialize(to_watch[num - 1]) if 1 <= num <= len(to_watch) else None
        return {"movie_num": num, "movie": movie}

    return to_watch, [_entry(row["movie_num"]) for row in basket_rows]


@app.get(
    "/basket",
    summary="Full vote basket (all users)",
    response_model=BasketResponse,
    tags=["Basket"],
)
async def get_basket(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """
    Returns the vote basket grouped by user, each entry resolved to a movie object.

    `movie_num` is the 1-based position in the current to_watch list.
    `movie` is null when the position is out of range (list changed since adding).
    """
    rows = get_full_basket(chat_id)
    to_watch = get_movies_db(chat_id, "to_watch")

    by_user: dict[int, dict] = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in by_user:
            by_user[uid] = {"user_id": uid, "user_name": row["user_name"], "movies": []}
        num = row["movie_num"]
        movie = _serialize(to_watch[num - 1]) if 1 <= num <= len(to_watch) else None
        by_user[uid]["movies"].append({"movie_num": num, "movie": movie})

    unique_count = len(get_unique_basket_movies(chat_id))
    return {"by_user": list(by_user.values()), "unique_count": unique_count}


@app.get(
    "/basket/my",
    summary="My basket entries",
    response_model=List[BasketMovieEntry],
    tags=["Basket"],
)
async def get_my_basket(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    user_id: int = Query(..., description="Telegram user ID", examples=[123456789]),
):
    """Returns the calling user's basket entries with resolved movie objects."""
    nums = get_user_basket(chat_id, user_id)
    to_watch = get_movies_db(chat_id, "to_watch")
    return [
        {"movie_num": n, "movie": _serialize(to_watch[n - 1]) if 1 <= n <= len(to_watch) else None}
        for n in nums
    ]


@app.post(
    "/basket/add",
    summary="Add movies to basket",
    response_model=BasketAddResponse,
    tags=["Basket"],
)
async def basket_add(body: BasketAddRequest, request: Request):
    """
    Add one or more to_watch positions to a user's basket.

    `movie_nums` are 1-based positions in the chat's current to_watch list.
    Positions already in the basket are reported in `already_in_basket` and not duplicated.
    """
    _check_body_access(request, body.chat_id, body.user_id)
    to_watch = get_movies_db(body.chat_id, "to_watch")
    valid = [n for n in body.movie_nums if 1 <= n <= len(to_watch)]
    invalid = [n for n in body.movie_nums if n not in valid]

    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"movie_nums out of range (watchlist has {len(to_watch)} items): {invalid}",
        )

    added, already = add_to_basket(body.chat_id, body.user_id, body.user_name, valid)
    return {"added": added, "already_in_basket": already}


@app.delete(
    "/basket/remove",
    summary="Remove from user's basket",
    response_model=StatusResponse,
    tags=["Basket"],
)
async def basket_remove(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    user_id: int = Query(..., description="Telegram user ID", examples=[123456789]),
    body: BasketRemoveRequest = BasketRemoveRequest(),
):
    """
    Remove specific positions from a user's basket, or clear the entire user basket.

    - Pass `movie_nums` in the request body to remove specific entries.
    - Omit `movie_nums` (or pass `null`) to clear all entries for that user.
    """
    count = remove_from_basket(chat_id, user_id, body.movie_nums)
    return {"status": "removed", "title": f"{count} entries"}


@app.delete(
    "/basket/clear",
    summary="Clear entire basket for a chat",
    response_model=StatusResponse,
    tags=["Basket"],
)
async def basket_clear_all(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Removes all basket entries for the chat (equivalent to bot `/vc`)."""
    count = clear_basket(chat_id)
    return {"status": "cleared", "title": f"{count} entries"}


@app.get(
    "/basket/random",
    summary="Random movie from basket",
    response_model=MovieResponse,
    responses={404: {"description": "Basket is empty or all positions out of range"}},
    tags=["Basket"],
)
async def basket_random_movie(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """Returns a randomly chosen movie from the unique basket positions (equivalent to bot `/vrand`)."""
    unique_nums = get_unique_basket_movies(chat_id)
    if not unique_nums:
        raise HTTPException(status_code=404, detail="Basket is empty")

    to_watch = get_movies_db(chat_id, "to_watch")
    valid = [n for n in unique_nums if 1 <= n <= len(to_watch)]
    if not valid:
        raise HTTPException(status_code=404, detail="No valid movies in basket")

    return _serialize(to_watch[_random.choice(valid) - 1])


# ── polling ───────────────────────────────────────────────────────────────────

@app.get(
    "/poll",
    summary="Random poll candidates",
    response_model=List[MovieResponse],
    responses={404: {"description": "Not enough movies in watchlist"}},
    tags=["Poll"],
)
async def poll_random(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    n: int = Query(3, ge=2, le=10, description="Number of movies to sample (2–10)"),
):
    """
    Returns `n` randomly sampled movies from the to_watch list — the options for a poll.

    Equivalent to bot `/poll N`. Send the results as a Telegram poll or display a picker in the app.
    """
    movies = get_movies_db(chat_id, "to_watch")
    if len(movies) < 2:
        raise HTTPException(status_code=404, detail="Need at least 2 movies in watchlist")
    sample = _random.sample(movies, min(n, len(movies)))
    return [_serialize(m) for m in sample]


@app.get(
    "/poll/pick",
    summary="Poll candidates by position",
    response_model=List[MovieResponse],
    responses={422: {"description": "Invalid or out-of-range movie_nums"}},
    tags=["Poll"],
)
async def poll_pick(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    movie_nums: str = Query(..., description="Comma-separated 1-based positions, e.g. `1,5,12`", examples=["1,5,12"]),
):
    """
    Returns the specific to_watch movies at the given positions.

    Equivalent to bot `/vote 1,5,12`. Use these as poll options or pass to `/poll/random_pick`
    to let the server choose one.
    """
    try:
        nums = [int(x.strip()) for x in movie_nums.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="movie_nums must be comma-separated integers")

    movies = get_movies_db(chat_id, "to_watch")
    invalid = [n for n in nums if not (1 <= n <= len(movies))]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Positions out of range (watchlist has {len(movies)} items): {invalid}",
        )

    return [_serialize(movies[n - 1]) for n in nums]


@app.get(
    "/poll/rpick",
    summary="Random pick from specific positions",
    response_model=MovieResponse,
    responses={422: {"description": "Invalid or out-of-range movie_nums"}},
    tags=["Poll"],
)
async def poll_rpick(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
    movie_nums: str = Query(..., description="Comma-separated 1-based positions, e.g. `1,5,12`", examples=["1,5,12"]),
):
    """
    Randomly picks **one** movie from the specified positions.

    Equivalent to bot `/rpoll 1,5,12`.
    """
    try:
        nums = [int(x.strip()) for x in movie_nums.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="movie_nums must be comma-separated integers")

    movies = get_movies_db(chat_id, "to_watch")
    valid = [n for n in nums if 1 <= n <= len(movies)]
    if not valid:
        raise HTTPException(status_code=422, detail="No valid positions provided")

    return _serialize(movies[_random.choice(valid) - 1])


# ── users ─────────────────────────────────────────────────────────────────────

@app.get(
    "/users",
    summary="List group members",
    response_model=List[ChatMemberResponse],
    responses={
        503: {"description": "TELEGRAM_BOT_TOKEN not configured"},
        502: {"description": "Telegram API error (bot not in group, invalid chat_id, etc.)"},
    },
    tags=["Users"],
)
async def get_users(
    request: Request,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """
    Returns all group administrators with basic profile info and proxied photo URLs.

    Since the group currently consists entirely of admins, this effectively returns all members.
    `photo_url` points to `GET /users/{user_id}/photo` — the bot token is never exposed to clients.
    Photo presence is checked for all members in parallel.
    """
    bot: Optional[Bot] = request.app.state.bot
    if bot is None:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN not configured")

    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram API error: {e}")

    async def _build(member) -> dict:
        u = member.user
        has_photo = await _has_profile_photo(bot, u.id)
        return {
            "user_id": u.id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "username": u.username,
            "is_bot": u.is_bot,
            "photo_url": f"/users/{u.id}/photo?chat_id={chat_id}" if has_photo else None,
        }

    return list(await asyncio.gather(*[_build(m) for m in admins]))


@app.get(
    "/users/{user_id}/photo",
    summary="Get user profile photo",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "JPEG image bytes"},
        404: {"description": "User has no profile photo"},
        503: {"description": "TELEGRAM_BOT_TOKEN not configured"},
        502: {"description": "Telegram API error"},
    },
    tags=["Users"],
)
async def get_user_photo(
    user_id: int,
    request: Request,
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """
    Proxies the user's Telegram profile photo as JPEG bytes.

    The bot token is never exposed in the URL — this endpoint fetches the image
    server-side and streams it to the client.
    Uses the smallest available photo size (suitable for avatars).
    """
    bot: Optional[Bot] = request.app.state.bot
    if bot is None:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN not configured")

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram API error: {e}")

    if not photos.total_count or not photos.photos:
        raise HTTPException(status_code=404, detail="User has no profile photo")

    # photos.photos[0] = most recent set; [0] = smallest size (good for avatars)
    file_id = photos.photos[0][0].file_id

    try:
        file_obj = await bot.get_file(file_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not resolve file: {e}")

    token = request.app.state.bot_token
    download_url = f"https://api.telegram.org/file/bot{token}/{file_obj.file_path}"

    async def _stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", download_url) as resp:
                async for chunk in resp.aiter_bytes(8192):
                    yield chunk

    return StreamingResponse(_stream(), media_type="image/jpeg")


# ── presence ──────────────────────────────────────────────────────────────────

class HeartbeatRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram group chat ID", examples=[-1001234567890])
    user_id: int = Field(..., description="Telegram user ID", examples=[123456789])


class PresenceResponse(BaseModel):
    online_user_ids: List[int] = Field(..., description="User IDs active in the last 90 seconds")


@app.post(
    "/presence/heartbeat",
    summary="Register user as online",
    response_model=dict,
    tags=["Presence"],
)
async def heartbeat(body: HeartbeatRequest, request: Request):
    """
    Call every ~30 seconds while the app is in the foreground to mark the user as online.
    Presence is stored in Redis with a sliding 90-second window.
    """
    _check_body_access(request, body.chat_id, body.user_id)
    if _redis_client:
        await _redis_client.zadd(
            f"presence:{body.chat_id}",
            {str(body.user_id): time.time()},
        )
    return {"status": "ok"}


@app.get(
    "/presence",
    summary="Get online users for a chat",
    response_model=PresenceResponse,
    tags=["Presence"],
)
async def get_presence(
    chat_id: int = Query(..., description="Telegram group chat ID", examples=[-1001234567890]),
):
    """
    Returns user IDs that sent a heartbeat within the last 90 seconds for this chat.
    """
    if not _redis_client:
        return PresenceResponse(online_user_ids=[])
    threshold = time.time() - 90
    members = await _redis_client.zrangebyscore(f"presence:{chat_id}", threshold, "+inf")
    return PresenceResponse(online_user_ids=[int(uid) for uid in members])


# ── realtime ──────────────────────────────────────────────────────────────────

@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: int, session_token: str = Query("")):
    """
    WebSocket endpoint for real-time watchlist and basket updates.

    Pass the session token as a query parameter: `?session_token=<token>`

    Connect once per chat to receive push events whenever data changes.
    The server never closes the connection — disconnect from the client side when done.

    **Event format:**
    ```json
    {"event": "<type>", "chat_id": <int>, "data": {...}}
    ```

    **Events:**
    - `movie_added` — data: `{title}`
    - `movie_watched` — data: `{id, title}`
    - `movie_unwatched` — data: `{id, title}`
    - `movie_removed` — data: `{id, title}`
    - `movie_renamed` — data: `{id, old_title, new_title}`
    - `basket_updated` — data: `{}` (refetch `/basket`)
    """
    if _redis_client:
        session_data = await _redis_client.get(f"session:{session_token}")
        if not session_data:
            await websocket.close(code=4001)
            return
        if json.loads(session_data)["chat_id"] != chat_id:
            await websocket.close(code=4001)
            return
    await manager.connect(chat_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; client messages are ignored
    except WebSocketDisconnect:
        manager.disconnect(chat_id, websocket)
