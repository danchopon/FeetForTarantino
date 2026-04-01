import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

logger = logging.getLogger(__name__)


# ============== CONNECTION ==============

def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # Movies table with TMDB data
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            title VARCHAR(255) NOT NULL,
            status VARCHAR(20) DEFAULT 'to_watch',
            added_by VARCHAR(100),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            watched_by VARCHAR(100),
            watched_at TIMESTAMP,
            tmdb_id INT,
            year INT,
            rating REAL,
            poster_path VARCHAR(255),
            genres TEXT,
            overview TEXT,
            runtime INT,
            director VARCHAR(255)
        )
    """)

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_chat_title
        ON movies(chat_id, LOWER(title))
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_movies_chat_status
        ON movies(chat_id, status)
    """)

    # Vote basket
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vote_basket (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            user_name VARCHAR(100),
            movie_num INT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, user_id, movie_num)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_vote_basket_chat
        ON vote_basket(chat_id)
    """)

    conn.commit()

    # Add new columns if they don't exist (migration)
    # Each ALTER uses a SAVEPOINT so a DuplicateColumn error doesn't abort the whole transaction
    for col, col_type in [("tmdb_id", "INT"), ("year", "INT"), ("rating", "REAL"),
                          ("poster_path", "VARCHAR(255)"), ("genres", "TEXT"),
                          ("overview", "TEXT"), ("runtime", "INT"), ("director", "VARCHAR(255)")]:
        try:
            cur.execute("SAVEPOINT before_alter")
            cur.execute(f"ALTER TABLE movies ADD COLUMN {col} {col_type}")
            cur.execute("RELEASE SAVEPOINT before_alter")
        except psycopg2.errors.DuplicateColumn:
            cur.execute("ROLLBACK TO SAVEPOINT before_alter")

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database initialized")


# ============== MOVIES CRUD ==============

def add_movie_db(chat_id: int, title: str, added_by: str,
                 tmdb_id: int = None, year: int = None, rating: float = None,
                 poster_path: str = None, genres: str = None,
                 overview: str = None, runtime: int = None, director: str = None) -> tuple[bool, str]:
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """INSERT INTO movies (chat_id, title, added_by, tmdb_id, year, rating, poster_path, genres,
                                   overview, runtime, director)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (chat_id, title, added_by, tmdb_id, year, rating, poster_path, genres,
             overview, runtime, director)
        )
        conn.commit()
        return True, "added"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.execute(
            "SELECT status FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)",
            (chat_id, title)
        )
        row = cur.fetchone()
        return False, row["status"] if row else "exists"
    finally:
        cur.close()
        conn.close()


def get_movie_by_id(chat_id: int, movie_id: int) -> dict | None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def mark_watched_by_id(chat_id: int, movie_id: int, watched_by: str) -> tuple[bool, str | None]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, title, status FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return False, None

    if row["status"] == "watched":
        cur.close()
        conn.close()
        return False, row["title"]

    cur.execute(
        "UPDATE movies SET status = 'watched', watched_by = %s, watched_at = %s WHERE id = %s",
        (watched_by, datetime.now(), row["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def unwatch_movie_by_id(chat_id: int, movie_id: int) -> tuple[bool, str | None]:
    """Move a watched movie back to to_watch list."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, title, status FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return False, None

    if row["status"] != "watched":
        cur.close()
        conn.close()
        return False, row["title"]

    cur.execute(
        "UPDATE movies SET status = 'to_watch', watched_by = NULL, watched_at = NULL WHERE id = %s",
        (row["id"],)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def update_movie_tmdb_data(chat_id: int, movie_id: int, tmdb_id: int, year: int = None,
                           rating: float = None, poster_path: str = None, genres: str = None) -> bool:
    """Update movie with TMDB data."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """UPDATE movies
           SET tmdb_id = %s, year = %s, rating = %s, poster_path = %s, genres = %s
           WHERE chat_id = %s AND id = %s""",
        (tmdb_id, year, rating, poster_path, genres, chat_id, movie_id)
    )
    conn.commit()
    success = cur.rowcount > 0
    cur.close()
    conn.close()
    return success


def rename_movie_by_id(chat_id: int, movie_id: int, new_title: str) -> tuple[bool, str | None]:
    """Rename a movie."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, title FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return False, None

    old_title = row["title"]

    try:
        cur.execute(
            "UPDATE movies SET title = %s WHERE chat_id = %s AND id = %s",
            (new_title, chat_id, movie_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True, old_title
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return False, old_title


def remove_movie_by_id(chat_id: int, movie_id: int) -> str | None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT title FROM movies WHERE chat_id = %s AND id = %s", (chat_id, movie_id))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return None

    cur.execute("DELETE FROM movies WHERE id = %s", (movie_id,))
    conn.commit()
    cur.close()
    conn.close()
    return row["title"]


def mark_watched_db(chat_id: int, search: str, watched_by: str) -> tuple[bool, str | None]:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, title, status FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)",
        (chat_id, search)
    )
    row = cur.fetchone()

    if not row:
        cur.execute(
            "SELECT id, title, status FROM movies WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s) AND status = 'to_watch' LIMIT 1",
            (chat_id, f"%{search}%")
        )
        row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return False, None

    if row["status"] == "watched":
        cur.close()
        conn.close()
        return False, row["title"]

    cur.execute(
        "UPDATE movies SET status = 'watched', watched_by = %s, watched_at = %s WHERE id = %s",
        (watched_by, datetime.now(), row["id"])
    )
    conn.commit()
    cur.close()
    conn.close()
    return True, row["title"]


def remove_movie_db(chat_id: int, search: str) -> str | None:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, title FROM movies WHERE chat_id = %s AND LOWER(title) = LOWER(%s)", (chat_id, search))
    row = cur.fetchone()

    if not row:
        cur.execute("SELECT id, title FROM movies WHERE chat_id = %s AND LOWER(title) LIKE LOWER(%s) LIMIT 1", (chat_id, f"%{search}%"))
        row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return None

    cur.execute("DELETE FROM movies WHERE id = %s", (row["id"],))
    conn.commit()
    cur.close()
    conn.close()
    return row["title"]


def get_movies_db(chat_id: int, status: str | None = None) -> list[dict]:
    conn = get_db_connection()
    cur = conn.cursor()

    if status:
        cur.execute("SELECT * FROM movies WHERE chat_id = %s AND status = %s ORDER BY added_at", (chat_id, status))
    else:
        cur.execute("SELECT * FROM movies WHERE chat_id = %s ORDER BY status DESC, added_at", (chat_id,))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_counts_db(chat_id: int) -> dict:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT status, COUNT(*) as count FROM movies WHERE chat_id = %s GROUP BY status", (chat_id,))

    counts = {"to_watch": 0, "watched": 0}
    for row in cur.fetchall():
        counts[row["status"]] = row["count"]

    cur.close()
    conn.close()
    return counts


def get_watched_genres(chat_id: int) -> list[int]:
    """Get most common genres from watched movies."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT genres FROM movies WHERE chat_id = %s AND status = 'watched' AND genres IS NOT NULL", (chat_id,))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    genre_count = {}
    for row in rows:
        if row["genres"]:
            for g in row["genres"].split(","):
                g = g.strip()
                if g.isdigit():
                    gid = int(g)
                    genre_count[gid] = genre_count.get(gid, 0) + 1

    sorted_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)
    return [g[0] for g in sorted_genres[:3]]


def get_watched_tmdb_ids(chat_id: int) -> list[int]:
    """Get TMDB IDs of watched movies."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT tmdb_id FROM movies WHERE chat_id = %s AND tmdb_id IS NOT NULL", (chat_id,))
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return [row["tmdb_id"] for row in rows]


# ============== VOTE BASKET ==============

def add_to_basket(chat_id: int, user_id: int, user_name: str, movie_nums: list[int]) -> tuple[list[int], list[int]]:
    conn = get_db_connection()
    cur = conn.cursor()

    added = []
    exists = []

    for num in movie_nums:
        try:
            cur.execute(
                "INSERT INTO vote_basket (chat_id, user_id, user_name, movie_num) VALUES (%s, %s, %s, %s)",
                (chat_id, user_id, user_name, num)
            )
            conn.commit()
            added.append(num)
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            exists.append(num)

    cur.close()
    conn.close()
    return added, exists


def remove_from_basket(chat_id: int, user_id: int, movie_nums: list[int] | None = None) -> int:
    conn = get_db_connection()
    cur = conn.cursor()

    if movie_nums is None:
        cur.execute("DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    else:
        cur.execute("DELETE FROM vote_basket WHERE chat_id = %s AND user_id = %s AND movie_num = ANY(%s)", (chat_id, user_id, movie_nums))

    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def clear_basket(chat_id: int) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM vote_basket WHERE chat_id = %s", (chat_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def get_user_basket(chat_id: int, user_id: int) -> list[int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT movie_num FROM vote_basket WHERE chat_id = %s AND user_id = %s ORDER BY movie_num", (chat_id, user_id))
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums


def get_full_basket(chat_id: int) -> list[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, user_name, movie_num FROM vote_basket WHERE chat_id = %s ORDER BY user_name, movie_num", (chat_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_unique_basket_movies(chat_id: int) -> list[int]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT movie_num FROM vote_basket WHERE chat_id = %s ORDER BY movie_num", (chat_id,))
    nums = [row["movie_num"] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return nums
