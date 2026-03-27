"""
FastAPI server for the movie watchlist bot.

Run locally:
    uvicorn api:app --reload --port 8000

Docs available at:
    http://localhost:8000/docs
"""

from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
from bot.tmdb_api import tmdb_search, parse_movie_query
from bot.groq_ai import get_rec_suggestions


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Movie Watchlist API",
    description="API for the FeetForTarantino Telegram bot",
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


# ── schemas ───────────────────────────────────────────────────────────────────

class AddMovieRequest(BaseModel):
    chat_id: int
    title: str
    added_by: str = "iOS"
    tmdb_id: Optional[int] = None
    year: Optional[int] = None
    rating: Optional[float] = None
    poster_path: Optional[str] = None
    genres: Optional[str] = None


class RenameMovieRequest(BaseModel):
    new_title: str


class WatchedRequest(BaseModel):
    watched_by: str = "iOS"


# ── movies ────────────────────────────────────────────────────────────────────

@app.get("/movies", summary="Get watchlist or watched movies")
async def get_movies(
    chat_id: int = Query(..., description="Telegram chat ID"),
    status: Optional[str] = Query(None, description="Filter: 'to_watch' or 'watched'"),
):
    """
    Returns movies for a chat.
    - No status filter → all movies
    - status=to_watch → watchlist
    - status=watched → watched history
    """
    rows = get_movies_db(chat_id, status=status)
    return [_serialize(r) for r in rows]


@app.get("/movies/{movie_id}", summary="Get a single movie")
async def get_movie(chat_id: int, movie_id: int):
    row = get_movie_by_id(chat_id, movie_id)
    if not row:
        raise HTTPException(status_code=404, detail="Movie not found")
    return _serialize(row)


@app.post("/movies", status_code=201, summary="Add a movie")
async def add_movie(body: AddMovieRequest):
    success, status = add_movie_db(
        chat_id=body.chat_id,
        title=body.title,
        added_by=body.added_by,
        tmdb_id=body.tmdb_id,
        year=body.year,
        rating=body.rating,
        poster_path=body.poster_path,
        genres=body.genres,
    )
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Movie already exists with status '{status}'",
        )
    return {"status": "added", "title": body.title}


@app.patch("/movies/{movie_id}/watched", summary="Mark movie as watched")
async def mark_watched(chat_id: int, movie_id: int, body: WatchedRequest):
    success, title = mark_watched_by_id(chat_id, movie_id, body.watched_by)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or already watched")
    return {"status": "watched", "title": title}


@app.patch("/movies/{movie_id}/unwatch", summary="Move back to watchlist")
async def unwatch(chat_id: int, movie_id: int):
    success, title = unwatch_movie_by_id(chat_id, movie_id)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or not in watched list")
    return {"status": "to_watch", "title": title}


@app.patch("/movies/{movie_id}/rename", summary="Rename a movie")
async def rename_movie(chat_id: int, movie_id: int, body: RenameMovieRequest):
    success, old_title = rename_movie_by_id(chat_id, movie_id, body.new_title)
    if not success:
        raise HTTPException(status_code=400, detail="Movie not found or title already exists")
    return {"old_title": old_title, "new_title": body.new_title}


@app.delete("/movies/{movie_id}", summary="Remove a movie")
async def remove_movie(chat_id: int, movie_id: int):
    title = remove_movie_by_id(chat_id, movie_id)
    if not title:
        raise HTTPException(status_code=404, detail="Movie not found")
    return {"status": "removed", "title": title}


# ── stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats", summary="Get watchlist stats")
async def get_stats(chat_id: int = Query(..., description="Telegram chat ID")):
    """Returns count of to_watch and watched movies."""
    return get_counts_db(chat_id)


# ── tmdb search ───────────────────────────────────────────────────────────────

@app.get("/search", summary="Search movies via TMDB")
async def search_movies(
    q: str = Query(..., description="Movie title, optionally with year: 'Inception 2010'"),
    page: int = Query(1, ge=1, le=10),
):
    """
    Search TMDB for movies. Returns up to 20 results sorted by popularity.
    Use the result's tmdb_id when calling POST /movies.
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

@app.get("/recommendations", summary="AI movie recommendations")
async def get_recommendations(
    chat_id: int = Query(..., description="Telegram chat ID"),
    q: str = Query("", description="Optional: movie name ('like Inception') or mood ('мрачный триллер')"),
):
    """
    Returns 3 AI-powered movie recommendations via Groq.
    - Empty q → based on watch history
    - Movie title → similar movies
    - Mood/genre description → mood-based picks
    """
    try:
        result = await get_rec_suggestions(chat_id, q)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return result
