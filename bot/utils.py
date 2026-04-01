def format_movie(movie: dict, idx: int = None) -> str:
    """Format movie for display."""
    parts = []
    if idx:
        parts.append(f"{idx}.")

    parts.append(movie["title"])

    if movie.get("year"):
        parts.append(f"({movie['year']})")

    if movie.get("rating"):
        parts.append(f"\u2b50{movie['rating']:.1f}")

    return " ".join(parts)
