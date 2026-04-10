"""
OTP generation for the native iOS app auth flow.

The bot calls create_otp() when a user runs /app. The token is stored
in Redis for 5 minutes and deleted on first use by POST /auth/exchange.
"""

import json
import os
import secrets

import redis

_client: redis.Redis | None = None
_OTP_TTL = 300  # 5 minutes


def _get_client() -> redis.Redis | None:
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _client = redis.from_url(url, decode_responses=True)
    return _client


def create_otp(chat_id: int, user_id: int, user_name: str, chat_name: str) -> str | None:
    """
    Generate a single-use OTP token valid for 5 minutes.
    Returns the token string, or None if Redis is not configured.
    """
    client = _get_client()
    if client is None:
        return None
    token = secrets.token_urlsafe(32)
    payload = json.dumps({
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "chat_name": chat_name,
    })
    client.setex(f"otp:{token}", _OTP_TTL, payload)
    return token
