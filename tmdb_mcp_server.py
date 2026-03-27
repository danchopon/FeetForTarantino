#!/usr/bin/env python3
"""
TMDB MCP Server — provides smart movie recommendation tools.

Tools:
  - suggest_movies: mood-based recommendations using watch history
  - find_similar: find movies similar to a given title, excluding watchlist
"""

import os
import json
import logging

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
DATABASE_URL = os.environ.get("DATABASE_URL")

# Genre ID -> name mapping (TMDB standard)
GENRE_MAP = {
    28: "боевик", 12: "приключения", 16: "мультфильм", 35: "комедия",
    80: "криминал", 99: "документальный", 18: "драма", 10751: "семейный",
    14: "фэнтези", 36: "история", 27: "ужасы", 10402: "музыка",
    9648: "детектив", 10749: "мелодрама", 878: "фантастика",
    10770: "телефильм", 53: "триллер", 10752: "военный", 37: "вестерн",
}

# Mood -> genre IDs + keywords for TMDB discover
MOOD_GENRES = {
    "мрачное": [80, 53, 27, 9648],
    "мрачный": [80, 53, 27, 9648],
    "dark": [80, 53, 27, 9648],
    "весёлое": [35, 16, 10751],
    "веселое": [35, 16, 10751],
    "смешное": [35],
    "funny": [35, 16],
    "comedy": [35],
    "страшное": [27, 53],
    "scary": [27, 53],
    "horror": [27],
    "романтика": [10749, 35],
    "романтичное": [10749],
    "romantic": [10749],
    "фантастика": [878, 14],
    "scifi": [878],
    "sci-fi": [878],
    "драма": [18],
    "drama": [18],
    "экшн": [28, 12],
    "action": [28, 12],
    "детектив": [9648, 80],
    "mystery": [9648],
    "триллер": [53],
    "thriller": [53],
    "документальное": [99],
    "documentary": [99],
    "военное": [10752, 36],
    "war": [10752],
    "мультфильм": [16],
    "animation": [16],
    "приключения": [12, 14],
    "adventure": [12],
}

mcp = FastMCP("tmdb-movies")


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def get_all_tmdb_ids(chat_id: int) -> set[int]:
    """Get all TMDB IDs from this chat's watchlist + watched."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT tmdb_id FROM movies WHERE chat_id = %s AND tmdb_id IS NOT NULL", (chat_id,))
    ids = {row["tmdb_id"] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return ids


def get_watched_movies(chat_id: int) -> list[dict]:
    """Get watched movies with their genres."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT title, tmdb_id, genres, rating, year FROM movies WHERE chat_id = %s AND status = 'watched'",
        (chat_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def get_watchlist_movies(chat_id: int) -> list[dict]:
    """Get to_watch movies."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT title, tmdb_id, genres, rating, year FROM movies WHERE chat_id = %s AND status = 'to_watch'",
        (chat_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def parse_mood_to_genres(mood: str) -> list[int]:
    """Extract genre IDs from a mood description."""
    mood_lower = mood.lower()
    genre_ids = set()
    for keyword, genres in MOOD_GENRES.items():
        if keyword in mood_lower:
            genre_ids.update(genres)
    return list(genre_ids)


def format_movie_result(movie: dict) -> dict:
    """Format a TMDB movie result into a clean dict."""
    year = movie.get("release_date", "")[:4]
    genre_names = [GENRE_MAP.get(gid, "?") for gid in movie.get("genre_ids", [])]
    return {
        "tmdb_id": movie["id"],
        "title": movie.get("title", "Unknown"),
        "year": year,
        "rating": movie.get("vote_average", 0),
        "overview": movie.get("overview", ""),
        "genres": ", ".join(genre_names),
        "popularity": movie.get("popularity", 0),
    }


async def tmdb_get(path: str, params: dict) -> dict | None:
    """Make a TMDB API request."""
    params["api_key"] = TMDB_API_KEY
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{TMDB_BASE_URL}{path}", params=params)
        if resp.status_code == 200:
            return resp.json()
    return None


@mcp.tool()
async def suggest_movies(chat_id: int, mood: str = "") -> str:
    """Suggest movies based on mood/description and group's watch history.

    Args:
        chat_id: Telegram chat ID to check watch history against
        mood: Description of desired mood/vibe (e.g. "мрачное но не слишком длинное",
              "что-то весёлое", "dark thriller"). Can be empty for general recommendations.

    Returns:
        JSON with suggested movies and reasoning.
    """
    if not TMDB_API_KEY:
        return json.dumps({"error": "TMDB API key not configured"})

    exclude_ids = get_all_tmdb_ids(chat_id)
    watched = get_watched_movies(chat_id)

    # Determine genres from mood
    mood_genres = parse_mood_to_genres(mood) if mood else []

    # If no mood genres found, use watch history genres
    if not mood_genres and watched:
        genre_count: dict[int, int] = {}
        for m in watched:
            if m.get("genres"):
                for g in str(m["genres"]).split(","):
                    g = g.strip()
                    if g.isdigit():
                        gid = int(g)
                        genre_count[gid] = genre_count.get(gid, 0) + 1
        sorted_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)
        mood_genres = [g[0] for g in sorted_genres[:3]]

    if not mood_genres:
        # Fallback: popular movies
        mood_genres = [18, 53, 28]  # drama, thriller, action

    # Check for runtime preference in mood
    max_runtime = None
    short_keywords = ["короткое", "короткий", "не длинное", "не слишком длинное", "short", "не долгое"]
    for kw in short_keywords:
        if kw in mood.lower():
            max_runtime = 120
            break

    # Discover movies
    params = {
        "language": "ru-RU",
        "with_genres": "|".join(map(str, mood_genres)),
        "sort_by": "vote_average.desc",
        "vote_count.gte": 200,
        "page": 1,
    }
    if max_runtime:
        params["with_runtime.lte"] = max_runtime

    data = await tmdb_get("/discover/movie", params)
    if not data:
        return json.dumps({"error": "TMDB API request failed"})

    results = data.get("results", [])

    # Filter out already known movies
    results = [m for m in results if m["id"] not in exclude_ids]

    # Take top 3
    suggestions = []
    for movie in results[:3]:
        formatted = format_movie_result(movie)

        # Build reasoning
        reasons = []
        movie_genre_ids = set(movie.get("genre_ids", []))
        matched_moods = []
        for keyword, genre_list in MOOD_GENRES.items():
            if keyword in mood.lower() and movie_genre_ids & set(genre_list):
                matched_moods.append(keyword)
        if matched_moods:
            reasons.append(f"Подходит под настроение: {', '.join(matched_moods)}")
        if movie.get("vote_average", 0) >= 7.5:
            reasons.append(f"Высокий рейтинг: {movie['vote_average']:.1f}")
        if max_runtime:
            reasons.append("Не слишком длинный")

        formatted["reasoning"] = ". ".join(reasons) if reasons else "Популярный фильм в подходящем жанре"
        suggestions.append(formatted)

    mood_genre_names = [GENRE_MAP.get(g, str(g)) for g in mood_genres]

    return json.dumps({
        "mood": mood or "общие рекомендации",
        "matched_genres": mood_genre_names,
        "suggestions": suggestions,
        "excluded_count": len(exclude_ids),
    }, ensure_ascii=False)


@mcp.tool()
async def find_similar(chat_id: int, movie_title: str) -> str:
    """Find movies similar to a given title, excluding already added movies.

    Args:
        chat_id: Telegram chat ID to check watchlist against
        movie_title: Title of the movie to find similar to (e.g. "Inception", "Начало")

    Returns:
        JSON with similar movies, filtered against the group's watchlist.
    """
    if not TMDB_API_KEY:
        return json.dumps({"error": "TMDB API key not configured"})

    exclude_ids = get_all_tmdb_ids(chat_id)

    # First, search for the source movie
    search_data = await tmdb_get("/search/movie", {
        "query": movie_title,
        "language": "ru-RU",
    })

    if not search_data or not search_data.get("results"):
        # Try English
        search_data = await tmdb_get("/search/movie", {
            "query": movie_title,
            "language": "en-US",
        })

    if not search_data or not search_data.get("results"):
        return json.dumps({"error": f"Фильм '{movie_title}' не найден в TMDB"}, ensure_ascii=False)

    source_movie = search_data["results"][0]
    source_id = source_movie["id"]
    source_formatted = format_movie_result(source_movie)

    # Get similar movies from TMDB
    similar_data = await tmdb_get(f"/movie/{source_id}/similar", {"language": "ru-RU"})
    recommendations_data = await tmdb_get(f"/movie/{source_id}/recommendations", {"language": "ru-RU"})

    # Combine and deduplicate
    all_movies: dict[int, dict] = {}
    for movie in (similar_data or {}).get("results", []):
        if movie["id"] not in exclude_ids and movie["id"] != source_id:
            all_movies[movie["id"]] = movie

    for movie in (recommendations_data or {}).get("results", []):
        if movie["id"] not in exclude_ids and movie["id"] != source_id:
            all_movies.setdefault(movie["id"], movie)

    # Sort by rating * vote_count
    sorted_movies = sorted(
        all_movies.values(),
        key=lambda x: x.get("vote_average", 0) * min(x.get("vote_count", 0), 1000),
        reverse=True,
    )

    # Take top 3
    suggestions = [format_movie_result(m) for m in sorted_movies[:3]]

    # Check how many were filtered
    watchlist = get_watchlist_movies(chat_id)
    watchlist_titles = [m["title"].lower() for m in watchlist]

    for s in suggestions:
        notes = []
        if s["title"].lower() in watchlist_titles:
            notes.append("Уже в вашем вотчлисте!")
        if s["rating"] >= 7.5:
            notes.append(f"Высокий рейтинг: {s['rating']:.1f}")
        s["note"] = ". ".join(notes) if notes else ""

    return json.dumps({
        "source": source_formatted,
        "similar": suggestions,
        "filtered_out": len(exclude_ids),
    }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
