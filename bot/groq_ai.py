"""
Groq AI integration for smart movie recommendations (/rec command).

Handles three intents automatically:
- similar: "как Inception", "похожее на Начало"
- mood: "мрачный триллер", "что-то весёлое"
- history: empty query → based on group's watch history
"""

import os
import json
import logging
import httpx
from groq import AsyncGroq
from bot.db import get_movies_db

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")

SYSTEM_PROMPT = """You are a smart movie recommendation assistant for a group chat.

Analyze the user's request and watch history, then suggest exactly 5 movies.

First, detect the intent:
- "similar" — user mentions a specific movie title (e.g. "like Inception", "похожее на Начало", "как Prestige")
- "mood" — user describes a vibe, genre, or theme (e.g. "мрачный триллер", "something funny", "sci-fi с хорошим сюжетом")
- "history" — empty request or just asks for recommendations without specifics

Rules:
- DO NOT suggest movies already in the watched list or watchlist
- For "similar" intent: recommend movies similar in style, theme, or director to the mentioned film
- For "mood" intent: match the described vibe, reference specific watched films when relevant
- For "history" intent: analyze patterns in watch history (genres, ratings, directors) and suggest accordingly
- Reasons must be in Russian, personal and specific (e.g. "Вам понравился Prisoners — тот же режиссёр и атмосфера напряжения")
- Return ONLY valid JSON, no other text

Response format:
{
  "intent": "similar|mood|history",
  "source_movie": "Movie Title (only for similar intent, otherwise null)",
  "suggestions": [
    {
      "title": "Movie Title in English",
      "year": 2010,
      "reason": "Объяснение на русском (1-2 предложения, конкретное)"
    }
  ]
}"""


def _build_user_prompt(query: str, watched: list[dict], watchlist: list[dict]) -> str:
    parts = []

    if watched:
        lines = []
        for m in watched[:30]:
            line = f"- {m['title']}"
            if m.get("year"):
                line += f" ({m['year']})"
            if m.get("genres"):
                line += f" [{m['genres']}]"
            if m.get("rating"):
                line += f" ⭐{m['rating']}"
            lines.append(line)
        parts.append("Already watched (DO NOT suggest):\n" + "\n".join(lines))
    else:
        parts.append("Watch history: empty (new group)")

    if watchlist:
        titles = [m["title"] for m in watchlist[:20]]
        parts.append("Already in watchlist (DO NOT suggest):\n" + "\n".join(f"- {t}" for t in titles))

    parts.append(f"User request: {query if query else '(no specific request — recommend based on history)'}")

    return "\n\n".join(parts)


async def _enrich_with_tmdb(title: str, year: int | None) -> dict | None:
    """Search TMDB and return enriched movie data with full details."""
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

    candidates = results[:5]
    if year:
        year_matched = [r for r in candidates if r.get("release_date", "")[:4] == str(year)]
        if year_matched:
            candidates = year_matched

    movie = max(candidates, key=lambda r: r.get("vote_count", 0))

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{movie['id']}",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"},
        )
        if resp.status_code == 200:
            movie = resp.json()

    return {
        "tmdb_id": movie["id"],
        "title": movie.get("title", title),
        "year": movie.get("release_date", "")[:4],
        "rating": movie.get("vote_average", 0),
        "overview": movie.get("overview", ""),
        "poster_path": movie.get("poster_path", ""),
    }


async def get_rec_suggestions(chat_id: int, query: str) -> dict:
    """
    Smart recommendations with automatic intent detection.

    Args:
        chat_id: Telegram chat ID (for watch history context)
        query: Free-form user request. Can be empty, mood description, or movie name.

    Returns:
        {
            "intent": "similar|mood|history",
            "source_movie": str | None,
            "suggestions": list of movie dicts with tmdb data + reason
        }
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY не задан в .env")

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
            max_tokens=1200,
        )
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        raise

    raw = response.choices[0].message.content.strip()
    logger.info(f"Groq response: {raw[:300]}")

    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Groq JSON: {e}\nRaw: {raw}")
        return {"intent": "history", "source_movie": None, "suggestions": []}

    intent = data.get("intent", "history")
    source_movie = data.get("source_movie")
    raw_suggestions = data.get("suggestions", [])

    all_existing = {m["title"].lower() for m in watched + watchlist}
    suggestions = []

    for s in raw_suggestions:
        if not isinstance(s, dict) or not s.get("title"):
            continue

        tmdb = await _enrich_with_tmdb(s["title"], s.get("year"))
        if not tmdb:
            suggestions.append({
                "title": s["title"],
                "year": str(s.get("year", "")),
                "reason": s.get("reason", ""),
                "tmdb_id": None,
                "rating": 0,
                "overview": "",
            })
            continue

        if tmdb["title"].lower() in all_existing:
            continue

        suggestions.append({**tmdb, "reason": s.get("reason", "")})

    return {"intent": intent, "source_movie": source_movie, "suggestions": suggestions}
