"""Cache layer 1: raw HTTP responses from the public APIs.

Historical data never changes, so a cached response never expires. This
- protects the Open-Meteo free quota (it counts long requests as many calls),
- lets the scoring be re-run during development without re-downloading,
- keeps the original responses as an audit trail ("where did this number come from?").

Two backends with the same behaviour:
- Redis, when REDIS_URL is set (e.g. redis://localhost:6379/0). One key per response:
  httpcache:<source>:<hash>, value = JSON envelope {url, params, fetched_at, data}. No expiry.
  Shared by every machine that points at the same Redis.
- Files in data/raw/<source>/<hash>.json otherwise (the default; local, git-ignored: the app
  itself never reads this cache, only ingestion does).

Copy a local file cache into Redis once with:
    uv run python -m app.sources.http_cache import-files
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.settings import settings

USER_AGENT = "weather-risk-agent/0.1 (take-home prototype)"
KEY_PREFIX = "httpcache"


class SourceError(RuntimeError):
    pass


def _digest(url: str, params: dict) -> str:
    # The API key is excluded from the key so paid and free calls share a cache.
    key_params = {k: v for k, v in params.items() if k != "apikey"}
    return hashlib.sha256(json.dumps([url, key_params], sort_keys=True).encode()).hexdigest()[:16]


class FileStore:
    name = "files"

    def __init__(self, root: Path):
        self.root = root

    def location(self, source: str, digest: str) -> str:
        path = self.root / source / f"{digest}.json"
        try:
            return str(path.relative_to(self.root.parent.parent))
        except ValueError:
            return str(path)

    def get(self, source: str, digest: str) -> dict | None:
        path = self.root / source / f"{digest}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def put(self, source: str, digest: str, data, url: str, params: dict) -> None:
        path = self.root / source / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")  # the raw body, as before


class RedisStore:
    name = "redis"

    def __init__(self, client):
        self.client = client

    @staticmethod
    def key(source: str, digest: str) -> str:
        return f"{KEY_PREFIX}:{source}:{digest}"

    def location(self, source: str, digest: str) -> str:
        return f"redis:{self.key(source, digest)}"

    def get(self, source: str, digest: str) -> dict | None:
        raw = self.client.get(self.key(source, digest))
        return json.loads(raw)["data"] if raw else None

    def put(self, source: str, digest: str, data, url: str, params: dict,
            fetched_at: str | None = None) -> None:
        envelope = {"url": url, "params": {k: v for k, v in params.items() if k != "apikey"},
                    "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "data": data}
        self.client.set(self.key(source, digest), json.dumps(envelope))  # no TTL: history does not change


_STORE = None


def store():
    """The configured backend: Redis if REDIS_URL is set, else files."""
    global _STORE
    if _STORE is None:
        if settings.redis_url:
            import redis
            client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            try:
                client.ping()
            except redis.RedisError as exc:  # a configured but unreachable Redis is an error, not a silent fallback
                raise SourceError(f"REDIS_URL is set but Redis is not reachable ({settings.redis_url}): {exc}") from exc
            _STORE = RedisStore(client)
        else:
            _STORE = FileStore(settings.raw_cache_dir)
    return _STORE


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


@retry(retry=retry_if_exception(_is_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=1, max=20), reraise=True)
def _get(url: str, params: dict) -> dict:
    resp = httpx.get(url, params=params, timeout=60, headers={"User-Agent": USER_AGENT})
    if resp.status_code == 429 or (resp.status_code == 400 and "limit" in resp.text.lower()):
        # Rate limits are not retried: retrying burns more quota and will not succeed today.
        raise SourceError(f"{url}: rate limited ({resp.status_code}): {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def cached_get_json(source: str, url: str, params: dict, refresh: bool = False) -> tuple[dict, str, bool]:
    """Return (json, cache location, from_cache). Only successful responses are cached."""
    s, digest = store(), _digest(url, params)
    if not refresh:
        hit = s.get(source, digest)
        if hit is not None:
            return hit, s.location(source, digest), True
    data = _get(url, params)
    if isinstance(data, dict) and data.get("error"):
        raise SourceError(f"{source}: {data.get('reason', data)}")
    s.put(source, digest, data, url, params)
    return data, s.location(source, digest), False


def import_files(root: Path | None = None, target: RedisStore | None = None) -> int:
    """Copy every cached response file into Redis (same keys). Returns the number imported.

    The file names are the cache keys, so the request URL and parameters are not known for old
    files; they are stored as null, with fetched_at taken from the file's modification time."""
    root = root or settings.raw_cache_dir
    target = target or store()
    if not isinstance(target, RedisStore):
        raise SourceError("set REDIS_URL to import the files into Redis")
    n = 0
    for path in sorted(root.glob("*/*.json")):
        fetched = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        target.put(path.parent.name, path.stem, json.loads(path.read_text(encoding="utf-8")),
                   url=None, params={}, fetched_at=fetched)
        n += 1
    return n


def main(argv: list[str]) -> None:
    if argv[:1] == ["import-files"]:
        print(f"imported {import_files()} cached responses into {settings.redis_url}")
    elif argv[:1] == ["status"]:
        s = store()
        if isinstance(s, RedisStore):
            keys = list(s.client.scan_iter(f"{KEY_PREFIX}:*"))
            by_source = {}
            for k in keys:
                by_source[k.split(":")[1]] = by_source.get(k.split(":")[1], 0) + 1
            print(f"backend: redis ({settings.redis_url}), {len(keys)} responses: {by_source}")
        else:
            files = list(settings.raw_cache_dir.glob("*/*.json"))
            print(f"backend: files ({settings.raw_cache_dir}), {len(files)} responses")
    else:
        sys.exit("usage: python -m app.sources.http_cache [status | import-files]")


if __name__ == "__main__":
    main(sys.argv[1:])
