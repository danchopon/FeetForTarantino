import os
import re

import httpx

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/w500"


def parse_movie_query(query: str) -> tuple[str, int | None]:
    """Parse movie title and year from query.

    Examples:
        "Inception 2010" -> ("Inception", 2010)
        "Начало (2010)" -> ("Начало", 2010)
        "The Matrix" -> ("The Matrix", None)
    """
    # Pattern 1: "Title (YYYY)"
    match = re.search(r'^(.+?)\s*\((\d{4})\)\s*$', query)
    if match:
        return match.group(1).strip(), int(match.group(2))

    # Pattern 2: "Title YYYY" (year at the end)
    match = re.search(r'^(.+?)\s+(\d{4})\s*$', query)
    if match:
        title = match.group(1).strip()
        year = int(match.group(2))
        if 1900 <= year <= 2030:
            return title, year

    return query.strip(), None


async def tmdb_search(query: str, page: int = 1, year: int | None = None) -> dict:
    """Search TMDB for movies with pagination and year filter.

    Args:
        query: Movie title to search
        page: Page number (1-based)
        year: Optional year filter (will search ±2 years)
    """
    if not TMDB_API_KEY:
        return {"results": [], "total_pages": 0, "page": 1}

    async def search_tmdb(search_query: str, lang: str) -> dict:
        params = {
            "api_key": TMDB_API_KEY,
            "query": search_query,
            "language": lang,
            "page": page,
        }
        if year:
            params["year"] = year

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{TMDB_BASE_URL}/search/movie", params=params)
            if resp.status_code == 200:
                return resp.json()
        return {"results": [], "total_pages": 0, "page": 1}

    # Search in Russian first
    data_ru = await search_tmdb(query, "ru-RU")
    results = data_ru.get("results", [])

    # If few results, try English as well
    if len(results) < 5:
        data_en = await search_tmdb(query, "en-US")
        en_results = data_en.get("results", [])

        existing_ids = {r.get("id") for r in results}
        for r in en_results:
            if r.get("id") not in existing_ids:
                results.append(r)
                existing_ids.add(r.get("id"))

    # If year specified, filter results to ±2 years
    if year and results:
        filtered = []
        for movie in results:
            release_date = movie.get("release_date", "")
            if release_date:
                try:
                    movie_year = int(release_date[:4])
                    if year - 2 <= movie_year <= year + 2:
                        filtered.append(movie)
                except (ValueError, IndexError):
                    pass
            else:
                filtered.append(movie)
        results = filtered

    results.sort(key=lambda x: (x.get("vote_count", 0) * x.get("vote_average", 0)), reverse=True)

    total_pages = min(data_ru.get("total_pages", 0), 10)

    return {
        "results": results,
        "total_pages": total_pages,
        "page": page,
    }


async def tmdb_get_movie(tmdb_id: int) -> dict | None:
    """Get movie details from TMDB."""
    if not TMDB_API_KEY:
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"},
        )
        if resp.status_code == 200:
            return resp.json()
    return None


async def tmdb_get_recommendations(tmdb_id: int) -> list[dict]:
    """Get movie recommendations from TMDB."""
    if not TMDB_API_KEY:
        return []

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/movie/{tmdb_id}/recommendations",
            params={"api_key": TMDB_API_KEY, "language": "ru-RU"},
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("results", [])[:10]
    return []


async def tmdb_discover_by_genres(genre_ids: list[int], exclude_ids: list[int] = None) -> list[dict]:
    """Discover movies by genres."""
    if not TMDB_API_KEY:
        return []

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE_URL}/discover/movie",
            params={
                "api_key": TMDB_API_KEY,
                "language": "ru-RU",
                "with_genres": ",".join(map(str, genre_ids)),
                "sort_by": "vote_average.desc",
                "vote_count.gte": 100,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if exclude_ids:
                results = [m for m in results if m["id"] not in exclude_ids]
            return results[:10]
    return []
