"""
Synchronous Redis publisher used by the bot process.

The API process subscribes to the same channels and broadcasts events
to connected WebSocket clients. Both processes share only the database
and Redis — no direct IPC.
"""

import json
import os

import redis

_client: redis.Redis | None = None


def _get_client() -> redis.Redis | None:
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _client = redis.from_url(url, decode_responses=True)
    return _client


def publish_event(chat_id: int, event: str, data: dict) -> None:
    """Publish a chat event to Redis. Silently no-ops if Redis is not configured."""
    client = _get_client()
    if client is None:
        return
    try:
        payload = json.dumps({"event": event, "chat_id": chat_id, "data": data})
        client.publish(f"chat_events:{chat_id}", payload)
    except Exception:
        pass
