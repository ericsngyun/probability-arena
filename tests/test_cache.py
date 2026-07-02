"""Redis cache must be best-effort: any failure degrades to a cache miss,
never an exception reaching the endpoint."""

import redis

from app.services import cache


class ExplodingClient:
    def get(self, key):
        raise redis.ConnectionError("redis down")

    def setex(self, key, ttl, value):
        raise redis.ConnectionError("redis down")


class MemoryClient:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value.encode() if isinstance(value, str) else value


def test_get_cached_returns_none_when_redis_errors(monkeypatch):
    monkeypatch.setattr(cache, "get_client", lambda: ExplodingClient())
    assert cache.get_cached("candidates:v1:25") is None


def test_set_cached_swallows_redis_errors(monkeypatch):
    monkeypatch.setattr(cache, "get_client", lambda: ExplodingClient())
    cache.set_cached("candidates:v1:25", "{}", 30)  # must not raise


def test_cache_disabled_when_client_unavailable(monkeypatch):
    monkeypatch.setattr(cache, "get_client", lambda: None)
    assert cache.get_cached("k") is None
    cache.set_cached("k", "v", 30)  # must not raise


def test_round_trip_when_redis_healthy(monkeypatch):
    client = MemoryClient()
    monkeypatch.setattr(cache, "get_client", lambda: client)
    assert cache.get_cached("k") is None
    cache.set_cached("k", '{"cached": true}', 30)
    assert cache.get_cached("k") == '{"cached": true}'
