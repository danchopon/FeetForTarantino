"""
Groq AI integration for smart movie recommendations.

Uses Llama 3.1 70B via Groq to analyze watch history and generate
personalized movie suggestions, then enriches them with TMDB data.
"""

import os
import json
import logging
from groq import AsyncGroq
from bot.db import get_movies_db
from bot.tmdb_api import tmdb_search, TMDB_API_KEY
import httpx

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"
TMDB_BASE_URL = "https://api.themoviedb.org/3"

SYSTEM_PROMPT = """You are a knowledgeable movie recommendation expert.
Your job is to suggest movies based on a user's request and their group's viewing history.

Rules:
- Suggest exactly 3 movies
- Do NOT suggest movies already in the watched list or watchlist
- Consider the user's taste based on their watch history
- Return ONLY a valid JSON array, no other text

Response format:
[
  {
    "title": "Movie Title in English",
    "year": 2010,
    "reason": "Short explanation in Russian why this fits the request (1-2 sentences)"
  }
]"""


def _build_user_prompt(query: str, watched: list[dict], watchlist: list[dict]) -> str:
    parts = []

    if watched:
        watched_lines = []
        for m in watched[:30]:  # limit context
            line = f"- {m['title']}"
            if m.get("year"):
                line += f" ({m['year']})"
            if m.get("genres"):
                line += f" [{m['genres']}]"
            if m.get("rating"):
                line += f" ⭐{m['rating']}"
            watched_lines.append(line)
        parts.append("Already watched (DO NOT suggest these):\n" + "\n".join(watched_lines))
    else:
        parts.append("Watch history: empty (new group)")

    if watchlist:
        wl_titles = [m["title"] for m in watchlist[:20]]
        parts.append("Already in watchlist (DO NOT suggest these):\n" + "\n".join(f"- {t}" for t in wl_titles))

    user_request = query if query else "general recommendations based on the watch history"
    parts.append(f"User request: {user_request}")

    return "\n\n".join(parts)


async def _enrich_with_tmdb(title: str, year: int | None) -> dict | None:
    """Search TMDB for a movie and return enriched data."""
    if not TMDB_API_KEY:
        return None

    async def _search(lang: str) -> list[dict]:
        params = {"api_key": TMDB_API_KEY, "query": title, "language": lang}
        if year:
            params["year"] = year
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{TMDB_BASE_URL}/search/movie", params=params)
            if resp.status_code == 200:
                return resp.json().get("results", [])
        return []

    results = await _search("ru-RU")
    if not results:
        results = await _search("en-US")
    if not results:
        return None

    # Pick best match: prefer year match + highest vote_count
    candidates = results[:5]
    if year:
        year_matched = [r for r in candidates if r.get("release_date", "")[:4] == str(year)]
        if year_matched:
            candidates = year_matched

    movie = max(candidates, key=lambda r: r.get("vote_count", 0))

    # Fetch full movie details for accurate rating
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{movie['id']}",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"},
        )
        if resp.status_code == 200:
            movie = resp.json()

    release_year = movie.get("release_date", "")[:4]
    return {
        "tmdb_id": movie["id"],
        "title": movie.get("title", title),
        "year": release_year,
        "rating": movie.get("vote_average", 0),
        "overview": movie.get("overview", ""),
        "poster_path": movie.get("poster_path", ""),
    }


async def get_ai_suggestions(chat_id: int, query: str) -> list[dict]:
    """
    Generate movie recommendations using Groq AI + TMDB enrichment.

    Args:
        chat_id: Telegram chat ID (for watch history context)
        query: User's request (mood, genre, description). Can be empty.

    Returns:
        List of movie dicts with tmdb_id, title, year, rating, overview, reason.
        Empty list on error.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set in environment")

    watched = get_movies_db(chat_id, status="watched")
    watchlist = get_movies_db(chat_id, status="to_watch")

    user_prompt = _build_user_prompt(query, watched, watchlist)

    client = AsyncGroq(api_key=GROQ_API_KEY)
    try:
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=600,
        )
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        raise

    raw = response.choices[0].message.content.strip()
    logger.info(f"Groq raw response: {raw[:200]}")

    # Parse JSON from response
    try:
        # Handle cases where model wraps JSON in ```json ... ```
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        suggestions = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Groq JSON: {e}\nRaw: {raw}")
        return []

    # Enrich each suggestion with TMDB data
    all_existing_titles = {m["title"].lower() for m in watched + watchlist}
    results = []

    for s in suggestions:
        if not isinstance(s, dict):
            continue
        title = s.get("title", "")
        year = s.get("year")
        reason = s.get("reason", "")

        if not title:
            continue

        tmdb = await _enrich_with_tmdb(title, year)
        if not tmdb:
            # Include without TMDB data
            results.append({"title": title, "year": str(year) if year else "", "reason": reason,
                            "tmdb_id": None, "rating": 0, "overview": ""})
            continue

        # Skip if it ended up being something already in the list
        if tmdb["title"].lower() in all_existing_titles:
            continue

        results.append({**tmdb, "reason": reason})

    return results
