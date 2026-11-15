"""The raw HTTP cache: file and Redis backends behave the same (Redis via fakeredis, no server)."""
import json

import fakeredis
import pytest

from app.sources import http_cache as hc


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params):
        seen.append((url, params))
        return {"daily": {"time": ["2025-01-01"], "snowfall_sum": [1.5]}}
    monkeypatch.setattr(hc, "_get", fake_get)
    return seen


@pytest.fixture
def redis_store(monkeypatch):
    store = hc.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(hc, "_STORE", store)
    return store


def test_redis_backend_caches_and_keeps_an_audit_envelope(redis_store, calls):
    params = {"latitude": 39.7, "apikey": "secret"}
    data, loc, from_cache = hc.cached_get_json("open_meteo", "https://api.example/x", params)
    assert not from_cache and loc.startswith("redis:httpcache:open_meteo:")
    data2, _, from_cache2 = hc.cached_get_json("open_meteo", "https://api.example/x", params)
    assert from_cache2 and data2 == data and len(calls) == 1          # second call: no API request
    raw = json.loads(redis_store.client.get(loc.removeprefix("redis:")))
    assert raw["url"] == "https://api.example/x" and "apikey" not in raw["params"] and raw["fetched_at"]
    assert redis_store.client.ttl(loc.removeprefix("redis:")) == -1   # no expiry: history does not change
    hc.cached_get_json("open_meteo", "https://api.example/x", params, refresh=True)
    assert len(calls) == 2                                            # refresh bypasses the cache


def test_file_and_redis_use_the_same_keys_and_import_copies_files(tmp_path, redis_store, calls, monkeypatch):
    files = hc.FileStore(tmp_path / "raw")
    monkeypatch.setattr(hc, "_STORE", files)
    data, _, _ = hc.cached_get_json("fema_declarations", "https://fema.example", {"fips": "48113"})
    assert hc.import_files(tmp_path / "raw", redis_store) == 1
    monkeypatch.setattr(hc, "_STORE", redis_store)
    again, loc, from_cache = hc.cached_get_json("fema_declarations", "https://fema.example", {"fips": "48113"})
    assert from_cache and again == data and len(calls) == 1          # served from Redis after the import


def test_unreachable_redis_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(hc, "_STORE", None)
    monkeypatch.setattr(hc.settings, "redis_url", "redis://127.0.0.1:1/0")
    with pytest.raises(hc.SourceError, match="not reachable"):
        hc.store()
    monkeypatch.setattr(hc, "_STORE", None)
