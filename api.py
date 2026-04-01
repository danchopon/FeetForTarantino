"""
FastAPI server for the movie watchlist bot.

Run locally:
    uvicorn api:app --reload --port 8000

Docs available at:
    http://localhost:8000/docs
"""

from contextlib import asynccontextmanager
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

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
)
from bot.tmdb_api import tmdb_search, parse_movie_query, tmdb_get_movie, tmdb_get_credits
from bot.groq_ai import get_rec_suggestions


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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


# ── internal helpers ─────────────────────────────────────────────────────────

async def _fetch_tmdb_extras(tmdb_id: int) -> tuple[dict | None, str | None]:
    """Fetch movie details and director from TMDB in parallel."""
    import asyncio
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
async def add_movie(body: AddMovieRequest):
    """
    Add a movie to the watchlist.

    Only `chat_id` and `title` are required. Provide TMDB fields for richer data
    (use `GET /search` to find them).

    When `tmdb_id` is supplied, `overview`, `runtime`, and `director` are fetched
    automatically from TMDB if not already provided in the request body.

    Returns **409** if the movie title already exists for this chat.
    """
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
