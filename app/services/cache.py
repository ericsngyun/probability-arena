"""Best-effort Redis cache for the candidates endpoint. Any Redis failure
degrades to a live scan rather than an error."""

import logging

import redis

from app.config import get_settings

logger = logging.getLogger(__name__)

CANDIDATES_KEY = "candidates:v1"

_client: redis.Redis | None = None


def get_client() -> redis.Redis | None:
    global _client
    if _client is None:
        try:
            _client = redis.Redis.from_url(
                get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
            )
        except Exception:
            logger.warning("Redis client init failed; caching disabled", exc_info=True)
            return None
    return _client


def get_cached(key: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    try:
        value = client.get(key)
        return value.decode() if value else None
    except redis.RedisError:
        logger.debug("Redis GET failed; treating as cache miss")
        return None


def set_cached(key: str, value: str, ttl_seconds: int) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, value)
    except redis.RedisError:
        logger.debug("Redis SETEX failed; skipping cache write")
