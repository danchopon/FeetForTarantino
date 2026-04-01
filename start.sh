#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Start FastAPI in background
uvicorn api:app --reload --port 8000 &
API_PID=$!

# Start bot in foreground
python movie_watchlist_bot.py

# When bot exits, kill the API too
kill $API_PID 2>/dev/null
